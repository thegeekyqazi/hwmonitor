# monitor.py
import time
import os
from process_engine import ProcessEngine
from hardware_engine import HardwareEngine

LHM_DLL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "LibreHardwareMonitorLib.dll")


def main():
    proc = ProcessEngine(interval=1.0, top_n=8)
    hw = HardwareEngine(lhm_dll_path=LHM_DLL, interval=2.0)

    proc.start()
    hw.start()

    try:
        while True:
            time.sleep(2.0)
            ps = proc.snapshot()
            hs = hw.snapshot()

            print("\n" + "=" * 70)
            if ps:
                print(f"CPU {ps.cpu_percent:5.1f}%   "
                      f"RAM {ps.memory_percent:5.1f}%   "
                      f"Disk R {ps.disk_read_mb_s:6.1f} MB/s   "
                      f"W {ps.disk_write_mb_s:6.1f} MB/s")
                print("-" * 70)
                for p in ps.processes:
                    print(f"  {p['name'][:28]:28}  cpu={p['cpu']:5.1f}  mem={p['mem_mb']:7.1f} MB")
            if hs:
                print("-" * 70)
                for hw_name, ss in hs.sensors.items():
                    print(f"[{hw_name}]")
                    for sname, (stype, val) in ss.items():
                        print(f"  {stype:12} {sname[:30]:30} {val:8.2f}")
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        proc.stop()
        hw.stop()


if __name__ == "__main__":
    main()