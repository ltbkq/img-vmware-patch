# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

* **`native/` — boot raw `.img` images in VMware directly (Linux hosts).**
  A disk-access layer (`fbimg-shim.c` → `libfbimgshim.so`) is injected as a
  `DT_NEEDED` entry into `vmware-vmx`, `vmware-vmx-debug`, `vmware-vmx-stats`
  and `appLoader`, so `open("disk.img")` is redirected to a hidden
  `.fbimg-desc` descriptor generated on the fly next to the image. Reads and
  writes still land at offset 0 of the `.img`; `rename`/`unlink`/`truncate` are
  hooked so VMware's descriptor update path can never replace the payload.
* `native/patch-vmware-ui-imgfilter.py` — adds `*.img` to the GUI "Virtual
  Disks" file filter (rewrites the 4-byte `lea` displacement that loads the
  second glob, plus the English/Chinese labels). Idempotent, `--status`,
  `--revert`, `--root`.
* `native/install-vmware-img.sh` — self-contained one-shot installer
  (`install` / `uninstall` / `check` / `selftest`), sources embedded, prefix
  auto-detection, `.orig` backups everywhere, safe against a running VMware.
* `native/install-native.sh` / `native/uninstall-native.sh` — split install and
  full revert, for repo development.
* `native/check-sync.sh` — asserts the embedded copies in the one-shot script
  match the standalone sources.
* `native/README.md` / `native/README.zh-CN.md` — full write-up: background,
  design, the GUI filter patch, verification evidence (VMware Workstation
  26.0.1), the eight pitfalls hit during development, portability notes and an
  FAQ.

### Changed

* VMware installation prefix is detected instead of hard-coded
  (`/usr/lib/vmware` → `/usr/local/vmware` → `/opt/vmware`, then by resolving
  the `vmware` command); the shim's process check matches any path containing
  `/vmware/` rather than a fixed prefix.

## [0.1.0] - 2026-09-26

### Added

* `link` — write only a `monolithicFlat` VMDK descriptor next to (or given by
  `-o`) a raw image, with `-a/--adapter {ide,sata,scsi}` and `-f/--force`.
* `create` — write the descriptor plus a minimal `.vmx` (`virtualHW.version`
  19) into `--dir` and optionally add it to the Workstation library with
  `vmrun -T ws register`; registration failures only warn (exit 0), a missing
  `vmrun` exits 3 after the files have been written.
* `info` — print image size, sector count and the computed
  cylinders/heads/sectors geometry.
* Geometry rules: 512-byte sectors, 16 heads, 63 sectors, floor division for
  cylinders with the remainder carried by a trailing incomplete cylinder,
  `--force` truncation for non-aligned images, "image too small" below one
  cylinder.
* Path normalisation: absolute paths with forward slashes everywhere
  (`C:/vm/disk.img` on Windows), `"` escaped as `\"` inside the extent line.
* VMware detection helpers for `vmrun` and `vmware-vdiskmanager` (Linux and
  Windows search order) that never execute anything during detection.
* `IMG_VMWARE_PATCH_REGISTER=0` to skip the optional registration call.
* Standard-library `unittest` suite (120 tests) covering geometry boundaries,
  path normalisation, `--force`, existing outputs and every CLI return code;
  the suite never runs `vmrun`.
