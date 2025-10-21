from __future__ import annotations
import json
import platform
import time
from typing import Any, Dict, Optional
import os

# -*- coding: utf-8 -*-
"""
Windows CPU information collector.

Collects CPU brand/vendor from registry, core counts, frequencies, usage,
times, and stats. Uses psutil when available, with graceful fallbacks.

Intended for Windows only.
"""



try:
    import psutil  # type: ignore
    HAS_PSUTIL = True
except Exception:
    psutil = None  # type: ignore
    HAS_PSUTIL = False

try:
    import winreg  # type: ignore
    HAS_WINREG = True
except Exception:
    HAS_WINREG = False


def _read_registry_cpu_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "brand": None,
        "vendor": None,
        "identifier": None,
        "base_mhz": None,
        "feature_set": None,
    }
    if not HAS_WINREG:
        return info

    # Registry path for CPU info
    path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"

    # Try 64-bit view first (if running 32-bit Python on 64-bit Windows)
    views = []
    try:
        views.append(winreg.KEY_READ | winreg.KEY_WOW64_64KEY)  # type: ignore[attr-defined]
    except Exception:
        pass
    views.append(winreg.KEY_READ)

    for access in views:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, access) as k:  # type: ignore
                def _get(name: str) -> Optional[Any]:
                    try:
                        val, _ = winreg.QueryValueEx(k, name)  # type: ignore
                        return val
                    except FileNotFoundError:
                        return None
                    except Exception:
                        return None

                info["brand"] = _get("ProcessorNameString") or info["brand"]
                info["vendor"] = _get("VendorIdentifier") or info["vendor"]
                info["identifier"] = _get("Identifier") or info["identifier"]
                # "~MHz" is REG_DWORD indicating the (approx) base clock
                base = _get("~MHz")
                if isinstance(base, int):
                    info["base_mhz"] = float(base)
                fs = _get("FeatureSet")
                if isinstance(fs, int):
                    info["feature_set"] = fs
                break
        except FileNotFoundError:
            continue
        except Exception:
            continue

    return info


def _safe_cpu_count(logical: bool = True) -> Optional[int]:
    try:
        if HAS_PSUTIL:
            return psutil.cpu_count(logical=logical)
        # Fallback: os.cpu_count is logical only
        c = os.cpu_count()
        if logical:
            return c
        # Physical cores best-effort fallback (None when unknown)
        return None
    except Exception:
        return None


def _collect_freq() -> Dict[str, Optional[float]]:
    cur = minf = maxf = None
    if HAS_PSUTIL:
        try:
            f = psutil.cpu_freq()
            if f is not None:
                cur = float(f.current) if f.current is not None else None
                minf = float(f.min) if f.min is not None and f.min > 0 else None
                maxf = float(f.max) if f.max is not None and f.max > 0 else None
        except Exception:
            pass
    base = _read_registry_cpu_info().get("base_mhz")
    # Prefer psutil's max if available; else use registry base as "base"
    return {
        "current_mhz": cur,
        "min_mhz": minf,
        "max_mhz": maxf,
        "base_mhz": base if isinstance(base, (int, float)) else None,
    }


def _collect_usage(sample_interval: float = 0.1) -> Dict[str, Any]:
    usage: Dict[str, Any] = {
        "total_percent": None,
        "per_cpu_percent": None,
    }
    if not HAS_PSUTIL:
        return usage
    try:
        # Blocking sample to get stable numbers; set interval=0 for instantaneous
        per_cpu = psutil.cpu_percent(interval=sample_interval, percpu=True)
        total = sum(per_cpu) / len(per_cpu) if per_cpu else psutil.cpu_percent(interval=None)
        usage["per_cpu_percent"] = per_cpu
        usage["total_percent"] = total
    except Exception:
        try:
            usage["total_percent"] = psutil.cpu_percent(interval=None)
        except Exception:
            pass
    return usage


def _collect_times() -> Dict[str, Any]:
    times: Dict[str, Any] = {"total": None, "per_cpu": None}
    if not HAS_PSUTIL:
        return times
    try:
        t = psutil.cpu_times()
        times["total"] = {
            "user": getattr(t, "user", None),
            "system": getattr(t, "system", None),
            "idle": getattr(t, "idle", None),
            "nice": getattr(t, "nice", None),
            "iowait": getattr(t, "iowait", None),
            "irq": getattr(t, "irq", None),
            "softirq": getattr(t, "softirq", None),
            "steal": getattr(t, "steal", None),
            "guest": getattr(t, "guest", None),
            "guest_nice": getattr(t, "guest_nice", None),
        }
    except Exception:
        pass
    try:
        per = psutil.cpu_times(percpu=True)
        times["per_cpu"] = [
            {
                "user": getattr(x, "user", None),
                "system": getattr(x, "system", None),
                "idle": getattr(x, "idle", None),
                "nice": getattr(x, "nice", None),
                "iowait": getattr(x, "iowait", None),
                "irq": getattr(x, "irq", None),
                "softirq": getattr(x, "softirq", None),
                "steal": getattr(x, "steal", None),
                "guest": getattr(x, "guest", None),
                "guest_nice": getattr(x, "guest_nice", None),
            }
            for x in per
        ]
    except Exception:
        pass
    return times


def _collect_stats() -> Dict[str, Optional[int]]:
    stats: Dict[str, Optional[int]] = {
        "ctx_switches": None,
        "interrupts": None,
        "software_interrupts": None,
        "syscalls": None,
        "dpcs": None,  # Windows-specific (available as interrupts+dpcs via psutil on Windows)
    }
    if not HAS_PSUTIL:
        return stats
    try:
        s = psutil.cpu_stats()
        stats["ctx_switches"] = getattr(s, "ctx_switches", None)
        stats["interrupts"] = getattr(s, "interrupts", None)
        stats["software_interrupts"] = getattr(s, "soft_interrupts", None)
        stats["syscalls"] = getattr(s, "syscalls", None)
        # psutil on Windows exposes dctx? Not directly; keep None for portability
    except Exception:
        pass
    return stats


def collect_cpu_info(sample_interval: float = 0.1) -> Dict[str, Any]:
    """
    Collect CPU information on Windows.

    sample_interval: seconds to block while sampling CPU percent (set 0 for non-blocking).
    """
    reg = _read_registry_cpu_info()
    arch = platform.machine()
    proc = platform.processor()

    info: Dict[str, Any] = {
        "timestamp": time.time(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "architecture": arch,
            "processor_string": proc or reg.get("identifier"),
        },
        "brand": reg.get("brand"),
        "vendor": reg.get("vendor"),
        "identifier": reg.get("identifier"),
        "feature_set_mask": reg.get("feature_set"),
        "cores": {
            "physical": _safe_cpu_count(logical=False),
            "logical": _safe_cpu_count(logical=True),
        },
        "frequency": _collect_freq(),
        "usage_percent": _collect_usage(sample_interval=sample_interval),
        "times": _collect_times(),
        "stats": _collect_stats(),
    }
    return info


def main() -> None:
    # Default sample interval 0.1s for stable numbers; override with env or args if needed.
    data = collect_cpu_info(sample_interval=0.1)
    print(json.dumps(data, indent=2, sort_keys=False, default=lambda o: o))


if __name__ == "__main__":
    main()