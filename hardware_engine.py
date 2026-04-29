# hardware_engine.py
import time
from dataclasses import dataclass
from typing import Dict, Tuple

from engine import Engine


@dataclass
class HardwareSnapshot:
    timestamp: float
    # {hardware_name: {sensor_name: (sensor_type, value)}}
    sensors: Dict[str, Dict[str, Tuple[str, float]]]


class HardwareEngine(Engine):
    def __init__(self, lhm_dll_path: str, interval: float = 2.0):
        super().__init__("HardwareEngine", interval)
        self.lhm_dll_path = lhm_dll_path
        self._computer = None

    def setup(self):
        import clr  # pythonnet
        import os
        
        lib_dir = os.path.dirname(self.lhm_dll_path)
        
    
        deps = [
            "System.Memory.dll",
            "System.Buffers.dll",
            "System.Numerics.Vectors.dll",
            "System.Runtime.CompilerServices.Unsafe.dll",
            "HidSharp.dll",  # LHM also needs this one
        ]
        for dep in deps:
            dep_path = os.path.join(lib_dir, dep)
            if os.path.exists(dep_path):
                clr.AddReference(dep_path)
    
        # Now load LHM itself
        clr.AddReference(self.lhm_dll_path)
        from LibreHardwareMonitor.Hardware import Computer
        
        c = Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsMemoryEnabled = True
        c.IsMotherboardEnabled = True
        c.IsControllerEnabled = True
        c.IsStorageEnabled = False
        c.Open()
        self._computer = c

    def teardown(self):
        if self._computer is not None:
            self._computer.Close()
            self._computer = None

    def poll(self) -> HardwareSnapshot:
        sensors: Dict[str, Dict[str, Tuple[str, float]]] = {}
        for hw in self._computer.Hardware:
            hw.Update()
            for sub in hw.SubHardware:
                sub.Update()
            data: Dict[str, Tuple[str, float]] = {}
            for s in hw.Sensors:
                if s.Value is not None:
                    data[str(s.Name)] = (str(s.SensorType), float(s.Value))
            if data:
                sensors[str(hw.Name)] = data
        return HardwareSnapshot(timestamp=time.time(), sensors=sensors)