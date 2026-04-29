# monitor.py
import time
import os
import ctypes

from process_engine import ProcessEngine
from hardware_engine import HardwareEngine
from aggregator import MetricsAggregator
from detector import Detector

LHM_DLL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "LibreHardwareMonitorLib.dll")


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def make_anomaly_printer():
    """Returns a callback that prints anomalies in a loud, obvious format."""
    def cb(event_type, anomaly):
        if event_type == "started":
            print("\n" + "!" * 70)
            print(f"  ANOMALY DETECTED: {anomaly.label}")
            print(f"    peak    = {anomaly.peak_value:.1f} {anomaly.unit}")
            print(f"    baseline= {anomaly.baseline:.1f} {anomaly.unit}")
            print(f"    threshold= {anomaly.threshold:.1f} {anomaly.unit}")
            if anomaly.suspects:
                print(f"  Suspects:")
                for s in anomaly.suspects:
                    flag = f" [{s.flag}]" if s.flag else ""
                    print(f"    - {s.name} (pid {s.pid})  delta={s.delta:+.1f}{flag}")
            else:
                print(f"  Suspects: (none identified)")
            print("!" * 70 + "\n")
        elif event_type == "ended":
            duration = (anomaly.ended_at or time.time()) - anomaly.started_at
            print(f"\n  >> Anomaly ended: {anomaly.label} (lasted {duration:.0f}s)\n")
    return cb


def main():
    if not is_admin():
        print("Note: not running as admin, some hardware sensors may be unavailable.")

    proc = ProcessEngine(interval=1.0, top_n=8)
    hw = HardwareEngine(lhm_dll_path=LHM_DLL, interval=2.0)
    agg = MetricsAggregator(proc, hw, interval=1.0)

    proc.start()
    hw.start()
    time.sleep(1.5)
    agg.start()

    # Detector starts after we have some history to compute baselines.
    # In production we'd let it boot immediately and skip until min_baseline
    # is reached, but for a clearer demo we wait.
    print("Waiting 30s to build baseline before starting detector...")
    time.sleep(30)

    det = Detector(
        agg,
        interval=3.0,
        baseline_window_sec=120.0,   # 2 min — shorter for demo so spikes are detected sooner
        recent_window_sec=8.0,
        min_baseline_samples=20,
        sigma=3.0,
        consecutive_required=3,
        cooldown_sec=30.0,
    )
    det.on_event = make_anomaly_printer()
    det.start()
    print("Detector armed. Try running a memory hog or CPU-burner to trigger an anomaly.\n")

    try:
        while True:
            time.sleep(2.0)
            sample = agg.snapshot()
            if sample is None:
                continue

            print("=" * 70)
            print(f"  {time.strftime('%H:%M:%S', time.localtime(sample.timestamp))}  "
                  f"CPU {sample.cpu_pct:5.1f}%   "
                  f"RAM {sample.ram_pct:5.1f}%   "
                  f"Disk R {sample.disk_read_mb_s:5.1f}  W {sample.disk_write_mb_s:5.1f} MB/s")

            hw_parts = []
            if sample.cpu_load_lhm is not None:
                hw_parts.append(f"CPU(LHM) {sample.cpu_load_lhm:5.1f}%")
            if sample.cpu_core_max is not None:
                hw_parts.append(f"CoreMax {sample.cpu_core_max:5.1f}%")
            if sample.gpu_load is not None:
                hw_parts.append(f"GPU {sample.gpu_load:5.1f}%")
            if hw_parts:
                print("  " + "   ".join(hw_parts))

            active = det.active_anomalies()
            if active:
                print(f"  ACTIVE: {', '.join(a.label for a in active)}")

            print(f"  [history: {len(agg.history())} samples | "
                  f"anomalies: {len(det.anomalies())}]")

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        try: det.stop()
        except Exception: pass
        agg.stop()
        hw.stop()
        proc.stop()


if __name__ == "__main__":
    main()