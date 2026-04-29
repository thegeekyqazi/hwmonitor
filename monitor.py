# monitor.py
import time
import os
import ctypes
import sys

from process_engine import ProcessEngine
from hardware_engine import HardwareEngine
from aggregator import MetricsAggregator

LHM_DLL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "LibreHardwareMonitorLib.dll")


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def main():
    if not is_admin():
        print("Note: not running as admin, some hardware sensors may be unavailable.")

    proc = ProcessEngine(interval=1.0, top_n=8)
    hw = HardwareEngine(lhm_dll_path=LHM_DLL, interval=2.0)
    agg = MetricsAggregator(proc, hw, interval=1.0)

    proc.start()
    hw.start()
    # Give the engines a moment to produce their first snapshot before aggregator starts
    time.sleep(1.5)
    agg.start()

    try:
        while True:
            time.sleep(2.0)
            sample = agg.snapshot()

            if sample is None:
                print("Waiting for first sample...")
                continue

            print("\n" + "=" * 70)
            print(f"  {time.strftime('%H:%M:%S', time.localtime(sample.timestamp))}  "
                  f"CPU {sample.cpu_pct:5.1f}%   "
                  f"RAM {sample.ram_pct:5.1f}%   "
                  f"Disk R {sample.disk_read_mb_s:5.1f}  W {sample.disk_write_mb_s:5.1f} MB/s")

            # Hardware line — only show what we actually have
            hw_parts = []
            if sample.cpu_load_lhm is not None:
                hw_parts.append(f"CPU Load(LHM) {sample.cpu_load_lhm:5.1f}%")
            if sample.cpu_core_max is not None:
                hw_parts.append(f"CoreMax {sample.cpu_core_max:5.1f}%")
            if sample.cpu_temp is not None:
                hw_parts.append(f"CPU {sample.cpu_temp:.0f}°C")
            if sample.gpu_load is not None:
                hw_parts.append(f"GPU {sample.gpu_load:5.1f}%")
            if sample.gpu_temp is not None:
                hw_parts.append(f"GPU {sample.gpu_temp:.0f}°C")
            if sample.fan_rpm_max is not None:
                hw_parts.append(f"Fan {sample.fan_rpm_max:.0f} RPM")
            if hw_parts:
                print("  " + "   ".join(hw_parts))

            # Top processes
            print("-" * 70)
            for p in sample.top_processes:
                print(f"  {p['name'][:28]:28}  cpu={p['cpu']:5.1f}  mem={p['mem_mb']:7.1f} MB")

            # Lifecycle events
            if sample.spawned:
                names = [s['name'] for s in sample.spawned[:5]]
                print(f"  >>> SPAWNED: {names}{' ...' if len(sample.spawned) > 5 else ''}")
            if sample.exited:
                print(f"  >>> EXITED: {len(sample.exited)} pid(s)")

            # Quick history sanity check (every ~10s)
            history_len = len(agg.history())
            print(f"  [history: {history_len} samples buffered]")

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        agg.stop()
        hw.stop()
        proc.stop()


if __name__ == "__main__":
    main()