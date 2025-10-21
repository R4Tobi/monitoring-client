from __future__ import annotations
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CPU information collector for Debian/Ubuntu-based systems.

- Uses lscpu, /proc/cpuinfo, sysfs cpufreq, and psutil (optional)
- Safe fallbacks if some tools/data are missing
- Can be executed directly to print JSON payload
"""



try:
    import psutil  # type: ignore
except Exception:  # psutil is optional
    psutil = None  # type: ignore


def _run_cmd(cmd: List[str], timeout: float = 1.5) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def _parse_lscpu_json() -> Dict[str, str]:
    """
    Parse `lscpu -J` if available. Returns dict of Field->Value with field colons stripped.
    """
    rc, out, _ = _run_cmd(["lscpu", "-J"])
    data: Dict[str, str] = {}
    if rc != 0 or not out:
        return data
    try:
        payload = json.loads(out)
        # Expected format: {"lscpu": [{"field":"Architecture:", "data":"x86_64"}, ...]}
        for item in payload.get("lscpu", []):
            field = str(item.get("field", "")).strip().rstrip(":")
            value = str(item.get("data", "")).strip()
            if field:
                data[field] = value
    except Exception:
        return {}
    return data


def _parse_lscpu_fallback() -> Dict[str, str]:
    """
    Parse `lscpu` plain text (key: value) if JSON mode is not available.
    """
    rc, out, _ = _run_cmd(["lscpu"])
    data: Dict[str, str] = {}
    if rc != 0 or not out:
        return data
    for line in out.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if key:
                data[key] = val
    return data


def _parse_lscpu() -> Dict[str, str]:
    data = _parse_lscpu_json()
    if data:
        return data
    return _parse_lscpu_fallback()


def _read_proc_cpuinfo() -> Dict[str, Any]:
    """
    Parse /proc/cpuinfo. Returns:
    {
        "processors": [ { ... per-CPU fields ... }, ... ],
        "first": { ... fields from the first processor ... }
    }
    """
    path = Path("/proc/cpuinfo")
    result: Dict[str, Any] = {"processors": [], "first": {}}
    if not path.exists():
        return result

    current: Dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    if current:
                        result["processors"].append(current)
                        current = {}
                    continue
                if ":" in line:
                    key, val = line.split(":", 1)
                    current[key.strip()] = val.strip()
            if current:
                result["processors"].append(current)
    except Exception:
        return result

    if result["processors"]:
        result["first"] = result["processors"][0]
    return result


def _cpu_flags_from_proc(proc_first: Dict[str, str]) -> List[str]:
    flags_line = proc_first.get("flags") or proc_first.get("Features") or ""
    if not flags_line:
        return []
    # flags are space-separated
    return [f for f in flags_line.strip().split() if f]


def _safe_float(val: Union[str, float, int, None]) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        # can include units like "2.30 GHz"
        num = re.findall(r"[-+]?\d*\.?\d+", str(val))
        return float(num[0]) if num else None
    except Exception:
        return None


def _psutil_cpu_counts() -> Tuple[Optional[int], Optional[int]]:
    if psutil is None:
        return None, None
    try:
        logical = psutil.cpu_count(logical=True)
        physical = psutil.cpu_count(logical=False)
        return logical, physical
    except Exception:
        return None, None


def _psutil_cpu_freq() -> Dict[str, Optional[float]]:
    info = {"current_mhz": None, "min_mhz": None, "max_mhz": None}
    if psutil is None:
        return info
    try:
        f = psutil.cpu_freq()
        if f:
            info["current_mhz"] = float(f.current) if f.current else None
            info["min_mhz"] = float(f.min) if f.min else None
            info["max_mhz"] = float(f.max) if f.max else None
    except Exception:
        pass
    return info


def _sysfs_cpufreq_policy() -> Dict[str, Any]:
    """
    Read policyN cpufreq for min/max/current and governor.
    Returns aggregated min/max across policies and distinct governors.
    """
    base = Path("/sys/devices/system/cpu/cpufreq")
    result: Dict[str, Any] = {
        "governors": [],
        "min_mhz": None,
        "max_mhz": None,
        "current_mhz": None,  # best-effort (not always available at policy level)
    }
    if not base.exists():
        # Some systems expose cpufreq under cpu*/cpufreq only
        return _sysfs_cpufreq_cpu()

    governors = set()
    mins: List[float] = []
    maxs: List[float] = []
    currents: List[float] = []

    for policy in sorted(base.glob("policy*")):
        try:
            gov = (policy / "scaling_governor").read_text().strip()
            if gov:
                governors.add(gov)
        except Exception:
            pass
        try:
            min_khz = float((policy / "scaling_min_freq").read_text().strip())
            mins.append(min_khz / 1000.0)
        except Exception:
            pass
        try:
            max_khz = float((policy / "scaling_max_freq").read_text().strip())
            maxs.append(max_khz / 1000.0)
        except Exception:
            pass
        # Try to read current freq from policy (not always present)
        for fname in ("scaling_cur_freq", "cpuinfo_cur_freq"):
            p = policy / fname
            if p.exists():
                try:
                    currents.append(float(p.read_text().strip()) / 1000.0)
                except Exception:
                    pass

    result["governors"] = sorted(governors)
    result["min_mhz"] = min(mins) if mins else None
    result["max_mhz"] = max(maxs) if maxs else None
    result["current_mhz"] = sum(currents) / len(currents) if currents else None
    return result


def _sysfs_cpufreq_cpu() -> Dict[str, Any]:
    """
    Fallback to per-cpu cpufreq directories if policy* doesn't exist.
    """
    base = Path("/sys/devices/system/cpu")
    result: Dict[str, Any] = {"governors": [], "min_mhz": None, "max_mhz": None, "current_mhz": None}
    if not base.exists():
        return result

    governors = set()
    mins: List[float] = []
    maxs: List[float] = []
    currents: List[float] = []

    for cpu in sorted(base.glob("cpu[0-9]*")):
        cpufreq = cpu / "cpufreq"
        if not cpufreq.exists():
            continue
        try:
            gov = (cpufreq / "scaling_governor").read_text().strip()
            if gov:
                governors.add(gov)
        except Exception:
            pass
        try:
            min_khz = float((cpufreq / "scaling_min_freq").read_text().strip())
            mins.append(min_khz / 1000.0)
        except Exception:
            pass
        try:
            max_khz = float((cpufreq / "scaling_max_freq").read_text().strip())
            maxs.append(max_khz / 1000.0)
        except Exception:
            pass
        for fname in ("scaling_cur_freq", "cpuinfo_cur_freq"):
            p = cpufreq / fname
            if p.exists():
                try:
                    currents.append(float(p.read_text().strip()) / 1000.0)
                except Exception:
                    pass

    result["governors"] = sorted(governors)
    result["min_mhz"] = min(mins) if mins else None
    result["max_mhz"] = max(maxs) if maxs else None
    result["current_mhz"] = sum(currents) / len(currents) if currents else None
    return result


def _psutil_temps() -> Dict[str, Any]:
    temps: Dict[str, Any] = {}
    if psutil is None:
        return temps
    try:
        data = psutil.sensors_temperatures(fahrenheit=False)
        # Convert to a simple dict: {label: current_temp}
        for name, entries in data.items():
            for e in entries:
                key = e.label or f"{name}"
                temps[key] = {
                    "current": e.current,
                    "high": e.high,
                    "critical": e.critical,
                }
    except Exception:
        pass
    return temps


def _get_loadavg() -> Dict[str, Optional[float]]:
    try:
        la1, la5, la15 = os.getloadavg()  # type: ignore[attr-defined]
        return {"1min": float(la1), "5min": float(la5), "15min": float(la15)}
    except Exception:
        return {"1min": None, "5min": None, "15min": None}


def _psutil_usage(interval: float = 0.2) -> Dict[str, Any]:
    if psutil is None:
        return {"overall_percent": None, "per_cpu_percent": []}
    try:
        overall = psutil.cpu_percent(interval=interval)
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        return {"overall_percent": overall, "per_cpu_percent": per_cpu}
    except Exception:
        return {"overall_percent": None, "per_cpu_percent": []}


def _detect_hypervisor(lscpu: Dict[str, str]) -> Dict[str, Any]:
    hv_vendor = lscpu.get("Hypervisor vendor") or lscpu.get("Hypervisor vendor (in short)")
    virt_type = lscpu.get("Virtualization type") or lscpu.get("Virtualization")
    is_vm = bool(hv_vendor) or bool(virt_type)
    if not is_vm:
        # best effort: try systemd-detect-virt
        rc, out, _ = _run_cmd(["systemd-detect-virt", "--vm"])
        if rc == 0 and out and out.lower() != "none":
            is_vm = True
            hv_vendor = out.strip()
    return {"hypervisor_vendor": hv_vendor, "virtualization_type": virt_type, "is_virtual_machine": is_vm}


def _extract_caches(lscpu: Dict[str, str]) -> Dict[str, Optional[str]]:
    # lscpu keys: "L1d cache", "L1i cache", "L2 cache", "L3 cache" with values like "32K", "256K", "12M"
    return {
        "L1d": lscpu.get("L1d cache"),
        "L1i": lscpu.get("L1i cache"),
        "L2": lscpu.get("L2 cache"),
        "L3": lscpu.get("L3 cache"),
    }


def _normalize_arch(machine: str, lscpu_arch: Optional[str]) -> str:
    arch = (lscpu_arch or "").strip().lower()
    if arch:
        return arch
    return machine.lower()


def collect_cpu_info() -> Dict[str, Any]:
    lscpu = _parse_lscpu()
    proc = _read_proc_cpuinfo()

    logical_count, physical_count = _psutil_cpu_counts()
    freq_psutil = _psutil_cpu_freq()
    freq_sysfs = _sysfs_cpufreq_policy()
    temps = _psutil_temps()
    loadavg = _get_loadavg()
    usage = _psutil_usage(interval=0.2)
    hyper = _detect_hypervisor(lscpu)
    caches = _extract_caches(lscpu)

    first = proc.get("first", {}) if isinstance(proc, dict) else {}
    flags = _cpu_flags_from_proc(first)

    # Model/vendor info
    vendor = (
        first.get("vendor_id")
        or lscpu.get("Vendor ID")
        or lscpu.get("CPU family")  # unlikely, but keep
        or None
    )
    model_name = first.get("model name") or lscpu.get("Model name") or None
    family = first.get("cpu family") or lscpu.get("CPU family") or None
    model = first.get("model") or lscpu.get("Model") or None
    stepping = first.get("stepping") or lscpu.get("Stepping") or None
    microcode = first.get("microcode") or lscpu.get("Microcode") or None

    sockets = None
    try:
        sockets = int(lscpu.get("Socket(s)", "")) if lscpu.get("Socket(s)") else None
    except Exception:
        sockets = None

    cores_per_socket = None
    try:
        cores_per_socket = int(lscpu.get("Core(s) per socket", "")) if lscpu.get("Core(s) per socket") else None
    except Exception:
        cores_per_socket = None

    threads_per_core = None
    try:
        threads_per_core = int(lscpu.get("Thread(s) per core", "")) if lscpu.get("Thread(s) per core") else None
    except Exception:
        threads_per_core = None

    architecture = _normalize_arch(platform.machine(), lscpu.get("Architecture"))

    # Frequencies: prefer psutil, then sysfs, then lscpu
    current_mhz = freq_psutil.get("current_mhz") or freq_sysfs.get("current_mhz") or _safe_float(lscpu.get("CPU MHz"))
    min_mhz = freq_psutil.get("min_mhz") or freq_sysfs.get("min_mhz") or _safe_float(lscpu.get("CPU min MHz"))
    max_mhz = freq_psutil.get("max_mhz") or freq_sysfs.get("max_mhz") or _safe_float(lscpu.get("CPU max MHz"))

    governors = freq_sysfs.get("governors") or []

    result: Dict[str, Any] = {
        "hostname": platform.node(),
        "architecture": architecture,
        "vendor_id": vendor,
        "model_name": model_name,
        "family": family,
        "model": model,
        "stepping": stepping,
        "microcode": microcode,
        "logical_cores": logical_count,
        "physical_cores": physical_count,
        "sockets": sockets,
        "cores_per_socket": cores_per_socket,
        "threads_per_core": threads_per_core,
        "frequency_mhz": {
            "current": current_mhz,
            "min": min_mhz,
            "max": max_mhz,
        },
        "cpufreq_governors": governors,
        "usage_percent": {
            "overall": usage.get("overall_percent"),
            "per_cpu": usage.get("per_cpu_percent"),
        },
        "load_average": loadavg,
        "caches": caches,
        "flags": flags,
        "temperatures": temps,
        "hypervisor": hyper,
        "sources": {
            "lscpu": bool(lscpu),
            "proc_cpuinfo": bool(proc.get("processors")),
            "psutil": psutil is not None,
            "sysfs_cpufreq": bool(governors) or any(v is not None for v in (min_mhz, max_mhz)),
        },
        "timestamp": int(time.time()),
    }

    return result


def main() -> int:
    info = collect_cpu_info()
    print(json.dumps(info, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())