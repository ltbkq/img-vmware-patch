"""Detection of VMware tooling: ``vmrun`` / ``vmware-vdiskmanager`` paths and
versions.

Detection is a pure path lookup (``os.path.isfile`` then ``shutil.which``);
nothing is executed by :func:`find_vmrun`/:func:`find_vdiskmanager`.  The only
functions that run a subprocess (:func:`get_version`, :func:`register_vm`)
accept an injectable ``runner``, which is how the unit tests exercise them
without a real VMware installation.

Search order (see CONTRACT.md):

* Linux/other: ``/usr/bin/vmrun`` then ``which vmrun``.
* Windows: ``C:\\Program Files (x86)\\VMware\\VMware Workstation\\vmrun.exe``
  and the usual Workstation/Player locations, then ``which vmrun``.

``link`` and ``info`` never import-time or run-time depend on this module;
they work on machines without VMware.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from typing import Callable, Optional, Sequence, Tuple

__all__ = [
    "LINUX_VMRUN_PATHS",
    "WINDOWS_VMRUN_PATHS",
    "LINUX_VDISK_PATHS",
    "WINDOWS_VDISK_PATHS",
    "default_candidates",
    "find_tool",
    "find_vmrun",
    "find_vdiskmanager",
    "get_version",
    "register_vm",
    "install_hint",
]

#: Linux lookup order for vmrun: fixed path first, then ``which vmrun``.
LINUX_VMRUN_PATHS: Tuple[str, ...] = ("/usr/bin/vmrun",)
#: Windows lookup order for vmrun.
WINDOWS_VMRUN_PATHS: Tuple[str, ...] = (
    r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
    r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
    r"C:\Program Files (x86)\VMware\VMware Player\vmrun.exe",
    r"C:\Program Files\VMware\VMware Player\vmrun.exe",
)
#: Linux lookup order for vmware-vdiskmanager.
LINUX_VDISK_PATHS: Tuple[str, ...] = ("/usr/bin/vmware-vdiskmanager",)
#: Windows lookup order for vmware-vdiskmanager.
WINDOWS_VDISK_PATHS: Tuple[str, ...] = (
    r"C:\Program Files (x86)\VMware\VMware Workstation\vmware-vdiskmanager.exe",
    r"C:\Program Files\VMware\VMware Workstation\vmware-vdiskmanager.exe",
)

_VERSION_RE = re.compile(r"(?<![\d.])(\d+\.\d+\.\d+(?:[A-Za-z0-9.\-]*[A-Za-z0-9])?)(?![\d.])")

_Runner = Callable[..., object]


def _is_windows(system: str) -> bool:
    return system.lower().startswith("win")


def default_candidates(system: Optional[str] = None, tool: str = "vmrun") -> Tuple[str, ...]:
    """Return the hard-coded candidate paths for ``tool`` on ``system``."""
    system = system if system is not None else platform.system()
    windows = _is_windows(system)
    if tool.startswith("vmware-vdiskmanager"):
        return WINDOWS_VDISK_PATHS if windows else LINUX_VDISK_PATHS
    return WINDOWS_VMRUN_PATHS if windows else LINUX_VMRUN_PATHS


def find_tool(
    tool: str,
    *,
    system: Optional[str] = None,
    candidates: Optional[Sequence[str]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Locate ``tool`` without executing anything.

    Args:
        tool: executable name understood by ``shutil.which`` (``vmrun``).
        system: platform name, defaults to :func:`platform.system`.
        candidates: explicit path list, defaults to :func:`default_candidates`.
        which: substitute for :func:`shutil.which` (tests inject fakes).

    Returns:
        The first existing candidate, otherwise the ``which`` result,
        otherwise ``None``.
    """
    if candidates is None:
        candidates = default_candidates(system, tool)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    which_fn = shutil.which if which is None else which
    found = which_fn(tool)
    return found or None


def find_vmrun(
    *,
    system: Optional[str] = None,
    candidates: Optional[Sequence[str]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Locate ``vmrun`` (never executes it)."""
    return find_tool("vmrun", system=system, candidates=candidates, which=which)


def find_vdiskmanager(
    *,
    system: Optional[str] = None,
    candidates: Optional[Sequence[str]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Locate ``vmware-vdiskmanager`` (never executes it)."""
    return find_tool(
        "vmware-vdiskmanager", system=system, candidates=candidates, which=which
    )


def _first_version(text: str) -> Optional[str]:
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def get_version(tool_path: str, *, runner: Optional[_Runner] = None) -> Optional[str]:
    """Best-effort version probe: run ``tool_path`` and parse its banner.

    ``runner`` must behave like :func:`subprocess.run`; tests inject fakes so
    that no real ``vmrun`` is ever started.
    """
    run = subprocess.run if runner is None else runner
    try:
        proc = run([tool_path], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = getattr(proc, "stdout", None) or ""
    stderr = getattr(proc, "stderr", None) or ""
    return _first_version("{0}\n{1}".format(stdout, stderr))


def register_vm(
    vmx_path: object,
    *,
    vmrun: Optional[str] = None,
    runner: Optional[_Runner] = None,
    timeout: int = 60,
) -> Tuple[bool, str]:
    """Run ``vmrun -T ws register <vmx>``.

    Returns ``(ok, detail)``; failures are reported, never raised, because the
    contract makes registration failures non-fatal (exit code stays 0).
    """
    run = subprocess.run if runner is None else runner
    if vmrun is None:
        vmrun = find_vmrun()
    if not vmrun:
        return False, "vmrun not found"
    command = [vmrun, "-T", "ws", "register", os.fspath(vmx_path)]
    try:
        proc = run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "vmrun timed out after {0}s".format(timeout)
    except OSError as exc:
        return False, str(exc)
    except subprocess.SubprocessError as exc:
        return False, str(exc)

    returncode = getattr(proc, "returncode", 1)
    if returncode == 0:
        return True, (getattr(proc, "stdout", None) or "").strip()
    detail = (getattr(proc, "stderr", None) or "").strip()
    if not detail:
        detail = (getattr(proc, "stdout", None) or "").strip()
    if not detail:
        detail = "vmrun exited with status {0}".format(returncode)
    return False, detail


def install_hint(system: Optional[str] = None) -> str:
    """Multi-line installation hint printed when ``create`` finds no vmrun."""
    system = system if system is not None else platform.system()
    lines = ["hint: install VMware Workstation/Player/Fusion, or put vmrun on PATH:"]
    if _is_windows(system):
        lines.append("      {0}".format(WINDOWS_VMRUN_PATHS[0]))
        lines.append("      then check with `where vmrun`")
    else:
        lines.append("      /usr/bin/vmrun   (VMware Workstation package)")
        lines.append("      then check with `which vmrun`")
    lines.append("      note: link/info never require VMware, only create does.")
    return "\n".join(lines)
