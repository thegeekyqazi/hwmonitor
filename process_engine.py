# process_engine.py
import time
from dataclasses import dataclass
from typing import List, Dict, Any

import psutil
from engine import Engine


@dataclass
class ProcessSnapshot:
    timestamp: float
    cpu_percent: float
    memory_percent: float
    disk_read_mb_s: float
    disk_write_mb_s: float
    processes: List[Dict[str, Any]]


class ProcessEngine(Engine):
    def __init__(self, interval: float = 1.0, top_n: int = 20):
        super().__init__("ProcessEngine", interval)
        self.top_n = top_n
        self._last_disk = None
        self._last_disk_t = None

    def setup(self):
        # psutil's cpu_percent needs a priming call; first real reading comes next tick
        psutil.cpu_percent(None)
        for p in psutil.process_iter():
            try:
                p.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self._last_disk = psutil.disk_io_counters()
        self._last_disk_t = time.monotonic()

    def poll(self) -> ProcessSnapshot:
        # disk throughput from delta of cumulative counters
        now = time.monotonic()
        cur = psutil.disk_io_counters()
        dt = max(now - self._last_disk_t, 1e-6)
        read_mb_s = (cur.read_bytes - self._last_disk.read_bytes) / dt / 1e6
        write_mb_s = (cur.write_bytes - self._last_disk.write_bytes) / dt / 1e6
        self._last_disk, self._last_disk_t = cur, now

        procs = []
        for p in psutil.process_iter(["pid", "name", "username", "memory_info"]):
            try:
                info = p.info
                cpu = p.cpu_percent(None)
                rss = info["memory_info"].rss if info["memory_info"] else 0
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"] or "?",
                    "user": info["username"] or "",
                    "cpu": cpu,
                    "mem_mb": rss / (1024 * 1024),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs.sort(key=lambda x: x["cpu"], reverse=True)

        return ProcessSnapshot(
            timestamp=time.time(),
            cpu_percent=psutil.cpu_percent(None),
            memory_percent=psutil.virtual_memory().percent,
            disk_read_mb_s=read_mb_s,
            disk_write_mb_s=write_mb_s,
            processes=procs[: self.top_n],
        )