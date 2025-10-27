import os
import re
import socket
import platform
import subprocess
import psutil

class Linux:
    def __init__(self):
        pass
    def gather_info(self) -> dict:
        try:
            import psutil
        except Exception:
            psutil = None

        # hostname
        hostname = platform.node() or ""

        # ip (best-effort, non-blocking)
        ip = "127.0.0.1"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            try:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            try:
                ip = socket.gethostbyname(hostname)
            except Exception:
                ip = "127.0.0.1"

        # cpu usage & frequency
        cpu_usage = None
        cpu_frequency = None
        try:
            if psutil:
                cpu_usage = float(psutil.cpu_percent(interval=0.5))
                cpu_freq = psutil.cpu_freq()
                cpu_frequency = float(cpu_freq.current) if cpu_freq and cpu_freq.current else None
            else:
                cpu_usage = 0.0
        except Exception:
            cpu_usage = None
            cpu_frequency = None

        # cpu temperature (best-effort)
        cpu_temperature = None
        try:
            if psutil and hasattr(psutil, "sensors_temperatures"):
                temps = psutil.sensors_temperatures(fahrenheit=False)
                # common keys
                for key in ("coretemp", "cpu_thermal", "k10temp"):
                    if key in temps and temps[key]:
                        cpu_temperature = float(temps[key][0].current)
                        break
                if cpu_temperature is None:
                    # pick first available reading
                    for entries in temps.values():
                        if entries:
                            cpu_temperature = float(entries[0].current)
                            break
            if cpu_temperature is None:
                # fallback to sysfs thermal zones
                tz_base = "/sys/class/thermal"
                if os.path.isdir(tz_base):
                    for name in sorted(os.listdir(tz_base)):
                        if name.startswith("thermal_zone"):
                            p = os.path.join(tz_base, name, "temp")
                            try:
                                with open(p, "r") as fh:
                                    raw = fh.read().strip()
                                    if raw:
                                        val = float(raw)
                                        # many sensors report millidegrees
                                        if val > 1000:
                                            val = val / 1000.0
                                        cpu_temperature = float(val)
                                        break
                            except Exception:
                                continue
        except Exception:
            cpu_temperature = None

        # GPU info (best-effort using nvidia-smi, fallback for model only)
        gpu_usage = None
        gpu_frequency = None
        gpu_temperature = None
        gpu_model = None
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
                # take first GPU line
                first = out.splitlines()[0]
                parts = [p.strip() for p in first.split(",")]
                if len(parts) >= 4:
                    try:
                        gpu_usage = float(parts[0])
                    except Exception:
                        gpu_usage = None
                    try:
                        gpu_frequency = float(parts[1])
                    except Exception:
                        gpu_frequency = None
                    try:
                        gpu_temperature = float(parts[2])
                    except Exception:
                        gpu_temperature = None
                    gpu_model = parts[3] if parts[3] else None
        except Exception:
            # attempt to get model via lspci
            try:
                lspci = subprocess.check_output(["lspci", "-mm"], stderr=subprocess.DEVNULL, timeout=1.0).decode(errors="ignore")
                # look for VGA/3D controller line
                for line in lspci.splitlines():
                    if re.search(r"\b(VGA|3D)\b", line, re.IGNORECASE):
                        # format can be like: "00:02.0" "VGA compatible controller" "Intel Corporation" "HD Graphics 620"
                        parts = [p.strip('" ') for p in line.split("\t") if p.strip()]
                        if parts:
                            gpu_model = parts[-1]
                            break
            except Exception:
                gpu_model = None

        # memory
        memory_usage = None
        memory_max = None
        try:
            if psutil:
                vm = psutil.virtual_memory()
                memory_usage = float(vm.percent)
                memory_max = float(vm.total)
            else:
                memory_usage = None
                memory_max = None
        except Exception:
            memory_usage = None
            memory_max = None

        # disk
        disks = []
        try:
            disk_usage = None
            disk_size = None
            if psutil:
                # enumerate partitions via psutil
                try:
                    parts = psutil.disk_partitions(all=False)
                except Exception:
                    parts = []
                seen = set()
                for part in parts:
                    mp = part.mountpoint
                    if mp in seen:
                        continue
                    seen.add(mp)
                    try:
                        du = psutil.disk_usage(mp)
                        disks.append({
                            "mountpoint": mp,
                            "usage": float(du.percent),
                            "size": int(du.total),
                        })
                    except Exception:
                        continue
                # fallback to root if nothing found
                if not disks:
                    try:
                        du = psutil.disk_usage("/")
                        disks.append({
                            "mountpoint": "/",
                            "usage": float(du.percent),
                            "size": float(du.total),
                        })
                    except Exception:
                        pass
                if disks:
                    disk_usage = disks[0]["usage"]
                    disk_size = disks[0]["size"]
            else:
                # fallback: parse /proc/mounts and use os.statvfs
                try:
                    mounts = set()
                    with open("/proc/mounts", "r") as fh:
                        for line in fh:
                            parts = line.split()
                            if len(parts) >= 2:
                                mounts.add(parts[1])
                    for mp in sorted(mounts):
                        # skip obvious pseudo filesystems
                        if mp.startswith(("/proc", "/sys")):
                            continue
                        try:
                            st = os.statvfs(mp)
                            total = st.f_blocks * st.f_frsize
                            free = st.f_bfree * st.f_frsize
                            used = total - free
                            percent = (used / total) * 100.0 if total > 0 else 0.0
                            disks.append({
                                "mountpoint": mp,
                                "usage": float(percent),
                                "size": float(total),
                            })
                        except Exception:
                            continue
                    if disks:
                        disk_usage = disks[0]["usage"]
                        disk_size = disks[0]["size"]
                except Exception:
                    pass

        except Exception:
            disks = []

        # processes - top 10 by RSS memory (name + pid)
        processes = []
        try:
            if psutil:
                procs = []
                for p in psutil.process_iter(attrs=["name", "pid", "memory_info"]):
                    try:
                        info = p.info
                        rss = info.get("memory_info").rss if info.get("memory_info") else 0
                        name = info.get("name") or f"pid:{info.get('pid')}"
                        procs.append((rss, f"{name} ({info.get('pid')})"))
                    except Exception:
                        continue
                procs.sort(key=lambda x: x[0], reverse=True)
                processes = [p[1] for p in procs[:10]]
        except Exception:
            processes = []

        # OS info from /etc/os-release and platform
        os_name = ""
        os_version = ""
        try:
            if os.path.exists("/etc/os-release"):
                with open("/etc/os-release", "r") as fh:
                    data = fh.read()
                m_name = re.search(r'^NAME="?([^"\n]+)"?', data, flags=re.M)
                m_ver = re.search(r'^VERSION="?([^"\n]+)"?', data, flags=re.M)
                if m_name:
                    os_name = m_name.group(1)
                if m_ver:
                    os_version = m_ver.group(1)
        except Exception:
            os_name = ""
            os_version = ""

        try:
            uname = platform.uname()
            os_kernel = uname.release or ""
            os_architecture = uname.machine or ""
        except Exception:
            os_kernel = ""
            os_architecture = ""

        # cpu model from /proc/cpuinfo
        cpu_model = ""
        try:
            if os.path.exists("/proc/cpuinfo"):
                with open("/proc/cpuinfo", "r") as fh:
                    for line in fh:
                        if line.lower().startswith("model name") or line.lower().startswith("cpu model"):
                            parts = line.split(":", 1)
                            if len(parts) == 2:
                                cpu_model = parts[1].strip()
                                break
                if not cpu_model:
                    # fallback: first "vendor_id" + "model"
                    with open("/proc/cpuinfo", "r") as fh:
                        txt = fh.read()
                    m = re.search(r"model name\s*:\s*(.+)", txt)
                    if m:
                        cpu_model = m.group(1).strip()
        except Exception:
            cpu_model = ""

        return {
            "hostname": hostname,
            "ip": ip,
            "uptime": int(psutil.boot_time()) if psutil else 0,
            "cpu_usage": float(cpu_usage) if cpu_usage is not None else 0.0,
            "cpu_frequency": int(cpu_frequency) if cpu_frequency is not None else 0,
            "gpu_usage": float(gpu_usage) if gpu_usage is not None else 0.0,
            "gpu_frequency": float(gpu_frequency) if gpu_frequency is not None else 0.0,
            "cpu_temperature": float(cpu_temperature) if cpu_temperature is not None else 0.0,
            "gpu_temperature": float(gpu_temperature) if gpu_temperature is not None else 0.0,
            "memory_usage": float(memory_usage) if memory_usage is not None else 0,
            "memory_max": int(memory_max) if memory_max is not None else 0,
            "disks": disks,
            "processes": processes,
            "os_name": os_name,
            "os_version": os_version,
            "os_kernel": os_kernel,
            "os_architecture": os_architecture,
            "cpu_model": cpu_model,
            "gpu_model": gpu_model,
        }