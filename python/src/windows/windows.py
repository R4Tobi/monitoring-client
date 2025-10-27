import os
import re
import socket
import platform
import subprocess
from typing import Optional, List, Dict, Any

try:
    import psutil
except Exception:
    psutil = None

try:
    import GPUtil
except Exception:
    GPUtil = None

try:
    import wmi
except Exception:
    wmi = None


class Windows:
    def __init__(self):
        # keep a WMI client if available to reduce repeated init cost
        self._wmi_client = None
        if wmi:
            try:
                self._wmi_client = wmi.WMI()
            except Exception:
                self._wmi_client = None

    def _get_ip(self, hostname: str) -> str:
        ip = "127.0.0.1"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            try:
                # doesn't actually send packets but resolves outgoing iface
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            try:
                ip = socket.gethostbyname(hostname)
            except Exception:
                ip = "127.0.0.1"
        return ip

    def _get_cpu_model(self) -> str:
        try:
            # prefer WMI if available (gives friendly CPU name)
            if self._wmi_client:
                try:
                    procs = self._wmi_client.Win32_Processor()
                    if procs:
                        name = getattr(procs[0], "Name", None)
                        if name:
                            return str(name)
                except Exception:
                    pass
            # fallback to platform
            proc = platform.processor()
            if proc:
                return proc
            uname_proc = getattr(platform.uname(), "processor", None)
            if uname_proc:
                return uname_proc
        except Exception:
            pass
        return ""

    def _get_cpu_temperature(self) -> Optional[float]:
        # best-effort: psutil sensors, then WMI (MSAcpi_ThermalZoneTemperature)
        try:
            if psutil and hasattr(psutil, "sensors_temperatures"):
                temps = psutil.sensors_temperatures(fahrenheit=False)
                if temps:
                    # common keys on Windows may be None, try any reading
                    for entries in temps.values():
                        if entries:
                            try:
                                val = entries[0].current
                                if val is not None:
                                    return float(val)
                            except Exception:
                                continue
        except Exception:
            pass

        if self._wmi_client:
            try:
                # namespace root\WMI MSAcpi_ThermalZoneTemperature (tenths of Kelvin)
                c = wmi.WMI(namespace="root\\OpenHardwareMonitor")
                temperature_infos = c.Sensor()
                for sensor in temperature_infos:
                    if sensor.SensorType==u'Temperature':
                        temp_c = float(sensor.Value)
                        print(temp_c)
            except Exception:
                pass

        return None

    def _get_gpu_via_nvidia_smi(self) -> Dict[str, Optional[float or str]]:
        info = {"usage": None, "frequency": None, "temperature": None, "model": None}
        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,clocks.current.graphics,temperature.gpu,name",
                    "--format=csv,noheader,nounits",
                ],
                stderr=subprocess.DEVNULL,
                timeout=1.0,
            ).decode(errors="ignore").strip()
            if out:
                first = out.splitlines()[0]
                parts = [p.strip() for p in first.split(",")]
                if len(parts) >= 4:
                    try:
                        info["usage"] = float(parts[0])
                    except Exception:
                        info["usage"] = None
                    try:
                        info["frequency"] = float(parts[1])
                    except Exception:
                        info["frequency"] = None
                    try:
                        info["temperature"] = float(parts[2])
                    except Exception:
                        info["temperature"] = None
                    info["model"] = parts[3] if parts[3] else None
        except Exception:
            pass
        return info

    def _get_gpu_via_gputil(self) -> Dict[str, Optional[float or str]]:
        info = {"usage": None, "frequency": None, "temperature": None, "model": None}
        if GPUtil:
            try:
                gpus = GPUtil.getGPUs()
                if gpus:
                    g = gpus[0]
                    info["usage"] = float(g.load * 100.0) if getattr(g, "load", None) is not None else None
                    info["temperature"] = float(g.temperature) if getattr(g, "temperature", None) is not None else None
                    info["model"] = getattr(g, "name", None)
                    freq = getattr(g, "clock", None) or getattr(g, "memoryClock", None)
                    if freq is not None:
                        info["frequency"] = float(freq)
            except Exception:
                pass
        return info

    def _get_gpu_via_wmi(self) -> Dict[str, Optional[float or str]]:
        info = {"usage": None, "frequency": None, "temperature": None, "model": None}
        if self._wmi_client:
            try:
                controllers = self._wmi_client.Win32_VideoController()
                if controllers:
                    name = getattr(controllers[0], "Name", None)
                    info["model"] = str(name) if name else None
            except Exception:
                pass
        return info

    def gather_info(self) -> Dict[str, Any]:
        # hostname
        hostname = platform.node() or ""

        # ip
        ip = self._get_ip(hostname)

        # cpu usage & frequency
        cpu_usage: Optional[float] = None
        cpu_frequency: Optional[float] = None
        try:
            if psutil:
                cpu_usage = float(psutil.cpu_percent(interval=0.5))
                cpu_freq = psutil.cpu_freq()
                cpu_frequency = float(cpu_freq.current) if cpu_freq and cpu_freq.current else None
            else:
                cpu_usage = None
        except Exception:
            cpu_usage = None
            cpu_frequency = None

        # cpu temperature
        cpu_temperature = self._get_cpu_temperature()

        # GPU info - try nvidia-smi, then GPUtil, then WMI model-only fallback
        gpu_info = {"usage": None, "frequency": None, "temperature": None, "model": None}
        try:
            nv = self._get_gpu_via_nvidia_smi()
            if any(v is not None for v in nv.values()):
                gpu_info = nv
            else:
                gu = self._get_gpu_via_gputil()
                if any(v is not None for v in gu.values()):
                    gpu_info = gu
                else:
                    wi = self._get_gpu_via_wmi()
                    if any(v is not None for v in wi.values()):
                        gpu_info = wi
        except Exception:
            pass

        gpu_usage = gpu_info.get("usage")
        gpu_frequency = gpu_info.get("frequency")
        gpu_temperature = gpu_info.get("temperature")
        gpu_model = gpu_info.get("model")

        # memory
        memory_usage: Optional[float] = None
        memory_max: Optional[float] = None
        try:
            if psutil:
                vm = psutil.virtual_memory()
                memory_usage = float(vm.percent)
                memory_max = float(vm.total)
        except Exception:
            memory_usage = None
            memory_max = None

        # disk (system drive, typically C:\)
        disks = []
        try:
            partition = "C:\\"
            if psutil:
                du = psutil.disk_usage(partition)
                disks.append({
                    "path": partition,
                    "usage": float(du.percent),
                    "size": int(du.total)
                })
            # add other mounted partitions (skip the system drive already added)    
            try:
                seen = {os.path.normcase(os.path.normpath(partition))}
                if psutil:
                    for p in psutil.disk_partitions(all=False):
                        try:
                            mp = p.mountpoint
                            if not mp:
                                continue
                            norm = os.path.normcase(os.path.normpath(mp))
                            if norm in seen:
                                continue
                            # skip UNC/network mounts on Windows
                            if norm.startswith(r"\\"):
                                continue
                            du = psutil.disk_usage(mp)
                            disks.append({
                                "path": mp,
                                "usage": float(du.percent),
                                "size": float(du.total)
                            })
                            seen.add(norm)
                        except Exception:
                            continue
            except Exception:
                pass
            
        except Exception:
            try:
                if psutil:
                    du = psutil.disk_usage("/")
                    disks.append({
                        "path": "/",
                        "usage": float(du.percent),
                        "size": float(du.total)
                    })
            except Exception:
                disks = []

        # processes - top 10 by RSS memory (name + pid)
        processes: List[str] = []
        try:
            if psutil:
                procs = []
                for p in psutil.process_iter(attrs=["name", "pid", "memory_info"]):
                    try:
                        info = p.info
                        meminfo = info.get("memory_info")
                        rss = meminfo.rss if meminfo else 0
                        name = info.get("name") or f"pid:{info.get('pid')}"
                        procs.append((rss, f"{name} ({info.get('pid')})"))
                    except Exception:
                        continue
                procs.sort(key=lambda x: x[0], reverse=True)
                processes = [p[1] for p in procs[:10]]
        except Exception:
            processes = []

        # OS info - try WMI for friendly name, fallback to platform
        os_name = ""
        os_version = ""
        os_kernel = ""
        os_architecture = ""
        try:
            if self._wmi_client:
                try:
                    os_items = self._wmi_client.Win32_OperatingSystem()
                    if os_items:
                        os_name = getattr(os_items[0], "Caption", "") or ""
                        os_version = getattr(os_items[0], "Version", "") or ""
                except Exception:
                    pass
        except Exception:
            pass
        try:
            uname = platform.uname()
            os_kernel = uname.release or ""
            os_architecture = uname.machine or ""
            if not os_name:
                os_name = platform.system() or ""
            if not os_version:
                os_version = platform.version() or ""
        except Exception:
            os_kernel = ""
            os_architecture = ""

        # cpu model
        cpu_model = self._get_cpu_model()

        res =  {
            "hostname": hostname,
            "ip": ip,
            "uptime": int(psutil.boot_time()) if psutil else None,
            "cpu_usage": float(cpu_usage) if cpu_usage is not None else None,
            "cpu_frequency": float(cpu_frequency) if cpu_frequency is not None else None,
            "gpu_usage": float(gpu_usage) if gpu_usage is not None else None,
            "gpu_frequency": float(gpu_frequency) if gpu_frequency is not None else None,
            "cpu_temperature": float(cpu_temperature) if cpu_temperature is not None else None,
            "gpu_temperature": float(gpu_temperature) if gpu_temperature is not None else None,
            "memory_usage": float(memory_usage) if memory_usage is not None else None,
            "memory_max": int(memory_max) if memory_max is not None else None,
            "disks": disks,
            "processes": processes,
            "os_name": os_name,
            "os_version": os_version,
            "os_kernel": os_kernel,
            "os_architecture": os_architecture,
            "cpu_model": cpu_model,
            "gpu_model": gpu_model,
        }
        return res