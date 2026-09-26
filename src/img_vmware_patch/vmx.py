"""Minimal ``.vmx`` configuration generation.

Only the keys needed to boot a VM around a flat VMDK descriptor are emitted:

* ``ide`` -> ``ide0:0.*``, ``sata`` -> ``sata0:0.*``,
  ``scsi`` -> ``scsi0:0.*`` plus ``scsi0.virtualDev = "lsilogic"``.
* ``<controller>:0.fileName`` is the bare descriptor file name: the descriptor
  always sits in the same directory as the ``.vmx``.
* ``virtualHW.version = "19"`` (Workstation 17), ``memsize``, ``displayName``
  and ``guestOS`` come from the arguments.
* The device/controller ``*.present`` lines and ``floppy0.present = "FALSE"``
  are what ``create`` adds on top of the bare template
  (``with_present=False`` renders the template without them).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from .descriptor import write_text_file

__all__ = [
    "ADAPTER_CONTROLLER",
    "DEFAULT_GUEST_OS",
    "DEFAULT_RAM_MB",
    "VIRTUAL_HW_VERSION",
    "build_vmx",
    "write_vmx",
]

#: CLI adapter name -> controller key prefix used in the ``.vmx``.
ADAPTER_CONTROLLER = {
    "ide": "ide0",
    "sata": "sata0",
    "scsi": "scsi0",
}
DEFAULT_GUEST_OS = "otherlinux-64"
DEFAULT_RAM_MB = 2048
VIRTUAL_HW_VERSION = "19"

_PathLike = Union[str, "os.PathLike[str]"]


def _quote(value: object) -> str:
    r"""Quote a value for the ``.vmx`` format (backslashes and quotes are escaped)."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def build_vmx(
    *,
    display_name: str,
    descriptor_name: str,
    adapter: str = "ide",
    ram_mb: int = DEFAULT_RAM_MB,
    guest_os: str = DEFAULT_GUEST_OS,
    with_present: bool = True,
) -> str:
    """Render a minimal ``.vmx`` file.

    Args:
        display_name: value of ``displayName`` (the VM name).
        descriptor_name: bare file name of the VMDK descriptor.
        adapter: one of ``ide``/``sata``/``scsi``.
        ram_mb: memory size in MB.
        guest_os: value of ``guestOS``.
        with_present: emit the ``*.present = "TRUE"`` device lines (``create``)
            and ``floppy0.present = "FALSE"``.
    """
    if adapter not in ADAPTER_CONTROLLER:
        raise ValueError(
            "unsupported adapter {0!r}; expected one of: {1}".format(
                adapter, ", ".join(sorted(ADAPTER_CONTROLLER))
            )
        )
    if not isinstance(ram_mb, int) or isinstance(ram_mb, bool) or ram_mb <= 0:
        raise ValueError("ram_mb must be a positive integer, got {0!r}".format(ram_mb))
    if not str(display_name).strip():
        raise ValueError("display_name must not be empty")
    if not str(descriptor_name).strip():
        raise ValueError("descriptor_name must not be empty")

    controller = ADAPTER_CONTROLLER[adapter]
    lines = [
        '.encoding = "UTF-8"',
        'config.version = "8"',
        'displayName = "{0}"'.format(_quote(display_name)),
        'guestOS = "{0}"'.format(_quote(guest_os)),
        'memsize = "{0}"'.format(int(ram_mb)),
        'virtualHW.version = "{0}"'.format(VIRTUAL_HW_VERSION),
        "",
    ]
    if adapter == "scsi":
        lines.append('{0}.virtualDev = "lsilogic"'.format(controller))
    if with_present:
        lines.append('{0}.present = "TRUE"'.format(controller))
        lines.append('{0}:0.present = "TRUE"'.format(controller))
    lines.append('{0}:0.fileName = "{1}"'.format(controller, _quote(descriptor_name)))
    if with_present:
        lines.extend(["", 'floppy0.present = "FALSE"'])
    return "\n".join(lines) + "\n"


def write_vmx(path: _PathLike, content: str, *, force: bool = False) -> Path:
    """Write a ``.vmx`` file (same rules as :func:`write_text_file`)."""
    written = write_text_file(str(path), content, force=force)
    return Path(written)
