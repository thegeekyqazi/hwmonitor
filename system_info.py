# system_info.py
"""
Collects system information and a deep hardware inventory.
Uses psutil + WMI + LibreHardwareMonitor where each is best.
All collection happens once at startup; results are cached.
"""
import platform
import socket
import psutil
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_system_info(hw_engine=None) -> Dict[str, Any]:
    """High-level system summary."""
    uname = platform.uname()
    cpu_freq = psutil.cpu_freq()
    vm = psutil.virtual_memory()
    return {
        "hostname": socket.gethostname(),
        "os": {
            "system": uname.system,
            "release": uname.release,
            "version": uname.version,
            "machine": uname.machine,
        },
        "python_version": platform.python_version(),
        "cpu": {
            "model": _cpu_friendly_name(hw_engine) or platform.processor() or uname.processor,
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
            "max_freq_mhz": round(cpu_freq.max, 0) if cpu_freq and cpu_freq.max else None,
            "current_freq_mhz": round(cpu_freq.current, 0) if cpu_freq and cpu_freq.current else None,
        },
        "memory": {
            "total_gb": round(vm.total / (1024**3), 2),
            "available_gb": round(vm.available / (1024**3), 2),
        },
        "boot_time": psutil.boot_time(),
    }


def collect_hardware_inventory(hw_engine=None) -> Dict[str, Any]:
    """Detailed component inventory. All probes are best-effort."""
    return {
        "processors": _list_processors(hw_engine),
        "memory_modules": _list_memory_modules(),
        "graphics": _list_graphics(hw_engine),
        "monitors": _list_monitors(),
        "storage": _list_storage_with_smart(),
        "audio": _list_audio_devices(),
        "peripherals": _list_peripherals(),
        "battery": _battery_info(),
        "network": _list_network_adapters(),
        "motherboard": _motherboard_info(),
    }


# ---------------------------------------------------------------------------
# WMI helper
# ---------------------------------------------------------------------------

def _wmi():
    """Get a WMI connection to the default namespace. Returns None on failure."""
    try:
        import wmi
        return wmi.WMI()
    except Exception:
        return None


def _wmi_namespace(namespace: str):
    """Get a WMI connection to a specific namespace (e.g. 'root\\wmi')."""
    try:
        import wmi
        return wmi.WMI(namespace=namespace)
    except Exception:
        return None


def _safe_str(x) -> str:
    if x is None:
        return ""
    return str(x).strip()


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

def _cpu_friendly_name(hw_engine) -> Optional[str]:
    if hw_engine is None:
        return None
    snap = hw_engine.snapshot()
    if snap is None:
        return None
    for hw_name in snap.sensors.keys():
        lower = hw_name.lower()
        if "ryzen" in lower or "intel" in lower or "core" in lower or "xeon" in lower:
            return hw_name
    return None


def _list_processors(hw_engine) -> List[Dict[str, Any]]:
    cpus = []
    name_from_lhm = _cpu_friendly_name(hw_engine)

    c = _wmi()
    if c is not None:
        try:
            for proc in c.Win32_Processor():
                cpus.append({
                    "name": _safe_str(proc.Name) or name_from_lhm or "Unknown",
                    "manufacturer": _safe_str(proc.Manufacturer),
                    "physical_cores": int(proc.NumberOfCores) if proc.NumberOfCores else None,
                    "logical_cores": int(proc.NumberOfLogicalProcessors) if proc.NumberOfLogicalProcessors else None,
                    "max_clock_mhz": int(proc.MaxClockSpeed) if proc.MaxClockSpeed else None,
                    "socket": _safe_str(proc.SocketDesignation),
                    "architecture": _decode_cpu_architecture(proc.Architecture),
                    "l2_cache_kb": int(proc.L2CacheSize) if proc.L2CacheSize else None,
                    "l3_cache_kb": int(proc.L3CacheSize) if proc.L3CacheSize else None,
                    "virtualization_enabled": bool(proc.VirtualizationFirmwareEnabled),
                })
            if cpus:
                return cpus
        except Exception:
            pass

    # Fallback
    return [{
        "name": name_from_lhm or platform.processor() or "Unknown",
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "architecture": platform.machine(),
    }]


def _decode_cpu_architecture(code) -> str:
    table = {0: "x86", 1: "MIPS", 2: "Alpha", 3: "PowerPC", 5: "ARM", 6: "ia64", 9: "x64", 12: "ARM64"}
    return table.get(code, f"Code {code}")


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def _list_memory_modules() -> List[Dict[str, Any]]:
    c = _wmi()
    if c is None:
        vm = psutil.virtual_memory()
        return [{"manufacturer": "Unknown", "capacity_gb": round(vm.total / (1024**3), 2)}]
    try:
        modules = []
        for mem in c.Win32_PhysicalMemory():
            modules.append({
                "manufacturer": _safe_str(mem.Manufacturer) or "Unknown",
                "part_number": _safe_str(mem.PartNumber) or None,
                "serial": _safe_str(mem.SerialNumber) or None,
                "capacity_gb": round(int(mem.Capacity) / (1024**3), 2) if mem.Capacity else None,
                "speed_mhz": int(mem.Speed) if mem.Speed else None,
                "configured_speed_mhz": int(mem.ConfiguredClockSpeed) if mem.ConfiguredClockSpeed else None,
                "form_factor": _decode_form_factor(mem.FormFactor),
                "memory_type": _decode_memory_type(mem.MemoryType, mem.SMBIOSMemoryType),
                "slot": _safe_str(mem.DeviceLocator) or None,
                "voltage_v": (int(mem.ConfiguredVoltage) / 1000.0) if mem.ConfiguredVoltage else None,
            })
        return modules
    except Exception:
        return []


def _decode_form_factor(code) -> str:
    table = {0: "Unknown", 8: "DIMM", 12: "SODIMM", 13: "SRIMM", 17: "FB-DIMM"}
    return table.get(code, f"Code {code}")


def _decode_memory_type(mem_type, smbios_type) -> str:
    smbios_table = {
        0: "Unknown", 20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 30: "LPDDR4",
        34: "DDR5", 35: "LPDDR5",
    }
    if smbios_type in smbios_table:
        return smbios_table[smbios_type]
    return f"Type {mem_type}"


# ---------------------------------------------------------------------------
# Graphics
# ---------------------------------------------------------------------------

def _list_graphics(hw_engine) -> List[Dict[str, Any]]:
    gpus = []
    c = _wmi()
    if c is not None:
        try:
            for vc in c.Win32_VideoController():
                gpus.append({
                    "name": _safe_str(vc.Name) or "Unknown GPU",
                    "manufacturer": _safe_str(vc.AdapterCompatibility),
                    "memory_mb": round(int(vc.AdapterRAM) / (1024**2)) if vc.AdapterRAM else None,
                    "driver_version": _safe_str(vc.DriverVersion) or None,
                    "driver_date": _wmi_date(vc.DriverDate),
                    "video_processor": _safe_str(vc.VideoProcessor),
                    "current_resolution": (
                        f"{vc.CurrentHorizontalResolution}x{vc.CurrentVerticalResolution}"
                        if vc.CurrentHorizontalResolution else None
                    ),
                    "current_refresh_hz": int(vc.CurrentRefreshRate) if vc.CurrentRefreshRate else None,
                })
        except Exception:
            pass
    return gpus or [{"name": "Unknown"}]


def _wmi_date(s) -> Optional[str]:
    """Convert WMI's CIM_DATETIME format to ISO date."""
    if not s:
        return None
    s = str(s)
    if len(s) < 8:
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


# ---------------------------------------------------------------------------
# Monitors (with EDID decoding)
# ---------------------------------------------------------------------------

# A small subset of the official EDID Manufacturer ID list — common ones.
# Full list: https://uefi.org/PNP_ID_List
EDID_MANUFACTURERS = {
    "AAC": "AcerView", "ACI": "Asus", "ACR": "Acer", "AOC": "AOC", "API": "Acer",
    "APP": "Apple", "AUO": "AU Optronics", "BNQ": "BenQ", "BOE": "BOE", "CMN": "Chimei Innolux",
    "CMO": "Chi Mei Optoelectronics", "CPL": "Compal", "CPQ": "Compaq", "CTX": "CTX",
    "DEL": "Dell", "DPC": "Delta", "DTI": "Diamond Touch", "ENC": "Eizo", "EPI": "Envision",
    "FCM": "Funai", "FUS": "Fujitsu", "GSM": "LG (GoldStar)", "GWY": "Gateway", "HEI": "Hyundai",
    "HIQ": "Hyundai ImageQuest", "HPN": "HP", "HSD": "Hannspree", "HTC": "Hitachi", "HWP": "HP",
    "IBM": "IBM", "ICL": "Fujitsu ICL", "IFS": "InFocus", "IQT": "Hyundai", "IVM": "Iiyama",
    "KFC": "KFC", "LCA": "Lacie", "LCD": "Toshiba", "LEN": "Lenovo", "LGD": "LG Display",
    "LPL": "LG Philips", "MAX": "Belinea", "MEI": "Panasonic", "MEL": "Mitsubishi",
    "MS_": "Panasonic", "MSI": "MSI", "NAN": "Nanao", "NEC": "NEC", "NOK": "Nokia",
    "OQI": "Optiquest", "PHL": "Philips", "PIO": "Pioneer", "REL": "Relisys", "SAM": "Samsung",
    "SHP": "Sharp", "SMC": "Samtron", "SMI": "Smile", "SNI": "Siemens Nixdorf", "SNY": "Sony",
    "SRC": "Shamrock", "STN": "Samtron", "TAT": "Tatung", "TOS": "Toshiba", "TSB": "Toshiba",
    "VIZ": "Vizio", "VSC": "ViewSonic", "WDE": "Westinghouse",
}


def _decode_edid_manufacturer(code: str) -> str:
    """Decode the 3-letter EDID code to a friendly manufacturer name."""
    code = (code or "").upper()
    return EDID_MANUFACTURERS.get(code, code or "Unknown")


def _decode_edid_string(uint16_array) -> str:
    """EDID strings are stored as arrays of UInt16 — convert to ASCII."""
    if not uint16_array:
        return ""
    try:
        chars = [chr(int(c)) for c in uint16_array if c and int(c) > 0]
        return "".join(chars).strip("\x00 \t\r\n")
    except Exception:
        return ""


def _list_monitors() -> List[Dict[str, Any]]:
    """Read monitor info from the WMI namespace root\\wmi."""
    monitors = []
    c = _wmi_namespace("root\\wmi")
    if c is None:
        return []

    try:
        for m in c.WmiMonitorID():
            mfg_code = _decode_edid_string(m.ManufacturerName)
            monitors.append({
                "manufacturer": _decode_edid_manufacturer(mfg_code),
                "manufacturer_code": mfg_code,
                "product_code": _decode_edid_string(m.ProductCodeID),
                "user_friendly_name": _decode_edid_string(m.UserFriendlyName),
                "serial": _decode_edid_string(m.SerialNumberID),
                "year_of_manufacture": int(m.YearOfManufacture) if m.YearOfManufacture else None,
                "week_of_manufacture": int(m.WeekOfManufacture) if m.WeekOfManufacture else None,
            })
    except Exception:
        pass

    # Try to add active resolution from main WMI
    c2 = _wmi()
    if c2 is not None:
        try:
            for d in c2.Win32_DesktopMonitor():
                # Match by index — best effort
                if d.ScreenWidth and d.ScreenHeight and len(monitors) > 0:
                    # Just attach to first if available
                    if "resolution" not in monitors[0]:
                        monitors[0]["resolution"] = f"{d.ScreenWidth}x{d.ScreenHeight}"
        except Exception:
            pass

    return monitors


# ---------------------------------------------------------------------------
# Storage with SMART
# ---------------------------------------------------------------------------

def _list_storage_with_smart() -> Dict[str, Any]:
    """Returns physical disks (with SMART status if available) and partitions."""
    physical = []
    partitions = []

    # Physical disks
    c = _wmi()
    if c is not None:
        try:
            for disk in c.Win32_DiskDrive():
                physical.append({
                    "model": _safe_str(disk.Model) or "Unknown",
                    "manufacturer": _safe_str(disk.Manufacturer) if disk.Manufacturer else None,
                    "size_gb": round(int(disk.Size) / (1024**3), 2) if disk.Size else None,
                    "interface": _safe_str(disk.InterfaceType) or None,
                    "media_type": _safe_str(disk.MediaType) or None,
                    "serial": _safe_str(disk.SerialNumber) or None,
                    "firmware_revision": _safe_str(disk.FirmwareRevision) or None,
                    "partitions": int(disk.Partitions) if disk.Partitions else None,
                    "smart_predicted_failure": None,  # filled in below
                    "smart_status": "unknown",
                })
        except Exception:
            pass

    # SMART failure prediction
    smart_c = _wmi_namespace("root\\wmi")
    if smart_c is not None:
        try:
            failures = {}
            for status in smart_c.MSStorageDriver_FailurePredictStatus():
                failures[_safe_str(status.InstanceName)] = bool(status.PredictFailure)

            # InstanceName format: "...PHYSICALDRIVE0_0"
            for disk in physical:
                # Match by model (best-effort)
                disk["smart_status"] = "ok"
                for instance, predict_fail in failures.items():
                    disk["smart_predicted_failure"] = predict_fail
                    disk["smart_status"] = "failing" if predict_fail else "ok"
                    break  # simple 1:1 match for now
        except Exception:
            pass

    # Logical partitions via psutil
    try:
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                partitions.append({
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total_gb": round(usage.total / (1024**3), 2),
                    "used_gb": round(usage.used / (1024**3), 2),
                    "free_gb": round(usage.free / (1024**3), 2),
                    "percent_used": usage.percent,
                })
            except (PermissionError, OSError):
                continue
    except Exception:
        pass

    return {"physical": physical, "partitions": partitions}


# ---------------------------------------------------------------------------
# Audio devices
# ---------------------------------------------------------------------------

def _list_audio_devices() -> List[Dict[str, Any]]:
    """Sound cards, speakers, microphones, headsets."""
    devices = []
    c = _wmi()
    if c is None:
        return []
    try:
        for d in c.Win32_SoundDevice():
            devices.append({
                "name": _safe_str(d.Name) or "Unknown",
                "manufacturer": _safe_str(d.Manufacturer),
                "status": _safe_str(d.Status),
                "category": "Sound device",
            })
        # Add audio endpoints from PnPEntity (more granular: speakers/mic/headphones)
        for d in c.Win32_PnPEntity():
            cls = _safe_str(d.PNPClass)
            if cls in ("AudioEndpoint", "MEDIA"):
                name = _safe_str(d.Name)
                if not name:
                    continue
                # Avoid duplicates
                if any(name == x["name"] for x in devices):
                    continue
                devices.append({
                    "name": name,
                    "manufacturer": _safe_str(d.Manufacturer),
                    "status": _safe_str(d.Status),
                    "category": cls,
                })
    except Exception:
        pass
    return devices


# ---------------------------------------------------------------------------
# Peripherals: mice, keyboards, trackpads
# ---------------------------------------------------------------------------

def _list_peripherals() -> Dict[str, List[Dict[str, Any]]]:
    """Pointing devices (mice & trackpads) and keyboards."""
    pointing = []
    keyboards = []
    cameras = []
    other = []

    c = _wmi()
    if c is None:
        return {"pointing": [], "keyboards": [], "cameras": [], "other": []}

    try:
        for p in c.Win32_PointingDevice():
            name = _safe_str(p.Name) or _safe_str(p.Description) or "Unknown"
            is_trackpad = any(kw in name.lower() for kw in ["touchpad", "trackpad", "synaptics", "elan", "precision"])
            pointing.append({
                "name": name,
                "manufacturer": _safe_str(p.Manufacturer),
                "type": "Trackpad" if is_trackpad else "Mouse",
                "device_id": _safe_str(p.DeviceID),
                "buttons": int(p.NumberOfButtons) if p.NumberOfButtons else None,
            })
    except Exception:
        pass

    try:
        for k in c.Win32_Keyboard():
            keyboards.append({
                "name": _safe_str(k.Name) or _safe_str(k.Description) or "Unknown",
                "manufacturer": _safe_str(k.Manufacturer),
                "device_id": _safe_str(k.DeviceID),
                "layout": _safe_str(k.Layout),
            })
    except Exception:
        pass

    # Cameras and other notable PnP devices
    try:
        for d in c.Win32_PnPEntity():
            cls = _safe_str(d.PNPClass)
            name = _safe_str(d.Name)
            if not name:
                continue
            if cls == "Camera" or "camera" in name.lower() or "webcam" in name.lower():
                cameras.append({
                    "name": name,
                    "manufacturer": _safe_str(d.Manufacturer),
                    "status": _safe_str(d.Status),
                })
            elif cls in ("Biometric", "FingerPrintReader") or "fingerprint" in name.lower():
                other.append({
                    "name": name,
                    "category": "Biometric",
                    "manufacturer": _safe_str(d.Manufacturer),
                })
            elif cls == "Bluetooth" and ("controller" in name.lower() or "adapter" in name.lower()):
                other.append({
                    "name": name,
                    "category": "Bluetooth",
                    "manufacturer": _safe_str(d.Manufacturer),
                })
    except Exception:
        pass

    return {
        "pointing": pointing,
        "keyboards": keyboards,
        "cameras": cameras,
        "other": other,
    }


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

def _battery_info() -> Optional[Dict[str, Any]]:
    """Battery details and wear estimate."""
    c = _wmi()
    if c is None:
        return None

    info = None
    try:
        for b in c.Win32_Battery():
            info = {
                "name": _safe_str(b.Name) or _safe_str(b.DeviceID),
                "manufacturer": _safe_str(b.Manufacturer),
                "chemistry": _decode_battery_chemistry(b.Chemistry),
                "design_voltage_mv": int(b.DesignVoltage) if b.DesignVoltage else None,
                "estimated_charge_pct": int(b.EstimatedChargeRemaining) if b.EstimatedChargeRemaining is not None else None,
                "estimated_runtime_min": int(b.EstimatedRunTime) if b.EstimatedRunTime else None,
                "status": _decode_battery_status(b.BatteryStatus),
            }
            break
    except Exception:
        return None

    if info is None:
        return None

    # Wear calculation from BatteryStaticData and BatteryFullChargedCapacity
    bms = _wmi_namespace("root\\wmi")
    if bms is not None:
        try:
            for static in bms.BatteryStaticData():
                info["design_capacity_mwh"] = int(static.DesignedCapacity) if static.DesignedCapacity else None
                break
            for full in bms.BatteryFullChargedCapacity():
                info["full_charge_capacity_mwh"] = int(full.FullChargedCapacity) if full.FullChargedCapacity else None
                break
            if info.get("design_capacity_mwh") and info.get("full_charge_capacity_mwh"):
                wear = 100.0 * (1 - info["full_charge_capacity_mwh"] / info["design_capacity_mwh"])
                info["wear_percent"] = round(max(0, wear), 1)
                info["health_percent"] = round(100 - max(0, wear), 1)
        except Exception:
            pass

    return info


def _decode_battery_chemistry(code) -> str:
    table = {1: "Other", 2: "Unknown", 3: "Lead Acid", 4: "Nickel Cadmium",
             5: "Nickel Metal Hydride", 6: "Lithium-ion", 7: "Zinc air", 8: "Lithium Polymer"}
    return table.get(code, f"Code {code}")


def _decode_battery_status(code) -> str:
    table = {1: "Discharging", 2: "AC Power", 3: "Fully Charged", 4: "Low",
             5: "Critical", 6: "Charging", 7: "Charging and High",
             8: "Charging and Low", 9: "Charging and Critical",
             10: "Undefined", 11: "Partially Charged"}
    return table.get(code, f"Code {code}")


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def _list_network_adapters() -> List[Dict[str, Any]]:
    adapters = []

    # Live IP info via psutil
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()

    # Hardware info via WMI
    wmi_adapters = {}
    c = _wmi()
    if c is not None:
        try:
            for a in c.Win32_NetworkAdapter():
                if not a.NetEnabled:
                    continue
                wmi_adapters[_safe_str(a.NetConnectionID)] = {
                    "manufacturer": _safe_str(a.Manufacturer),
                    "product": _safe_str(a.ProductName) or _safe_str(a.Name),
                    "mac": _safe_str(a.MACAddress),
                    "adapter_type": _safe_str(a.AdapterType),
                    "speed_bps": int(a.Speed) if a.Speed else None,
                }
        except Exception:
            pass

    for name, addr_list in addrs.items():
        meta = wmi_adapters.get(name, {})
        adapter = {
            "name": name,
            "is_up": stats[name].isup if name in stats else False,
            "speed_mbps": stats[name].speed if name in stats else 0,
            "mac": meta.get("mac"),
            "product": meta.get("product"),
            "manufacturer": meta.get("manufacturer"),
            "adapter_type": meta.get("adapter_type"),
            "addresses": [],
        }
        for a in addr_list:
            family = str(a.family).split(".")[-1]
            if family in ("AF_INET", "AF_INET6"):
                adapter["addresses"].append({"family": family, "address": a.address})
        adapters.append(adapter)
    return adapters


# ---------------------------------------------------------------------------
# Motherboard / BIOS
# ---------------------------------------------------------------------------

def _motherboard_info() -> Dict[str, Any]:
    info = {"manufacturer": "Unknown", "product": "Unknown"}
    c = _wmi()
    if c is None:
        return info

    try:
        for board in c.Win32_BaseBoard():
            info.update({
                "manufacturer": _safe_str(board.Manufacturer) or "Unknown",
                "product": _safe_str(board.Product) or "Unknown",
                "serial": _safe_str(board.SerialNumber) or None,
                "version": _safe_str(board.Version) or None,
            })
            break
    except Exception:
        pass

    try:
        for bios in c.Win32_BIOS():
            info["bios_version"] = _safe_str(bios.SMBIOSBIOSVersion) or None
            info["bios_manufacturer"] = _safe_str(bios.Manufacturer) or None
            info["bios_release_date"] = _wmi_date(bios.ReleaseDate)
            break
    except Exception:
        pass

    try:
        for sys in c.Win32_ComputerSystem():
            info["system_manufacturer"] = _safe_str(sys.Manufacturer) or None
            info["system_model"] = _safe_str(sys.Model) or None
            break
    except Exception:
        pass

    return info