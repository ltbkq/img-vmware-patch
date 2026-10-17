# img-vmware-patch

> **English** | [简体中文](README.zh-CN.md)

Generate a **VMDK descriptor** for a raw disk image (`.img`, `.bin`, `.raw`, …)
and optionally prepare a minimal **VMware Workstation / Player / Fusion**
virtual machine around it.

No conversion, no wizard: the raw image stays exactly where it is and is
referenced through a single `FLAT` extent, which makes the operation instant
even for multi-terabyte images.

## Two ways to run a raw image

| | `link` / `create` (this tool) | [`native/`](native/README.md) |
| --- | --- | --- |
| Approach | writes a `.vmdk` **descriptor file** next to the image | patches VMware so it can open the bare `.img` directly |
| On disk | one extra `.vmdk` you can name | hidden `.fbimg-desc` / `.fbimg-data` |
| GUI file chooser | pick the `.vmdk` | pick the `.img` (GUI patch included) |
| Modifies VMware | no | yes — one-shot, idempotent, fully reversible |
| Best for | Windows/macOS hosts, shipping a `.vmdk`, zero-risk setups | booting the raw `.img` as-is on a Linux host |

Both can coexist; they do not conflict.

* Pure Python 3.9+, **standard library only**.
* `link` and `info` never require VMware — they work on any machine.
* `create` writes the `.vmx` first, then tries to add it to the Workstation
  library via `vmrun` when available (harmless if the command is unsupported).

## Installation

```bash
python3 -m pip install .            # provides the `img-vmware-patch` command
# or run it straight from a checkout:
PYTHONPATH=src python3 -m img_vmware_patch.cli --help
```

## Usage

```
img-vmware-patch <command> [options]

Commands:
  link      仅生成 VMDK descriptor 文件          (write only the descriptor)
  create    生成 descriptor 并创建/注册虚拟机     (descriptor + .vmx + register)
  info      显示 .img 文件信息和计算出的几何参数   (image info + geometry)

Global: -h/--help  -v/--version
link:   -o/--output PATH   -a/--adapter {ide,sata,scsi}   -f/--force
create: -n/--name  -m/--ram MB(默认 2048)  -g/--guest(默认 otherlinux-64)
        --dir PATH   [-o/--output 同 link]  [-a/--adapter 同 link]  [-f/--force]
```

Examples:

```bash
# show what the image looks like to VMware
img-vmware-patch info /var/lib/vms/disk.img

# descriptor only, SCSI/LSI Logic adapter
img-vmware-patch link disk.img -o disk.vmdk -a scsi

# descriptor + .vmx in /srv/vms/MyVM, 4 GiB RAM, then attempt vmrun registration
img-vmware-patch create disk.img -n "MyVM" --dir /srv/vms/MyVM -m 4096 -a scsi

# overwrite an existing descriptor and tolerate a non 512-aligned image
img-vmware-patch link disk.img -o disk.vmdk -f
```

`create` writes `NAME.vmx` and `NAME.vmdk` into `--dir` and points
`<controller>:0.fileName` at the bare descriptor name, so both files must live
in the same directory (pass `-o` inside `--dir` if you rename the descriptor).

Environment variables:

| Variable | Effect |
| --- | --- |
| `IMG_VMWARE_PATCH_REGISTER=0` | `create` skips the optional `vmrun -T ws register` call (files are still written). Handy on CI machines. |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | general error: image not 512-byte aligned (without `-f`), image too small, missing input file, existing output without `-f`, I/O error |
| `2` | argument error (argparse usage errors, invalid VM name) |
| `3` | VMware compatibility problem: `create` found no `vmrun` (descriptor and `.vmx` are written anyway, an install hint is printed) |

`vmrun` registration failures are *not* fatal: a warning is printed and `create`
still exits `0`. Note that Workstation 17.6.3's `vmrun` does not implement the
`register` command, so the VM is usually *not* added to the Workstation library —
open `NAME.vmx` via **File > Open**, or launch it directly with
`vmrun -T ws start NAME.vmx nogui`.

## Geometry rules

* `total_sectors = file_size // 512`; a size that is not a multiple of 512 is
  rejected with exit code 1 (or truncated down to the boundary with `--force`,
  with a warning on stderr).
* `heads = 16`, `sectors = 63`,
  `cylinders = total_sectors // (16 * 63)` → 1008 sectors per cylinder.
* Left-over sectors stay in the extent and are carried by a trailing
  incomplete cylinder; fewer than one full cylinder (e.g. a 512 B file) is an
  "image too small" error.

## Descriptor and `.vmx`

```
# Disk DescriptorFile
version=1
encoding="UTF-8"
CID=fffffffe
parentCID=ffffffff
createType="monolithicFlat"

# Extent description
RW 131072 FLAT "/abs/path/to/disk.img" 0

# The Disk Data Base
#DDB

ddb.virtualHWVersion = "4"
ddb.adapterType = "lsilogic"      # ide -> ide, sata -> sata, scsi -> lsilogic
ddb.geometry.cylinders = "130"
ddb.geometry.heads = "16"
ddb.geometry.sectors = "63"
```

Paths are absolute and always use forward slashes (`C:/vm/disk.img` on
Windows); embedded `"` characters are escaped as `\"`.

The `.vmx` is a minimal configuration: `config.version = "8"`,
`virtualHW.version = "19"` (WS 17), `memsize`, `displayName`, `guestOS`,
`<controller>:0.*` plus `scsi0.virtualDev = "lsilogic"` for SCSI and
`floppy0.present = "FALSE"`.

## Development

```bash
python3 -m unittest discover -s tests -t .     # 120 tests, stdlib unittest
```

The test suite never starts `vmrun` or a virtual machine; the VMware probe is
always mocked.

## License

MIT — see [LICENSE](LICENSE).
