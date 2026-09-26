"""Unit tests for :mod:`img_vmware_patch.vmx` (minimal .vmx generation)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from img_vmware_patch.descriptor import OutputExistsError, OutputError
from img_vmware_patch.vmx import (
    DEFAULT_GUEST_OS,
    DEFAULT_RAM_MB,
    build_vmx,
    write_vmx,
)


def _vmx(**kwargs):
    defaults = {"display_name": "My VM", "descriptor_name": "My VM.vmdk"}
    defaults.update(kwargs)
    return build_vmx(**defaults)


class BuildVmxTest(unittest.TestCase):
    def test_common_keys(self):
        text = _vmx(ram_mb=4096, guest_os="ubuntu-64")
        self.assertIn('.encoding = "UTF-8"', text)
        self.assertIn('config.version = "8"', text)
        self.assertIn('displayName = "My VM"', text)
        self.assertIn('guestOS = "ubuntu-64"', text)
        self.assertIn('memsize = "4096"', text)
        self.assertIn('virtualHW.version = "19"', text)
        self.assertIn('floppy0.present = "FALSE"', text)
        self.assertTrue(text.endswith("\n"))

    def test_defaults(self):
        self.assertEqual(DEFAULT_RAM_MB, 2048)
        self.assertEqual(DEFAULT_GUEST_OS, "otherlinux-64")
        text = _vmx()
        self.assertIn('memsize = "2048"', text)
        self.assertIn('guestOS = "otherlinux-64"', text)

    def test_ide_adapter(self):
        text = _vmx(adapter="ide")
        self.assertIn('ide0.present = "TRUE"', text)
        self.assertIn('ide0:0.present = "TRUE"', text)
        self.assertIn('ide0:0.fileName = "My VM.vmdk"', text)
        self.assertNotIn("sata", text)
        self.assertNotIn("scsi", text)

    def test_sata_adapter(self):
        text = _vmx(adapter="sata")
        self.assertIn('sata0.present = "TRUE"', text)
        self.assertIn('sata0:0.present = "TRUE"', text)
        self.assertIn('sata0:0.fileName = "My VM.vmdk"', text)
        self.assertNotIn("ide0", text)
        self.assertNotIn("scsi", text)

    def test_scsi_adapter(self):
        text = _vmx(adapter="scsi")
        self.assertIn('scsi0.virtualDev = "lsilogic"', text)
        self.assertIn('scsi0.present = "TRUE"', text)
        self.assertIn('scsi0:0.present = "TRUE"', text)
        self.assertIn('scsi0:0.fileName = "My VM.vmdk"', text)
        self.assertNotIn("ide0", text)
        self.assertNotIn("sata", text)

    def test_file_name_is_bare_descriptor_name(self):
        text = _vmx(descriptor_name="custom.vmdk")
        self.assertIn('ide0:0.fileName = "custom.vmdk"', text)

    def test_without_present_lines(self):
        text = _vmx(adapter="ide", with_present=False)
        self.assertIn('ide0:0.fileName = "My VM.vmdk"', text)
        self.assertNotIn(".present", text)
        self.assertNotIn("floppy0", text)

    def test_scsi_virtualdev_survives_without_present(self):
        text = _vmx(adapter="scsi", with_present=False)
        self.assertIn('scsi0.virtualDev = "lsilogic"', text)
        self.assertNotIn(".present", text)

    def test_quotes_in_display_name_are_escaped(self):
        text = _vmx(display_name='say "hi"')
        self.assertIn('displayName = "say \\"hi\\""', text)

    def test_backslash_in_display_name_is_escaped(self):
        text = _vmx(display_name=r"a\b")
        self.assertIn('displayName = "a\\\\b"', text)

    def test_unsupported_adapter(self):
        with self.assertRaises(ValueError):
            _vmx(adapter="nvme")

    def test_invalid_ram(self):
        for ram in (0, -1, "4096"):
            with self.subTest(ram=ram):
                with self.assertRaises(ValueError):
                    _vmx(ram_mb=ram)  # type: ignore[arg-type]

    def test_empty_names(self):
        with self.assertRaises(ValueError):
            _vmx(display_name="   ")
        with self.assertRaises(ValueError):
            _vmx(descriptor_name="")


class WriteVmxTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_writes_file(self):
        target = self.root / "vm.vmx"
        content = _vmx()
        written = write_vmx(target, content)
        self.assertEqual(written, target)
        self.assertEqual(target.read_text(encoding="utf-8"), content)

    def test_existing_file_without_force_raises(self):
        target = self.root / "vm.vmx"
        target.write_text("old\n", encoding="utf-8")
        with self.assertRaises(OutputExistsError) as ctx:
            write_vmx(target, _vmx())
        self.assertIsInstance(ctx.exception, OutputError)
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_existing_file_with_force_overwrites(self):
        target = self.root / "vm.vmx"
        target.write_text("old\n", encoding="utf-8")
        write_vmx(target, "new\n", force=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")


if __name__ == "__main__":
    unittest.main()
