"""Unit tests for :mod:`img_vmware_patch.descriptor` (path normalisation,
extent/descriptor rendering and output file writing)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from img_vmware_patch.descriptor import (
    ADAPTER_TYPES,
    OutputError,
    OutputExistsError,
    OutputIsDirectoryError,
    adapter_type,
    build_descriptor,
    escape_path,
    extent_line,
    normalize_path,
    write_text_file,
)
from img_vmware_patch.geometry import compute_geometry

MIB = 1024 * 1024


class NormalizePathTest(unittest.TestCase):
    """Path normalisation: absolute + always forward slashes."""

    def test_posix_absolute_is_kept(self):
        self.assertEqual(
            normalize_path("/var/lib/vm/disk.img", windows=False),
            "/var/lib/vm/disk.img",
        )

    def test_posix_relative_uses_base(self):
        self.assertEqual(
            normalize_path("disk.img", base="/var/lib/vm", windows=False),
            "/var/lib/vm/disk.img",
        )

    def test_posix_keeps_spaces_and_unicode(self):
        self.assertEqual(
            normalize_path("my images/中文 镜像.img", base="/data", windows=False),
            "/data/my images/中文 镜像.img",
        )

    def test_posix_collapses_dot_segments(self):
        self.assertEqual(
            normalize_path("../images/./a.img", base="/data/vm", windows=False),
            "/data/images/a.img",
        )

    def test_posix_collapses_redundant_slashes(self):
        self.assertEqual(
            normalize_path("/data//vm/./a.img", windows=False),
            "/data/vm/a.img",
        )

    def test_windows_backslashes(self):
        self.assertEqual(
            normalize_path(r"C:\Users\me\my images\disk.img", windows=True),
            "C:/Users/me/my images/disk.img",
        )

    def test_windows_forward_slashes_stay_forward(self):
        self.assertEqual(
            normalize_path("C:/Users/me/disk.img", windows=True),
            "C:/Users/me/disk.img",
        )

    def test_windows_drive_case_is_preserved(self):
        self.assertEqual(
            normalize_path(r"c:\data\disk.img", windows=True),
            "c:/data/disk.img",
        )

    def test_windows_relative_uses_base(self):
        self.assertEqual(
            normalize_path(r"images\a.img", base=r"C:\vm", windows=True),
            "C:/vm/images/a.img",
        )

    def test_windows_dot_segments(self):
        self.assertEqual(
            normalize_path(r"C:\vm\..\shared\a.img", windows=True),
            "C:/shared/a.img",
        )

    def test_windows_unc_path(self):
        self.assertEqual(
            normalize_path(r"\\server\share\a.img", windows=True),
            "//server/share/a.img",
        )

    def test_windows_spaces_and_unicode(self):
        self.assertEqual(
            normalize_path(r"D:\虚拟机\my vm\disk.img", windows=True),
            "D:/虚拟机/my vm/disk.img",
        )

    def test_default_follows_current_platform(self):
        # this suite runs on POSIX; the default must not mangle posix paths
        if not sys.platform.startswith("win"):
            self.assertEqual(normalize_path("/tmp/a.img"), "/tmp/a.img")

    def test_bytes_path_is_decoded(self):
        self.assertEqual(
            normalize_path(b"/tmp/bytes.img", windows=False), "/tmp/bytes.img"
        )


class EscapePathTest(unittest.TestCase):
    def test_plain_path_untouched(self):
        self.assertEqual(escape_path("/tmp/a b.img"), "/tmp/a b.img")

    def test_quote_is_escaped(self):
        self.assertEqual(escape_path('/tmp/we"ird.img'), '/tmp/we\\"ird.img')


class ExtentLineTest(unittest.TestCase):
    def test_single_extent_line_format(self):
        self.assertEqual(
            extent_line(131072, "/tmp/opencode/smoke.bin"),
            'RW 131072 FLAT "/tmp/opencode/smoke.bin" 0',
        )

    def test_relative_path_becomes_absolute(self):
        self.assertEqual(
            extent_line(1008, "disk.img", base="/data", windows=False),
            'RW 1008 FLAT "/data/disk.img" 0',
        )

    def test_spaces_and_unicode_need_no_extra_escaping(self):
        self.assertEqual(
            extent_line(1008, "/data/my images/中文.img"),
            'RW 1008 FLAT "/data/my images/中文.img" 0',
        )

    def test_embedded_quote_is_escaped(self):
        self.assertEqual(
            extent_line(1008, '/data/we"ird.img'),
            'RW 1008 FLAT "/data/we\\"ird.img" 0',
        )

    def test_windows_style_path_uses_forward_slashes(self):
        self.assertEqual(
            extent_line(1008, r"C:\vm\disk.img", windows=True),
            'RW 1008 FLAT "C:/vm/disk.img" 0',
        )


class AdapterTypeTest(unittest.TestCase):
    def test_contract_mapping(self):
        self.assertEqual(adapter_type("ide"), "ide")
        self.assertEqual(adapter_type("sata"), "sata")
        self.assertEqual(adapter_type("scsi"), "lsilogic")
        self.assertEqual(ADAPTER_TYPES["scsi"], "lsilogic")

    def test_unknown_adapter(self):
        with self.assertRaises(ValueError):
            adapter_type("nvme")


class BuildDescriptorTest(unittest.TestCase):
    def test_full_descriptor_snapshot(self):
        geometry_ = compute_geometry(64 * MIB)
        text = build_descriptor(
            "/data/disk.img", geometry_, adapter="ide", windows=False
        )
        expected = (
            "# Disk DescriptorFile\n"
            "version=1\n"
            'encoding="UTF-8"\n'
            "CID=fffffffe\n"
            "parentCID=ffffffff\n"
            'createType="monolithicFlat"\n'
            "\n"
            "# Extent description\n"
            'RW 131072 FLAT "/data/disk.img" 0\n'
            "\n"
            "# The Disk Data Base\n"
            "#DDB\n"
            "\n"
            'ddb.virtualHWVersion = "4"\n'
            'ddb.adapterType = "ide"\n'
            'ddb.geometry.cylinders = "130"\n'
            'ddb.geometry.heads = "16"\n'
            'ddb.geometry.sectors = "63"\n'
        )
        self.assertEqual(text, expected)

    def test_ends_with_newline(self):
        text = build_descriptor("/data/disk.img", compute_geometry(64 * MIB))
        self.assertTrue(text.endswith("\n"))

    def test_adapter_variants(self):
        geometry_ = compute_geometry(64 * MIB)
        self.assertIn(
            'ddb.adapterType = "ide"',
            build_descriptor("/d/a.img", geometry_, adapter="ide"),
        )
        self.assertIn(
            'ddb.adapterType = "sata"',
            build_descriptor("/d/a.img", geometry_, adapter="sata"),
        )
        self.assertIn(
            'ddb.adapterType = "lsilogic"',
            build_descriptor("/d/a.img", geometry_, adapter="scsi"),
        )

    def test_unknown_adapter_raises(self):
        with self.assertRaises(ValueError):
            build_descriptor("/d/a.img", compute_geometry(64 * MIB), adapter="nvme")

    def test_extent_sectors_match_total_sectors(self):
        geometry_ = compute_geometry(MIB + 1, force=True)  # 2048 sectors
        text = build_descriptor("/d/a.img", geometry_)
        self.assertIn("RW 2048 FLAT", text)

    def test_geometry_ddb_values(self):
        geometry_ = compute_geometry(64 * MIB)
        text = build_descriptor("/d/a.img", geometry_)
        self.assertIn('ddb.geometry.cylinders = "130"', text)
        self.assertIn('ddb.geometry.heads = "16"', text)
        self.assertIn('ddb.geometry.sectors = "63"', text)
        self.assertIn('ddb.virtualHWVersion = "4"', text)

    def test_windows_image_path_rendering(self):
        geometry_ = compute_geometry(64 * MIB)
        text = build_descriptor(
            r"C:\images\my disk.img", geometry_, adapter="ide", windows=True
        )
        self.assertIn('RW 131072 FLAT "C:/images/my disk.img" 0', text)


class WriteTextFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_writes_content_and_creates_parents(self):
        target = self.root / "nested" / "dir" / "disk.vmdk"
        written = write_text_file(target, "hello\n")
        self.assertEqual(written, target)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

    def test_unicode_content_roundtrip(self):
        target = self.root / "u.vmdk"
        write_text_file(target, "中文 镜像\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "中文 镜像\n")

    def test_existing_file_without_force_raises(self):
        target = self.root / "disk.vmdk"
        target.write_text("old\n", encoding="utf-8")
        with self.assertRaises(OutputExistsError) as ctx:
            write_text_file(target, "new\n")
        self.assertIsInstance(ctx.exception, OutputError)
        self.assertIn("already exists", str(ctx.exception))
        # unchanged
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_existing_file_with_force_is_overwritten(self):
        target = self.root / "disk.vmdk"
        target.write_text("old\n", encoding="utf-8")
        write_text_file(target, "new\n", force=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_directory_target_raises(self):
        target = self.root / "adir"
        target.mkdir()
        with self.assertRaises(OutputIsDirectoryError):
            write_text_file(target, "x")

    def test_directory_target_raises_even_with_force(self):
        target = self.root / "adir"
        target.mkdir()
        with self.assertRaises(OutputIsDirectoryError):
            write_text_file(target, "x", force=True)


if __name__ == "__main__":
    unittest.main()
