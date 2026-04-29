# aggregator.py
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from engine import Engine
from process_engine import ProcessEngine
from hardware_engine import HardwareEngine


@dataclass
class UnifiedSample:
    """One unified moment-in-time across all data sources."""
    timestamp: float

    # System-wide percentages (0-100), from psutil
    cpu_pct: float
    ram_pct: float

    # Disk throughput in MB/s (rates), from psutil
    disk_read_mb_s: float
    disk_write_mb_s: float

    # Hardware sensors from LHM (None if unavailable on this machine)
    cpu_load_lhm: Optional[float] = None       # CPU Total load %
    cpu_core_max: Optional[float] = None        # hottest single core %
    cpu_temp: Optional[float] = None            # °C
    cpu_power: Optional[float] = None           # W
    gpu_load: Optional[float] = None            # GPU Core %
    gpu_temp: Optional[float] = None            # °C
    gpu_memory_used_mb: Optional[float] = None  # MB
    memory_load_lhm: Optional[float] = None     # %
    fan_rpm_max: Optional[float] = None         # RPM (hottest fan)

    # Process info
    top_processes: List[Dict[str, Any]] = field(default_factory=list)
    spawned: List[Dict[str, Any]] = field(default_factory=list)
    exited: List[int] = field(default_factory=list)

    # Keep a reference to raw sensors for the UI detail view
    raw_sensors: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class MetricsAggregator(Engine):
    """
    Reads ProcessEngine and HardwareEngine snapshots every tick and
    produces a UnifiedSample. This is the canonical time-series the
    detector and API read from.
    """

    def __init__(self, proc_engine: ProcessEngine, hw_engine: HardwareEngine,
                 interval: float = 1.0, history_size: int = 7200):
        # 7200 = 2 hours at 1Hz
        super().__init__("Aggregator", interval, history_size)
        self.proc = proc_engine
        self.hw = hw_engine

    def poll(self) -> Optional[UnifiedSample]:
        ps = self.proc.snapshot()
        hs = self.hw.snapshot()

        # If we have no process data yet, skip — process metrics are non-optional
        if ps is None:
            return None

        sample = UnifiedSample(
            timestamp=time.time(),
            cpu_pct=ps.cpu_percent,
            ram_pct=ps.memory_percent,
            disk_read_mb_s=ps.disk_read_mb_s,
            disk_write_mb_s=ps.disk_write_mb_s,
            top_processes=ps.processes,
            spawned=ps.spawned,
            exited=ps.exited,
        )

        if hs is not None:
            self._fill_hardware(sample, hs.sensors)
            sample.raw_sensors = hs.sensors

        return sample

    def _fill_hardware(self, sample: UnifiedSample, sensors: Dict[str, Dict[str, Any]]):
        """
        Walk the hardware sensors dict and pick out the values we want.
        Sensor names vary by hardware — this scanning approach is more
        robust than hardcoded paths.
        """
        fan_rpms = []

        for hw_name, sensor_dict in sensors.items():
            hw_lower = hw_name.lower()
            is_cpu = "ryzen" in hw_lower or "intel" in hw_lower or "cpu" in hw_lower
            is_gpu = "radeon" in hw_lower or "nvidia" in hw_lower or "graphics" in hw_lower or "gpu" in hw_lower
            is_memory = "memory" in hw_lower and "virtual" not in hw_lower

            for sname, sval in sensor_dict.items():
                # sval is (sensor_type, value)
                stype, value = sval
                sname_lower = sname.lower()

                if is_cpu:
                    if stype == "Load" and sname_lower == "cpu total":
                        sample.cpu_load_lhm = value
                    elif stype == "Load" and sname_lower == "cpu core max":
                        sample.cpu_core_max = value
                    elif stype == "Temperature" and "tctl" in sname_lower and value > 0:
                        sample.cpu_temp = value
                    elif stype == "Power" and sname_lower == "package" and value > 0:
                        sample.cpu_power = value

                elif is_gpu:
                    if stype == "Load" and sname_lower == "gpu core":
                        sample.gpu_load = value
                    elif stype == "Temperature" and "gpu" in sname_lower and value > 0:
                        sample.gpu_temp = value
                    elif stype == "SmallData" and sname_lower == "gpu memory used":
                        sample.gpu_memory_used_mb = value

                elif is_memory:
                    if stype == "Load" and sname_lower == "memory":
                        sample.memory_load_lhm = value

                # Fan RPMs can come from anywhere
                if stype == "Fan":
                    fan_rpms.append(value)

        if fan_rpms:
            sample.fan_rpm_max = max(fan_rpms)