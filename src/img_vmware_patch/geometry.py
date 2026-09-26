"""Pure geometry computation: raw image size -> virtual CHS geometry.

This module is intentionally free of I/O: it never opens files, never writes
to stdout/stderr and never exits the process.  Callers (``cli.py``) decide how
to report the :class:`GeometryError` subclasses raised here.

Rules (see CONTRACT.md):

* ``total_sectors = file_size // 512``; a size that is not a multiple of 512
  raises :class:`ImageAlignmentError` unless ``force=True``, in which case the
  size is truncated down to the 512-byte boundary (the caller should warn).
* ``heads=16``, ``sectors=63``, ``cylinders = total_sectors // (16 * 63)``.
  The remainder sectors are carried by a trailing incomplete cylinder and stay
  part of the extent; ``cylinders`` counts full cylinders only.
* Fewer than one full cylinder means the image is too small
  (:class:`ImageTooSmallError`).
"""

from __future__ import annotations

import dataclasses
from typing import Optional, Tuple

__all__ = [
    "SECTOR_SIZE",
    "HEADS",
    "SECTORS",
    "CYLINDER_SECTORS",
    "Geometry",
    "GeometryError",
    "ImageAlignmentError",
    "ImageTooSmallError",
    "compute_geometry",
]

#: Bytes per logical sector.
SECTOR_SIZE = 512
#: Heads per cylinder (fixed by the contract).
HEADS = 16
#: Sectors per track (fixed by the contract).
SECTORS = 63
#: Sectors per full cylinder: 16 * 63 = 1008.
CYLINDER_SECTORS = HEADS * SECTORS


class GeometryError(ValueError):
    """Base class for geometry problems (the CLI maps this to exit code 1)."""


class ImageAlignmentError(GeometryError):
    """Raised when the image size is not a multiple of 512 bytes."""

    def __init__(self, file_size: int) -> None:
        self.file_size = int(file_size)
        super().__init__(
            "file size {0} is not a multiple of {1} bytes "
            "(镜像未按 512 字节对齐)".format(self.file_size, SECTOR_SIZE)
        )


class ImageTooSmallError(GeometryError):
    """Raised when the image cannot fill even one full cylinder."""

    def __init__(self, total_sectors: int, file_size: int) -> None:
        self.total_sectors = int(total_sectors)
        self.file_size = int(file_size)
        super().__init__(
            "image too small (镜像太小): {0} bytes = {1} sectors, but one cylinder "
            "needs {2} sectors ({3} B x {4} heads x {5} sectors)".format(
                self.file_size,
                self.total_sectors,
                CYLINDER_SECTORS,
                SECTOR_SIZE,
                HEADS,
                SECTORS,
            )
        )


@dataclasses.dataclass(frozen=True)
class Geometry:
    """Virtual disk geometry derived from an image size.

    Attributes:
        file_size: effective size in bytes (always a multiple of 512).
        total_sectors: ``file_size // 512``; value used by the extent line.
        cylinders: number of *full* cylinders.
        heads: heads per cylinder (16).
        sectors: sectors per track (63).
        original_size: size as found on disk, when it differs from
            ``file_size`` because ``--force`` truncated it.
    """

    file_size: int
    total_sectors: int
    cylinders: int
    heads: int = HEADS
    sectors: int = SECTORS
    original_size: Optional[int] = None

    @property
    def cylinder_sectors(self) -> int:
        """Sectors in one full cylinder."""
        return self.heads * self.sectors

    @property
    def source_size(self) -> int:
        """Size before any ``--force`` truncation."""
        return self.file_size if self.original_size is None else self.original_size

    @property
    def truncated(self) -> bool:
        """Whether ``--force`` had to truncate a non aligned image."""
        return self.source_size != self.file_size

    @property
    def remainder_sectors(self) -> int:
        """Sectors beyond the last full cylinder (incomplete cylinder)."""
        return self.total_sectors - self.cylinders * self.cylinder_sectors

    @property
    def chs(self) -> Tuple[int, int, int]:
        """``(cylinders, heads, sectors)``."""
        return (self.cylinders, self.heads, self.sectors)

    @property
    def chs_string(self) -> str:
        """``cylinders/heads/sectors``."""
        return "{0}/{1}/{2}".format(self.cylinders, self.heads, self.sectors)

    def describe(self) -> str:
        """Human readable one-liner used by the CLI."""
        return "{0} cylinders x {1} heads x {2} sectors".format(
            self.cylinders, self.heads, self.sectors
        )


def compute_geometry(file_size: int, *, force: bool = False) -> Geometry:
    """Turn a raw byte size into a :class:`Geometry`.

    Args:
        file_size: image size in bytes.
        force: truncate a non 512-aligned size down to the boundary instead of
            raising :class:`ImageAlignmentError`.

    Raises:
        GeometryError: negative or otherwise unusable size.
        ImageAlignmentError: size is not a multiple of 512 and ``force`` is off.
        ImageTooSmallError: size does not fill a single full cylinder.
    """
    if not isinstance(file_size, int):
        raise GeometryError(
            "image size must be an integer number of bytes, got {0}".format(
                type(file_size).__name__
            )
        )
    if file_size < 0:
        raise GeometryError("invalid image size: {0}".format(file_size))

    original_size = file_size
    effective_size = file_size
    if file_size % SECTOR_SIZE:
        if not force:
            raise ImageAlignmentError(file_size)
        effective_size = file_size - (file_size % SECTOR_SIZE)

    total_sectors = effective_size // SECTOR_SIZE
    cylinders = total_sectors // CYLINDER_SECTORS
    if cylinders < 1:
        raise ImageTooSmallError(total_sectors, file_size)

    return Geometry(
        file_size=effective_size,
        total_sectors=total_sectors,
        cylinders=cylinders,
        heads=HEADS,
        sectors=SECTORS,
        original_size=original_size,
    )
