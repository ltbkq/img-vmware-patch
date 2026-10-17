# native — boot a raw `.img` in VMware, natively

> [简体中文](README.zh-CN.md) ｜ Project overview: [../README.md](../README.md)

**TL;DR** — teach VMware to understand `.img`. Point a virtual machine straight at
`disk.img`: no conversion, no copying, and the image stays byte-identical, so it is
still usable with `dd`, `qemu-img`, or burned to a USB stick.

```bash
git clone https://github.com/ltbkq/img-vmware-patch && cd img-vmware-patch/native
sudo ./install-vmware-img.sh --img /path/to/disk.img     # install + self-test
# restart VMware -> VM Settings -> Hard Disk -> Change/Add -> pick the .img
```

| File | Purpose |
| --- | --- |
| `install-vmware-img.sh` | **one-shot installer** — embeds all sources, single file to copy to another machine |
| `fbimg-shim.c` | source of the disk-access layer `libfbimgshim.so` (~530 lines of C) |
| `patch-vmware-ui-imgfilter.py` | GUI file-filter patch (make `.img` selectable) |
| `install-native.sh` / `uninstall-native.sh` | split install / uninstall (same logic, for repo development) |
| `check-sync.sh` | verify the embedded copies in the one-shot script match the standalone sources |

---

## 1. Why it is needed: two obstacles

### 1.1 The disk engine only accepts vmdk containers

VMware's disk engine (DISKLIB, inside the `vmware-vmx` process) opens a virtual disk by
first reading the head of the file as a **descriptor** and following the extent line
`RW <sectors> FLAT "<datafile>" <offset>`:

```
# Disk DescriptorFile
version=1
encoding="UTF-8"
CID=aedf133b
parentCID=ffffffff
createType="monolithicFlat"

# Extent description
RW 67108864 FLAT ".fbimg-data" 0     # 64 x 512 B = 32 GiB of payload

# The Disk Data Base
#DDB
ddb.adapterType = "lsilogic"
```

A raw `.img` starts with a partition table, so the parser gives up with:

```
Failed to open disk: The specified file is not a virtual disk (16063)
Failed to get the list of partitions: The virtual disk specified is invalid.
```

Note this is a **container** limitation, not a format one: a vmdk is just a ~350 byte
text shell, and the payload may live in any file at any offset. That is exactly what
this project exploits.

### 1.2 The GUI only lists `*.vmdk`

The *Virtual Disks* entry of the "choose a disk" dialog has only two globs:

| Filter | Stock globs |
| --- | --- |
| Virtual Disks | `*.vmdk` `*.VMDK` |
| ISO Images | `*.iso` `*.ISO` **`*.img`** |
| Floppy Images | `*.flp` **`*.img`** |

so `.img` never even shows up in the list.

Moreover, "Unable to retrieve disk information" comes from the **GUI process**, not from
`vmware-vmx`: Workstation's GUI (`/usr/bin/vmware` → `appLoader`) runs its own DISKLIB
probe. Patching only `vmware-vmx` is not enough — the dialog keeps failing.

---

## 2. How it works

### 2.1 Intercept `open()` and synthesise a descriptor

```
open("<dir>/xxx.img")
   ├─ if <dir>/.fbimg-desc is missing, generate it from the real image size
   └─ open <dir>/.fbimg-desc instead
everything else is passed through untouched
```

The generated descriptor references a hidden symlink `.fbimg-data` in the same directory,
which points at the real image:

```
   VMware  --open-->  .fbimg-desc (277 B, text)
                          |  RW 67108864 FLAT ".fbimg-data" 0
                          v
                       .fbimg-data --symlink--> xxx.img   <- payload at offset 0
```

* VMware sees a completely valid descriptor that *it* would have written itself;
* every read/write lands at offset 0 of `xxx.img`, byte for byte;
* the descriptor is only 277 B, so VMware can rewrite it (CID, `ddb.longContentID`, …)
  as often as it likes without ever touching the payload.

### 2.2 More than `open()` is hooked

| Hooked | Why |
| --- | --- |
| `open`/`open64`/`openat`/`openat64` (+ `__open_2` etc.) | redirect `.img` to the descriptor |
| `creat`/`creat64` | some code paths create the disk that way |
| `rename`/`renameat`/`renameat2` | **payload protection**: VMware updates a descriptor via `write "xxx~"` + `rename("xxx~","xxx")`; left unhooked this *replaces the raw image itself* |
| `unlink`/`unlinkat` | deleting the descriptor must not delete the payload |
| `truncate` | keep VMware from truncating the payload to descriptor length |

The `rename`/`unlink`/`truncate` hooks exist because of a real incident: an early version
lost a 20 GiB image whose inode was replaced by a 277-byte descriptor.

### 2.3 Activation conditions (all three must hold; otherwise a no-op)

1. **The process is VMware**: the executable path contains `/vmware/` (covers
   `/usr/lib/vmware`, `/usr/local/vmware`, `/opt/vmware`, …) or the process name is one of
   `vmware-vmx`, `vmware-vmx-debug`, `vmware-vmx-stats`, `appLoader`, `vmrun`, `vmcli`,
   `vmrest`, `emmett`, `mksSandbox`, … or `FBIMG_ENABLE=1` is set;
2. the opened path **ends in `.img`**;
3. a descriptor **can be found or generated** in the same directory (size must be a
   multiple of 512).

Everything else — other programs, other extensions (`.raw`, `.bin`, `.qcow2`) — is
completely untouched.

### 2.4 Why `appLoader` must be patched too

`/usr/bin/vmware`, `vmrun` and `vmware-mount` are all symlinks to
`/usr/lib/vmware/bin/appLoader`, which `dlopen`s the GUI library `libvmwareui.so`:

| Task | Process | Needs the shim |
| --- | --- | --- |
| power on / read / write a disk | `vmware-vmx` | yes |
| add disk / change disk / retrieve disk info | `appLoader` | **yes** |
| `vmware-mount -p`, `vmware-vdiskmanager` | `appLoader` | **yes** |

Symptom when `appLoader` is missed: the `.vmx` works from the CLI, but the GUI reports
"Unable to retrieve disk information" as soon as you pick a disk.

---

## 3. Why `DT_NEEDED` instead of `LD_PRELOAD`

`vmware-vmx` is **setuid root**; glibc drops `LD_PRELOAD`/`LD_LIBRARY_PATH` in
secure-execution mode, so environment-based injection never works. `patchelf --add-needed`
makes the library a private dependency of those binaries instead:

| Approach | works with setuid | blast radius | needs env vars |
| --- | --- | --- | --- |
| `LD_PRELOAD` | no (dropped) | every launched process | yes |
| `/etc/ld.so.preload` | yes | **every program on the system** | no |
| **`DT_NEEDED` (this project)** | yes | only those VMware binaries | no |

Supporting details: the library carries a **SONAME** and is `ldconfig`-ed (setuid programs
resolve dependencies from the trusted cache only); `--add-needed` inserts the entry **first**
in the NEEDED table, so interposition wins over libc; running binaries are never written in
place (that gives `ETXTBSY`) — a new file is created and `rename`d over, with
`chmod --reference` copying permissions including the setuid bit.

---

## 4. The GUI filter: why a `lea` patch and not a string edit

```
1501fcb  *.vmdk        <- glob slot 1
1501fd2  *.VMDK        <- glob slot 2
1501ffd  *.img         <- already used by the ISO/floppy filters
```

The filter table has only **two glob slots** and `g_pattern_match_simple` supports no
character classes, so a single `*.[vV][mM][dD][kK]` is impossible and there is no room to
write `*.img` over `*.vmdk`. The patch therefore rewrites the **4-byte RIP-relative
displacement of the `lea` that loads slot 2**, pointing it at the existing `*.img` string:

```asm
; before: points at 0x1501fd2 "*.VMDK"
c4cd03: 48 8d 35 ?? ?? ?? ??   lea <...>(%rip),%rsi
; after
c4cd03: 48 8d 35 f3 52 8b 00   lea 0x8b52f3(%rip),%rsi  # 1501ffd
```

The only cost is that uppercase `*.VMDK` disappears from the list. Display labels are
updated in place as well (same length or shorter, never touching neighbouring data):

| Location | Before | After |
| --- | --- | --- |
| `libvmwareui.so` | `Virtual Disks (*.vmdk)` | `Disks (*.vmdk, *.img)` |
| `libvmwareui.so` | `ISO Images (*.iso)` | `ISO (*.iso, *.img)` |
| `messages/zh_CN/vmware.vmsg` | `虚拟磁盘 (*.vmdk)` | `虚拟磁盘 (*.vmdk, *.img)` |
| `messages/zh_CN/vmware.vmsg` | `ISO 映像 (*.iso)` | `ISO 映像 (*.iso, *.img)` |

The patcher searches for strings, **verifies the hit count** and gives up loudly
(never corrupting anything) if a VMware version does not match. Originals are kept as
`*.bak-imgfilter`.

---

## 5. Usage

```bash
sudo ./install-vmware-img.sh                    # install (auto-detects the VMware root)
sudo ./install-vmware-img.sh --img disk.img     # install + self-test against that image
sudo ./install-vmware-img.sh uninstall          # full revert (including the GUI patch)
./install-vmware-img.sh check                   # status only, no root needed
./install-vmware-img.sh selftest disk.img       # read the partition table via DISKLIB
```

Options: `--root DIR`, `--bin DIR`, `--img FILE`, `--no-apt`, `-h`, `-V`.
The script is idempotent, self-contained (sources embedded), keeps `.orig` backups and
handles a running VMware safely.

Point the VM at the image through the GUI (**VM Settings → Hard Disk → Change/Add**) or in
the `.vmx`:

```
scsi0:0.fileName   = "/absolute/path/disk.img"
scsi0:0.deviceType = "disk"
```

On first open, `.fbimg-desc` and `.fbimg-data` appear next to the image; both are
disposable and regenerated on demand. The image itself is never rewritten.

---

## 6. Verified on

VMware Workstation **26.0.1 build 25688693**, host Linux Mint 22.x, subject a 32 GiB raw
Linux Mint 22.3 image (GPT: BIOS boot + ESP + ext4). `vmware-mount -p` runs inside
`appLoader` — the very same binary as the GUI — which makes it the meaningful A/B:

```
A) uninstalled : Failed to open disk: The specified file is not a virtual disk (16063)
B) installed   : [AppLoader] ...
                 Nr      Start       Size Type Id Sytem
                  1       2048       4096  GPT EE Unknown
                  2       6144    1048576  GPT EE Efi System
                  3    1054720   66054111  GPT EE Linux FS
```

`readelf -d` shows `[libfbimgshim.so]` as the first NEEDED of all four binaries,
`vmware-vmx` keeps `-rwsr-xr-x`, and every successful redirect is logged to
`/tmp/fbimg-shim.<uid>.log`. A full install → uninstall → install round trip was run:
after `uninstall` the image is rejected again with `not a virtual disk`, and the GUI patch
is reverted as well.

## 7. Licence

MIT — see [../LICENSE](../LICENSE).
