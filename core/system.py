"""Live hardware telemetry for the HUD widgets (psutil)."""

from __future__ import annotations

import logging
import os
import platform
import socket
import sys
import threading
import time

import psutil

log = logging.getLogger("jarvis.system")


def _cpu_name() -> str:
    try:
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        if sys.platform == "darwin":
            import subprocess

            out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=2)
            if out.stdout.strip():
                return out.stdout.strip()
    except Exception:
        pass
    return platform.processor() or platform.machine() or "Unknown CPU"


def _system_drive() -> str:
    if sys.platform == "win32":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


class SystemMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        psutil.cpu_percent(percpu=True)  # prime the counters; the first reading is otherwise 0
        self._net = psutil.net_io_counters()
        self._net_t = time.monotonic()
        self._static: dict | None = None

    def static_info(self) -> dict:
        if self._static is None:
            vm = psutil.virtual_memory()
            self._static = {
                "hostname": socket.gethostname(),
                "os": f"{platform.system()} {platform.release()}",
                "cpu_name": _cpu_name(),
                "cores_physical": psutil.cpu_count(logical=False) or 0,
                "cores_logical": psutil.cpu_count(logical=True) or 0,
                "ram_total_gb": round(vm.total / 1024**3, 1),
                "python": platform.python_version(),
            }
        return self._static

    def snapshot(self) -> dict:
        with self._lock:
            per_core = psutil.cpu_percent(percpu=True)
            cpu = sum(per_core) / len(per_core) if per_core else psutil.cpu_percent()
            vm = psutil.virtual_memory()

            now = time.monotonic()
            net = psutil.net_io_counters()
            dt = max(now - self._net_t, 1e-3)
            up = max(0, net.bytes_sent - self._net.bytes_sent) / dt if net and self._net else 0.0
            down = max(0, net.bytes_recv - self._net.bytes_recv) / dt if net and self._net else 0.0
            self._net, self._net_t = net, now

        data = {
            "cpu": round(cpu, 1),
            "per_core": [round(c) for c in per_core][:32],
            "ram": round(vm.percent, 1),
            "ram_used_gb": round((vm.total - vm.available) / 1024**3, 1),
            "ram_total_gb": round(vm.total / 1024**3, 1),
            "net_up_kbps": round(up / 1024, 1),
            "net_down_kbps": round(down / 1024, 1),
            "uptime_s": int(time.time() - psutil.boot_time()),
            "processes": len(psutil.pids()),
            "disk": None,
            "battery": None,
            "cpu_freq_ghz": None,
        }
        try:
            data["disk"] = round(psutil.disk_usage(_system_drive()).percent, 1)
        except Exception:
            pass
        try:
            freq = psutil.cpu_freq()
            if freq and freq.current:
                data["cpu_freq_ghz"] = round(freq.current / 1000, 2)
        except Exception:
            pass
        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                data["battery"] = {"percent": round(bat.percent), "plugged": bool(bat.power_plugged)}
        except Exception:
            pass
        return data
