"""Unit tests for :mod:`img_vmware_patch.geometry` (pure computation)."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from img_vmware_patch import geometry
from img_vmware_patch.geometry import (
    CYLINDER_SECTORS,
    HEADS,
    SECTOR_SIZE,
    SECTORS,
    Geometry,
    GeometryError,
    ImageAlignmentError,
    ImageTooSmallError,
    compute_geometry,
)

MIB = 1024 * 1024


class ConstantsTest(unittest.TestCase):
    def test_contract_constants(self):
        self.assertEqual(SECTOR_SIZE, 512)
        self.assertEqual(HEADS, 16)
        self.assertEqual(SECTORS, 63)
        self.assertEqual(CYLINDER_SECTORS, 1008)


class ComputeGeometryTest(unittest.TestCase):
    def test_64_mib_image(self):
        geometry_ = compute_geometry(64 * MIB)
        self.assertEqual(geometry_.file_size, 67108864)
        self.assertEqual(geometry_.total_sectors, 131072)
        # 131072 // 1008 = 130 full cylinders, 32 sectors left over
        self.assertEqual(geometry_.cylinders, 130)
        self.assertEqual(geometry_.heads, 16)
        self.assertEqual(geometry_.sectors, 63)
        self.assertEqual(geometry_.remainder_sectors, 32)
        self.assertFalse(geometry_.truncated)

    def test_exactly_one_cylinder(self):
        geometry_ = compute_geometry(CYLINDER_SECTORS * SECTOR_SIZE)
        self.assertEqual(geometry_.cylinders, 1)
        self.assertEqual(geometry_.total_sectors, CYLINDER_SECTORS)
        self.assertEqual(geometry_.remainder_sectors, 0)

    def test_boundary_one_byte_below_one_cylinder(self):
        with self.assertRaises(ImageTooSmallError):
            compute_geometry((CYLINDER_SECTORS - 1) * SECTOR_SIZE)

    def test_512_bytes_is_too_small(self):
        with self.assertRaises(ImageTooSmallError) as ctx:
            compute_geometry(512)
        self.assertEqual(ctx.exception.total_sectors, 1)
        self.assertEqual(ctx.exception.file_size, 512)
        self.assertIn("image too small", str(ctx.exception))
        self.assertIn("镜像太小", str(ctx.exception))

    def test_zero_bytes_is_too_small(self):
        with self.assertRaises(ImageTooSmallError):
            compute_geometry(0)

    def test_one_mib_plus_one_without_force_is_rejected(self):
        with self.assertRaises(ImageAlignmentError) as ctx:
            compute_geometry(MIB + 1)
        self.assertEqual(ctx.exception.file_size, MIB + 1)
        self.assertIn("not a multiple of 512", str(ctx.exception))

    def test_non_multiple_sizes_without_force(self):
        for size in (1, 100, 513, 4096 + 7, 64 * MIB + 511):
            with self.subTest(size=size):
                with self.assertRaises(ImageAlignmentError):
                    compute_geometry(size)

    def test_one_mib_plus_one_with_force_truncates(self):
        geometry_ = compute_geometry(MIB + 1, force=True)
        self.assertEqual(geometry_.file_size, MIB)
        self.assertEqual(geometry_.total_sectors, 2048)
        self.assertEqual(geometry_.cylinders, 2)  # 2048 // 1008
        self.assertEqual(geometry_.remainder_sectors, 32)
        self.assertTrue(geometry_.truncated)
        self.assertEqual(geometry_.source_size, MIB + 1)
        self.assertEqual(geometry_.original_size, MIB + 1)

    def test_force_that_truncates_below_one_cylinder_still_fails(self):
        with self.assertRaises(ImageTooSmallError):
            compute_geometry(SECTOR_SIZE - 1, force=True)

    def test_negative_size(self):
        with self.assertRaises(GeometryError):
            compute_geometry(-512)

    def test_non_integer_size(self):
        with self.assertRaises(GeometryError):
            compute_geometry(1024.5)  # type: ignore[arg-type]

    def test_exception_hierarchy(self):
        self.assertTrue(issubclass(ImageAlignmentError, GeometryError))
        self.assertTrue(issubclass(ImageTooSmallError, GeometryError))
        self.assertTrue(issubclass(GeometryError, ValueError))

    def test_geometry_is_frozen(self):
        geometry_ = compute_geometry(64 * MIB)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            geometry_.cylinders = 1  # type: ignore[misc]

    def test_chs_helpers(self):
        geometry_ = compute_geometry(64 * MIB)
        self.assertEqual(geometry_.chs, (130, 16, 63))
        self.assertEqual(geometry_.chs_string, "130/16/63")
        self.assertEqual(geometry_.cylinder_sectors, 1008)
        self.assertEqual(
            geometry_.describe(), "130 cylinders x 16 heads x 63 sectors"
        )

    def test_default_geometry_values(self):
        geometry_ = compute_geometry(64 * MIB)
        self.assertEqual(geometry_.heads, HEADS)
        self.assertEqual(geometry_.sectors, SECTORS)
        self.assertIsInstance(geometry_, Geometry)

    def test_module_exposes_compute_geometry(self):
        self.assertIs(geometry.compute_geometry, compute_geometry)


if __name__ == "__main__":
    unittest.main()
