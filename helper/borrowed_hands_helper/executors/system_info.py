"""system.info: non-sensitive capability-discovery data only. No serial numbers, hostnames, user
names, paths, network data or uptime. Runs as the owner; needs no approval."""
from __future__ import annotations

import asyncio
import platform
import re
import shutil
from typing import Callable

from .. import caps
from . import AcceptedJob, EmitState, JobOutcome


def _disk_free() -> int:
    for p in ("/System/Volumes/Data", "/"):
        try:
            return shutil.disk_usage(p).free
        except OSError:
            continue
    return 0


def _clip(s: str, n: int, pattern: str = r"[ -~]*") -> str:
    s = s.strip()[:n]
    return s if re.fullmatch(pattern, s) else ""


class SystemInfoExecutor:
    def __init__(self, run: caps.Run = caps.run_cmd, arch: Callable[[], str] = platform.machine,
                 disk_free: Callable[[], int] = _disk_free):
        self._run = run
        self._arch = arch
        self._disk_free = disk_free

    def _collect(self) -> dict:
        def out(argv):
            rc, text = self._run(argv)
            return text if rc == 0 else ""
        try:
            mem = int(out(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip() or 0)
        except ValueError:
            mem = 0
        xc = caps.xcode_info(self._run)
        arch = self._arch()
        return {"os_version": _clip(out(["/usr/bin/sw_vers", "-productVersion"]), 32, r"[0-9.]*"),
                "os_build": _clip(out(["/usr/bin/sw_vers", "-buildVersion"]), 32, r"[0-9A-Za-z]*"),
                "arch": arch if arch in ("arm64", "x86_64") else "arm64",
                "cpu": _clip(out(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]), 100),
                "memory_gb": max(0, mem // 2**30),
                "xcode": {"version": xc[0], "build": xc[1]} if xc else None,
                "disk_free_gb": max(0, int(self._disk_free()) // 2**30)}

    async def run(self, job: AcceptedJob, emit_state: EmitState) -> JobOutcome:
        result = await asyncio.get_running_loop().run_in_executor(None, self._collect)
        return JobOutcome("succeeded", result, None)

    async def cancel(self, job_id: str) -> None:
        return None          # finishes in well under a second; nothing to stop


def make_executor(**kwargs) -> SystemInfoExecutor:
    return SystemInfoExecutor()
