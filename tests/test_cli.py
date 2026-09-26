"""Unit tests for the ``img-vmware-patch`` CLI (return codes, options, output).

No real ``vmrun``/``vmware`` is ever executed: every test that reaches the
VMware code path patches :mod:`img_vmware_patch.vmware` explicitly, and a
class-wide guard makes an unpatched call fail loudly instead of running the
real tooling.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from img_vmware_patch import cli, vmware
from img_vmware_patch.cli import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VMWARE,
    REGISTER_ENV,
    main,
)

MIB = 1024 * 1024
CYLINDER_BYTES = 16 * 63 * 512  # 516096
NO_VMWARE = "real vmrun must never be called from unit tests"


def run_cli(argv):
    """Run ``main`` capturing stdout/stderr; argparse ``SystemExit`` becomes a code."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:  # argparse: --help/--version/usage errors
            code = 0 if exc.code is None else exc.code
            if not isinstance(code, int):
                code = 1
    return code, stdout.getvalue(), stderr.getvalue()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

        # Guard: fail loudly instead of probing/executing real VMware tools.
        for attribute in ("find_vmrun", "get_version", "register_vm"):
            patcher = mock.patch.object(
                cli.vmware,
                attribute,
                side_effect=AssertionError("{0}: {1}".format(NO_VMWARE, attribute)),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def image(self, name: str, size: int) -> Path:
        """Create a (sparse) image file of exactly ``size`` bytes."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(size)
        self.assertEqual(path.stat().st_size, size)
        return path


class GlobalOptionsTest(CliTestCase):
    def test_version_returns_zero(self):
        code, out, _ = run_cli(["-v"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("0.1.0", out)
        self.assertIn("img-vmware-patch", out)

    def test_long_version(self):
        code, out, _ = run_cli(["--version"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("img-vmware-patch 0.1.0", out)

    def test_help_returns_zero_and_lists_commands(self):
        code, out, _ = run_cli(["--help"])
        self.assertEqual(code, EXIT_OK)
        for command in ("link", "create", "info"):
            self.assertIn(command, out)
        self.assertIn("仅生成 VMDK descriptor 文件", out)

    def test_no_command_is_usage_error(self):
        code, _, err = run_cli([])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", err.lower())

    def test_unknown_command_is_usage_error(self):
        code, _, err = run_cli(["bogus"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", err.lower())


class InfoTest(CliTestCase):
    def test_success_returns_zero(self):
        image = self.image("disk.img", 64 * MIB)
        code, out, err = run_cli(["info", str(image)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        self.assertIn(str(image), out)
        self.assertIn("67108864 bytes", out)
        self.assertIn("131072", out)
        self.assertIn("130 cylinders x 16 heads x 63 sectors", out)
        self.assertIn("chs:        130/16/63", out)
        self.assertIn("partial:    32 sectors", out)

    def test_exact_cylinder_image_has_no_partial_line(self):
        image = self.image("exact.img", CYLINDER_BYTES)
        code, out, _ = run_cli(["info", str(image)])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("partial:", out)
        self.assertIn("chs:        1/16/63", out)

    def test_unaligned_returns_one(self):
        image = self.image("odd.img", MIB + 1)
        code, _, err = run_cli(["info", str(image)])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not a multiple of 512", err)

    def test_missing_image_returns_one(self):
        code, _, err = run_cli(["info", str(self.root / "nope.img")])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not found", err)

    def test_too_small_image_returns_one(self):
        image = self.image("tiny.img", 512)
        code, _, err = run_cli(["info", str(image)])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("image too small", err)
        self.assertIn("镜像太小", err)

    def test_missing_argument_returns_two(self):
        code, _, err = run_cli(["info"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", err.lower())

    def test_never_probes_vmware(self):
        image = self.image("disk.img", 64 * MIB)
        code, _, _ = run_cli(["info", str(image)])
        self.assertEqual(code, EXIT_OK)


class LinkTest(CliTestCase):
    def test_success_returns_zero_and_writes_descriptor(self):
        image = self.image("smoke.bin", 64 * MIB)
        output = self.root / "smoke.vmdk"
        code, out, err = run_cli(
            ["link", str(image), "-o", str(output), "-a", "scsi"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        self.assertIn(str(output), out)
        self.assertIn("RW 131072 FLAT", out)
        self.assertIn("lsilogic", out)

        text = output.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Disk DescriptorFile\n"))
        self.assertIn(
            'RW 131072 FLAT "{0}" 0'.format(image.resolve()), text
        )
        self.assertIn('createType="monolithicFlat"', text)
        self.assertIn('ddb.adapterType = "lsilogic"', text)
        self.assertIn('ddb.geometry.cylinders = "130"', text)
        self.assertIn('ddb.geometry.heads = "16"', text)
        self.assertIn('ddb.geometry.sectors = "63"', text)
        self.assertIn('ddb.virtualHWVersion = "4"', text)

    def test_default_output_is_beside_the_image(self):
        image = self.image("disk.img", 64 * MIB)
        code, out, _ = run_cli(["link", str(image)])
        self.assertEqual(code, EXIT_OK)
        expected = image.with_suffix(".vmdk")
        self.assertTrue(expected.exists())
        self.assertIn(str(expected), out)

    def test_existing_output_returns_one(self):
        image = self.image("disk.img", 64 * MIB)
        output = self.root / "disk.vmdk"
        output.write_text("old\n", encoding="utf-8")
        code, _, err = run_cli(["link", str(image), "-o", str(output)])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("already exists", err)
        self.assertIn("-f/--force", err)
        self.assertEqual(output.read_text(encoding="utf-8"), "old\n")

    def test_force_overwrites_existing_output(self):
        image = self.image("disk.img", 64 * MIB)
        output = self.root / "disk.vmdk"
        output.write_text("old\n", encoding="utf-8")
        code, _, _ = run_cli(
            ["link", str(image), "-o", str(output), "--force"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertNotEqual(output.read_text(encoding="utf-8"), "old\n")
        self.assertIn("monolithicFlat", output.read_text(encoding="utf-8"))

    def test_unaligned_without_force_returns_one(self):
        image = self.image("odd.img", MIB + 1)
        code, _, err = run_cli(["link", str(image)])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not a multiple of 512", err)
        self.assertIn("-f/--force", err)
        self.assertFalse((self.root / "odd.vmdk").exists())

    def test_force_truncates_unaligned_image(self):
        image = self.image("odd.img", MIB + 1)
        output = self.root / "odd.vmdk"
        code, _, err = run_cli(
            ["link", str(image), "-o", str(output), "-f"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("warning", err)
        self.assertIn("1048576", err)
        text = output.read_text(encoding="utf-8")
        self.assertIn("RW 2048 FLAT", text)
        self.assertIn('ddb.geometry.cylinders = "2"', text)

    def test_missing_image_returns_one(self):
        code, _, err = run_cli(
            ["link", str(self.root / "nope.img"), "-o", str(self.root / "x.vmdk")]
        )
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not found", err)

    def test_bad_adapter_returns_two(self):
        image = self.image("disk.img", 64 * MIB)
        code, _, err = run_cli(["link", str(image), "-a", "nvme"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", err.lower())

    def test_missing_image_argument_returns_two(self):
        code, _, err = run_cli(["link"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", err.lower())

    def test_never_probes_vmware(self):
        image = self.image("disk.img", 64 * MIB)
        code, _, _ = run_cli(["link", str(image), "-o", str(self.root / "d.vmdk")])
        self.assertEqual(code, EXIT_OK)


class CreateTest(CliTestCase):
    def _vmware_patches(self, vmrun="/fake/vmrun", version="1.17.0", registered=True,
                        detail=""):
        """Return a list of patches describing a (fake) VMware installation."""
        patches = [
            mock.patch.object(cli.vmware, "find_vmrun", return_value=vmrun),
            mock.patch.object(
                cli.vmware, "get_version", return_value=version
            ),
            mock.patch.object(
                cli.vmware, "register_vm", return_value=(registered, detail)
            ),
        ]
        return patches

    def _run_patched(self, argv, patches):
        for patch in patches:
            patch.start()
        try:
            return run_cli(argv)
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_without_vmrun_returns_three_but_writes_files(self):
        image = self.image("smoke.bin", 64 * MIB)
        vm_dir = self.root / "vm"
        patches = [
            mock.patch.object(cli.vmware, "find_vmrun", return_value=None),
            mock.patch.object(
                cli.vmware, "get_version", side_effect=AssertionError(NO_VMWARE)
            ),
            mock.patch.object(
                cli.vmware, "register_vm", side_effect=AssertionError(NO_VMWARE)
            ),
        ]
        code, out, err = self._run_patched(
            ["create", str(image), "-n", "SmokeVM", "--dir", str(vm_dir)], patches
        )
        self.assertEqual(code, EXIT_VMWARE)
        self.assertIn("vmrun not found", err)
        self.assertIn("install", err)
        self.assertIn(str(vm_dir / "SmokeVM.vmdk"), out)
        self.assertIn(str(vm_dir / "SmokeVM.vmx"), out)

        descriptor = (vm_dir / "SmokeVM.vmdk").read_text(encoding="utf-8")
        self.assertIn('RW 131072 FLAT "{0}" 0'.format(image.resolve()), descriptor)
        self.assertIn('createType="monolithicFlat"', descriptor)

        vmx = (vm_dir / "SmokeVM.vmx").read_text(encoding="utf-8")
        self.assertIn('displayName = "SmokeVM"', vmx)
        self.assertIn('ide0:0.fileName = "SmokeVM.vmdk"', vmx)
        self.assertIn('ide0:0.present = "TRUE"', vmx)
        self.assertIn('virtualHW.version = "19"', vmx)
        self.assertIn('memsize = "2048"', vmx)
        self.assertIn('guestOS = "otherlinux-64"', vmx)
        self.assertIn('floppy0.present = "FALSE"', vmx)

    def test_registers_when_vmrun_is_found(self):
        image = self.image("disk.img", 64 * MIB)
        vm_dir = self.root / "vm"
        patches = self._vmware_patches()
        code, out, err = self._run_patched(
            [
                "create",
                str(image),
                "-n",
                "RegVM",
                "--dir",
                str(vm_dir),
                "-m",
                "4096",
                "-g",
                "ubuntu-64",
                "-a",
                "scsi",
            ],
            patches,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        self.assertIn("/fake/vmrun", out)
        self.assertIn("1.17.0", out)
        self.assertIn("registration: ok", out)

    def test_register_called_with_expected_command(self):
        image = self.image("disk.img", 64 * MIB)
        vm_dir = self.root / "vm"
        register_mock = mock.Mock(return_value=(True, ""))
        patches = [
            mock.patch.object(cli.vmware, "find_vmrun", return_value="/fake/vmrun"),
            mock.patch.object(cli.vmware, "get_version", return_value=None),
            mock.patch.object(cli.vmware, "register_vm", register_mock),
        ]
        code, _, _ = self._run_patched(
            ["create", str(image), "-n", "RegVM", "--dir", str(vm_dir)],
            patches,
        )
        self.assertEqual(code, EXIT_OK)
        register_mock.assert_called_once_with(
            vm_dir / "RegVM.vmx", vmrun="/fake/vmrun"
        )

        vmx = (vm_dir / "RegVM.vmx").read_text(encoding="utf-8")
        self.assertIn('ide0:0.fileName = "RegVM.vmdk"', vmx)

    def test_register_failure_still_returns_zero(self):
        image = self.image("disk.img", 64 * MIB)
        vm_dir = self.root / "vm"
        patches = self._vmware_patches(registered=False, detail="boom: not registered")
        code, _, err = self._run_patched(
            ["create", str(image), "-n", "FailVM", "--dir", str(vm_dir)],
            patches,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("warning", err)
        self.assertIn("boom: not registered", err)
        self.assertTrue((vm_dir / "FailVM.vmx").exists())

    def test_register_can_be_skipped_via_environment(self):
        image = self.image("disk.img", 64 * MIB)
        vm_dir = self.root / "vm"
        patches = [
            mock.patch.object(cli.vmware, "find_vmrun", return_value="/fake/vmrun"),
            mock.patch.object(
                cli.vmware,
                "get_version",
                side_effect=AssertionError(NO_VMWARE),
            ),
            mock.patch.dict(os.environ, {REGISTER_ENV: "0"}),
        ]
        code, out, _ = self._run_patched(
            ["create", str(image), "-n", "SkipVM", "--dir", str(vm_dir)],
            patches,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("skipped", out)
        self.assertTrue((vm_dir / "SkipVM.vmx").exists())

    def test_missing_name_returns_two(self):
        image = self.image("disk.img", 64 * MIB)
        code, _, err = run_cli(["create", str(image), "--dir", str(self.root / "vm")])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", err.lower())

    def test_missing_dir_returns_two(self):
        image = self.image("disk.img", 64 * MIB)
        code, _, err = run_cli(["create", str(image), "-n", "VM"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", err.lower())

    def test_bad_ram_returns_two(self):
        image = self.image("disk.img", 64 * MIB)
        for value in ("abc", "0", "-1024"):
            with self.subTest(value=value):
                code, _, err = run_cli(
                    [
                        "create",
                        str(image),
                        "-n",
                        "VM",
                        "--dir",
                        str(self.root / "vm"),
                        "-m",
                        value,
                    ]
                )
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn("usage", err.lower())

    def test_empty_guest_returns_two(self):
        image = self.image("disk.img", 64 * MIB)
        code, _, _ = run_cli(
            [
                "create",
                str(image),
                "-n",
                "VM",
                "--dir",
                str(self.root / "vm"),
                "-g",
                "  ",
            ]
        )
        self.assertEqual(code, EXIT_USAGE)

    def test_invalid_name_returns_two(self):
        image = self.image("disk.img", 64 * MIB)
        for name in ("../evil", "a/b", 'has"quote'):
            with self.subTest(name=name):
                code, _, err = run_cli(
                    ["create", str(image), "-n", name, "--dir", str(self.root / "vm")]
                )
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn("invalid VM name", err)
                self.assertFalse((self.root / "vm").exists())

    def test_existing_outputs_return_one_then_force_returns_zero(self):
        image = self.image("disk.img", 64 * MIB)
        vm_dir = self.root / "vm"
        argv = ["create", str(image), "-n", "DupVM", "--dir", str(vm_dir)]
        patches = self._vmware_patches()

        code, _, _ = self._run_patched(argv, patches)
        self.assertEqual(code, EXIT_OK)

        code, _, err = self._run_patched(argv, patches)
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("already exists", err)
        self.assertIn("-f/--force", err)

        code, _, _ = self._run_patched(argv + ["-f"], patches)
        self.assertEqual(code, EXIT_OK)

    def test_creates_missing_vm_directory(self):
        image = self.image("disk.img", 64 * MIB)
        vm_dir = self.root / "does" / "not" / "exist"
        patches = self._vmware_patches()
        code, _, _ = self._run_patched(
            ["create", str(image), "-n", "NewVM", "--dir", str(vm_dir)], patches
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue((vm_dir / "NewVM.vmx").is_file())
        self.assertTrue((vm_dir / "NewVM.vmdk").is_file())

    def test_custom_descriptor_output_is_used_as_file_name(self):
        image = self.image("disk.img", 64 * MIB)
        vm_dir = self.root / "vm"
        output = vm_dir / "custom.vmdk"
        patches = self._vmware_patches()
        code, _, _ = self._run_patched(
            [
                "create",
                str(image),
                "-n",
                "CustomVM",
                "--dir",
                str(vm_dir),
                "-o",
                str(output),
            ],
            patches,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(output.exists())
        vmx = (vm_dir / "CustomVM.vmx").read_text(encoding="utf-8")
        self.assertIn('ide0:0.fileName = "custom.vmdk"', vmx)

    def test_descriptor_outside_vm_dir_warns(self):
        image = self.image("disk.img", 64 * MIB)
        vm_dir = self.root / "vm"
        outside = self.root / "elsewhere" / "out.vmdk"
        patches = self._vmware_patches()
        code, _, err = self._run_patched(
            [
                "create",
                str(image),
                "-n",
                "WideVM",
                "--dir",
                str(vm_dir),
                "-o",
                str(outside),
            ],
            patches,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("outside --dir", err)
        self.assertTrue(outside.exists())
        vmx = (vm_dir / "WideVM.vmx").read_text(encoding="utf-8")
        self.assertIn('ide0:0.fileName = "out.vmdk"', vmx)

    def test_nonexistent_image_returns_one(self):
        code, _, err = run_cli(
            [
                "create",
                str(self.root / "missing.img"),
                "-n",
                "VM",
                "--dir",
                str(self.root / "vm"),
            ]
        )
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not found", err)

    def test_unaligned_image_without_force_returns_one(self):
        image = self.image("odd.img", MIB + 1)
        code, _, err = run_cli(
            ["create", str(image), "-n", "VM", "--dir", str(self.root / "vm")]
        )
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not a multiple of 512", err)
        self.assertFalse((self.root / "vm").exists())


class VmwareDetectionTest(unittest.TestCase):
    """``vmware.py``: detection never executes the real VMware tooling."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _fake_tool(self, name="vmrun"):
        path = self.root / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_linux_default_candidates(self):
        self.assertEqual(
            vmware.default_candidates("Linux", "vmrun"), ("/usr/bin/vmrun",)
        )
        self.assertEqual(
            vmware.default_candidates("Linux", "vmware-vdiskmanager"),
            ("/usr/bin/vmware-vdiskmanager",),
        )

    def test_windows_default_candidates(self):
        candidates = vmware.default_candidates("Windows", "vmrun")
        self.assertEqual(
            candidates[0],
            r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
        )
        self.assertTrue(all(path.endswith(".exe") for path in candidates))
        self.assertEqual(
            vmware.default_candidates("Windows", "vmware-vdiskmanager")[0],
            r"C:\Program Files (x86)\VMware\VMware Workstation\vmware-vdiskmanager.exe",
        )

    def test_fixed_candidate_wins_and_which_is_not_consulted(self):
        fake = self._fake_tool()
        probed = []

        def which(name):
            probed.append(name)
            return "/somewhere/else/vmrun"

        found = vmware.find_vmrun(
            system="Linux", candidates=[str(fake)], which=which
        )
        self.assertEqual(found, str(fake))
        self.assertEqual(probed, [])

    def test_falls_back_to_which(self):
        found = vmware.find_vmrun(
            system="Linux", candidates=[], which=lambda name: "/opt/bin/vmrun"
        )
        self.assertEqual(found, "/opt/bin/vmrun")

    def test_returns_none_when_nothing_is_installed(self):
        found = vmware.find_vmrun(
            system="Linux", candidates=["/no/such/vmrun"], which=lambda name: None
        )
        self.assertIsNone(found)

    def test_detection_never_executes_a_candidate(self):
        marker = self.root / "executed"
        fake = self._fake_tool()
        fake.write_text(
            "#!/bin/sh\ntouch {0}\n".format(marker), encoding="utf-8"
        )
        fake.chmod(0o755)
        found = vmware.find_vmrun(system="Linux", candidates=[str(fake)])
        self.assertEqual(found, str(fake))
        self.assertFalse(marker.exists())

    def test_vdiskmanager_detection(self):
        fake = self._fake_tool("vmware-vdiskmanager")
        found = vmware.find_vdiskmanager(
            system="Linux", candidates=[str(fake)], which=lambda name: None
        )
        self.assertEqual(found, str(fake))

    def test_get_version_uses_injected_runner(self):
        seen = []

        def runner(command, **kwargs):
            seen.append(list(command))
            return SimpleNamespace(
                returncode=1,
                stdout="vmrun 1.17.0 build-17704449\n",
                stderr="",
            )

        self.assertEqual(vmware.get_version("/usr/bin/vmrun", runner=runner), "1.17.0")
        self.assertEqual(seen, [["/usr/bin/vmrun"]])

    def test_get_version_failure_returns_none(self):
        def runner(command, **kwargs):
            raise OSError("cannot execute")

        self.assertIsNone(vmware.get_version("/bad/vmrun", runner=runner))

    def test_register_vm_command_and_success(self):
        seen = []

        def runner(command, **kwargs):
            seen.append(list(command))
            return SimpleNamespace(returncode=0, stdout="registered\n", stderr="")

        ok, detail = vmware.register_vm(
            "/vm/x.vmx", vmrun="/usr/bin/vmrun", runner=runner
        )
        self.assertTrue(ok)
        self.assertEqual(detail, "registered")
        self.assertEqual(
            seen, [["/usr/bin/vmrun", "-T", "ws", "register", "/vm/x.vmx"]]
        )

    def test_register_vm_failure_is_reported_not_raised(self):
        def runner(command, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="Vmrun error")

        ok, detail = vmware.register_vm(
            "/vm/x.vmx", vmrun="/usr/bin/vmrun", runner=runner
        )
        self.assertFalse(ok)
        self.assertEqual(detail, "Vmrun error")

    def test_register_vm_without_vmrun(self):
        with mock.patch.object(vmware, "find_vmrun", return_value=None):
            ok, detail = vmware.register_vm(
                "/vm/x.vmx", runner=mock.Mock(side_effect=AssertionError(NO_VMWARE))
            )
        self.assertFalse(ok)
        self.assertEqual(detail, "vmrun not found")

    def test_install_hint_mentions_installation(self):
        for system in ("Linux", "Windows"):
            with self.subTest(system=system):
                hint = vmware.install_hint(system)
                self.assertIn("install", hint)
                self.assertIn("link/info", hint)


if __name__ == "__main__":
    unittest.main()
