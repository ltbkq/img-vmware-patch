# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
