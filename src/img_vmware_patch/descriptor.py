"""Sector/CHS helpers are in :mod:`img_vmware_patch.geometry`; this module
normalises image paths and renders flat VMDK descriptor files.

Descriptor layout (see CONTRACT.md):

.. code-block:: text

    # Disk DescriptorFile
    version=1
    encoding="UTF-8"
    CID=fffffffe
    parentCID=ffffffff
    createType="monolithicFlat"

    RW <total_sectors> FLAT "<absolute image path>" 0

    ddb.virtualHWVersion = "4"
    ddb.adapterType = "<ide|sata|lsilogic>"
    ddb.geometry.cylinders = "<n>"
    ddb.geometry.heads = "<16>"
    ddb.geometry.sectors = "<63>"
"""

from __future__ import annotations

import ntpath
import os
import posixpath
from pathlib import Path
from typing import Optional, Union

from .geometry import Geometry

__all__ = [
    "ADAPTER_TYPES",
    "DEFAULT_ADAPTER",
    "CREATE_TYPE",
    "VIRTUAL_HW_VERSION",
    "OutputError",
    "OutputExistsError",
    "OutputIsDirectoryError",
    "adapter_type",
    "normalize_path",
    "escape_path",
    "extent_line",
    "build_descriptor",
    "write_text_file",
]

#: CLI adapter name -> ``ddb.adapterType`` value.
ADAPTER_TYPES = {
    "ide": "ide",
    "sata": "sata",
    "scsi": "lsilogic",
}
DEFAULT_ADAPTER = "ide"
CREATE_TYPE = "monolithicFlat"
VIRTUAL_HW_VERSION = "4"

_PathLike = Union[str, "os.PathLike[str]"]


class OutputError(Exception):
    """Base class for output-path problems (the CLI maps this to exit code 1)."""

    def __init__(self, path: _PathLike, reason: str) -> None:
        self.path = Path(path)
        self.reason = reason
        super().__init__("{0}: {1}".format(reason, self.path))


class OutputExistsError(OutputError):
    """Raised when the output file exists and ``--force`` was not given."""

    def __init__(self, path: _PathLike) -> None:
        super().__init__(path, "output file already exists")


class OutputIsDirectoryError(OutputError):
    """Raised when the output path is a directory."""

    def __init__(self, path: _PathLike) -> None:
        super().__init__(path, "output path is a directory")


def adapter_type(adapter: str) -> str:
    """Map a CLI adapter name onto its ``ddb.adapterType`` value."""
    try:
        return ADAPTER_TYPES[adapter]
    except KeyError:
        raise ValueError(
            "unsupported adapter {0!r}; expected one of: {1}".format(
                adapter, ", ".join(sorted(ADAPTER_TYPES))
            )
        ) from None


def normalize_path(
    path: _PathLike,
    *,
    base: Optional[str] = None,
    windows: Optional[bool] = None,
) -> str:
    """Return an absolute path that always uses forward slashes.

    Args:
        path: path to normalise; relative paths are resolved against ``base``.
        base: directory used for relative paths (defaults to the cwd).
        windows: force Windows/POSIX semantics; by default they are derived
            from :data:`os.name`.

    Windows paths keep their drive letter (``C:/vm/disk.img``), UNC paths keep
    their ``//server/share`` prefix, ``.``/``..`` are collapsed lexically and
    backslashes become forward slashes.
    """
    text = os.fspath(path)
    if not isinstance(text, str):
        text = os.fsdecode(text)
    if windows is None:
        windows = os.name == "nt"
    if base is None:
        base = os.getcwd()

    if windows:
        if not ntpath.isabs(text):
            text = ntpath.join(base, text)
        return ntpath.normpath(text).replace("\\", "/")

    if not posixpath.isabs(text):
        text = posixpath.join(base, text)
    return posixpath.normpath(text)


def escape_path(path: str) -> str:
    """Escape ``"`` so the path can live inside a quoted extent/descriptor."""
    return path.replace('"', '\\"')


def extent_line(total_sectors: int, image_path: _PathLike, **norm_kwargs: object) -> str:
    """Render the single ``RW ... FLAT "..." 0`` extent line."""
    normalized = normalize_path(image_path, **norm_kwargs)  # type: ignore[arg-type]
    return 'RW {0} FLAT "{1}" 0'.format(int(total_sectors), escape_path(normalized))


def build_descriptor(
    image_path: _PathLike,
    geometry: Geometry,
    *,
    adapter: str = DEFAULT_ADAPTER,
    **norm_kwargs: object,
) -> str:
    """Render a complete monolithicFlat VMDK descriptor file."""
    lines = [
        "# Disk DescriptorFile",
        "version=1",
        'encoding="UTF-8"',
        "CID=fffffffe",
        "parentCID=ffffffff",
        'createType="{0}"'.format(CREATE_TYPE),
        "",
        "# Extent description",
        extent_line(geometry.total_sectors, image_path, **norm_kwargs),
        "",
        "# The Disk Data Base",
        "#DDB",
        "",
        'ddb.virtualHWVersion = "{0}"'.format(VIRTUAL_HW_VERSION),
        'ddb.adapterType = "{0}"'.format(adapter_type(adapter)),
        'ddb.geometry.cylinders = "{0}"'.format(geometry.cylinders),
        'ddb.geometry.heads = "{0}"'.format(geometry.heads),
        'ddb.geometry.sectors = "{0}"'.format(geometry.sectors),
    ]
    return "\n".join(lines) + "\n"


def write_text_file(path: _PathLike, content: str, *, force: bool = False) -> Path:
    """Write ``content`` to ``path`` creating missing parent directories.

    Raises:
        OutputIsDirectoryError: ``path`` is a directory.
        OutputExistsError: ``path`` exists and ``force`` is off.
    """
    target = Path(path)
    if target.is_dir():
        raise OutputIsDirectoryError(target)
    if target.exists() and not force:
        raise OutputExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return target
