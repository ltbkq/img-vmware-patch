"""``argparse`` entry point for the ``img-vmware-patch`` command.

Exit codes (see CONTRACT.md):

* ``0`` - success
* ``1`` - general error (unaligned image without ``--force``, image too small,
  missing input file, existing output without ``-f``, I/O problems)
* ``2`` - argument error (argparse usage errors, invalid VM name)
* ``3`` - VMware compatibility problem (``create`` found no ``vmrun``)

``link`` and ``info`` never probe for VMware, so they work on machines without
Workstation installed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__, vmware
from .descriptor import (
    ADAPTER_TYPES,
    OutputError,
    adapter_type,
    build_descriptor,
    extent_line,
    write_text_file,
)
from .geometry import (
    SECTOR_SIZE,
    Geometry,
    ImageAlignmentError,
    ImageTooSmallError,
    compute_geometry,
)
from .vmx import DEFAULT_GUEST_OS, DEFAULT_RAM_MB, build_vmx

__all__ = [
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_USAGE",
    "EXIT_VMWARE",
    "REGISTER_ENV",
    "CommandError",
    "build_parser",
    "main",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VMWARE = 3

#: Set to ``0``/``false``/``no``/``off`` to let ``create`` skip the optional
#: ``vmrun -T ws register`` call (used by CI machines without/with VMware
#: where registration must not happen).
REGISTER_ENV = "IMG_VMWARE_PATCH_REGISTER"
_REGISTER_DISABLED = frozenset({"0", "false", "no", "off", "skip", "disabled"})

ADAPTER_CHOICES = tuple(ADAPTER_TYPES)
_INVALID_NAME_CHARS = (
    frozenset('/\\:*?"<>|')
    | frozenset(chr(code) for code in range(32))
    | frozenset(chr(127))
)


class CommandError(Exception):
    """Error carrying an exit code and an optional hint line."""

    def __init__(self, message: str, *, code: int = EXIT_ERROR, hint: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _human_size(size: int) -> str:
    if size < 1024:
        return "{0} B".format(size)
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        value /= 1024.0
        if value < 1024 or unit == "PiB":
            return "{0:.2f} {1}".format(value, unit)
    return "{0} B".format(size)


def _check_image(image: str) -> Path:
    path = Path(image)
    if not path.exists():
        raise CommandError("image file not found: {0}".format(path))
    if not path.is_file():
        raise CommandError("image path is not a regular file: {0}".format(path))
    return path


def _load_geometry(image: Path, *, force: bool, allow_force: bool) -> Geometry:
    try:
        size = image.stat().st_size
    except OSError as exc:
        raise CommandError("cannot read image size: {0}".format(exc)) from exc

    try:
        geometry = compute_geometry(size, force=force)
    except ImageAlignmentError as exc:
        if allow_force:
            hint = "use -f/--force to truncate the size to a 512-byte boundary"
        else:
            hint = (
                "this command has no --force; align the image or use "
                "link/create with -f/--force"
            )
        raise CommandError(str(exc), hint=hint) from exc
    except ImageTooSmallError as exc:
        raise CommandError(str(exc)) from exc

    if geometry.truncated:
        print(
            "warning: file size {0} is not a multiple of {1} bytes; "
            "truncated to {2} bytes ({3} sectors)".format(
                geometry.source_size, SECTOR_SIZE, geometry.file_size,
                geometry.total_sectors,
            ),
            file=sys.stderr,
        )
    return geometry


def _validate_name(name: str) -> str:
    if not str(name).strip():
        raise CommandError("VM name must not be empty", code=EXIT_USAGE)
    bad = sorted({char for char in name if char in _INVALID_NAME_CHARS})
    if bad:
        raise CommandError(
            "invalid VM name {0!r}: forbidden character(s) {1}".format(
                name, " ".join(repr(char) for char in bad)
            ),
            code=EXIT_USAGE,
            hint="avoid path separators and Windows-reserved characters",
        )
    return name


def _register_enabled() -> bool:
    raw = os.environ.get(REGISTER_ENV, "")
    return raw.strip().lower() not in _REGISTER_DISABLED


def _ensure_writable(descriptor_path: Path, vmx_path: Path, force: bool) -> None:
    for target in (descriptor_path, vmx_path):
        if target.is_dir():
            raise CommandError("output path is a directory: {0}".format(target))
        if target.exists() and not force:
            raise CommandError(
                "output file already exists: {0}".format(target),
                hint="use -f/--force to overwrite",
            )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_link(args: argparse.Namespace) -> int:
    """``link``: only write the VMDK descriptor."""
    image = _check_image(args.image)
    geometry = _load_geometry(image, force=args.force, allow_force=True)

    output = Path(args.output) if args.output else image.with_suffix(".vmdk")
    content = build_descriptor(image, geometry, adapter=args.adapter)
    try:
        written = write_text_file(output, content, force=args.force)
    except OutputError as exc:
        raise CommandError(str(exc), hint="use -f/--force to overwrite") from exc

    print("descriptor: {0}".format(written))
    print("image:      {0}".format(image))
    print("extent:     {0}".format(extent_line(geometry.total_sectors, image)))
    print("geometry:   {0}".format(geometry.describe()))
    print(
        "adapter:    {0} -> ddb.adapterType={1}".format(
            args.adapter, adapter_type(args.adapter)
        )
    )
    return EXIT_OK


def cmd_info(args: argparse.Namespace) -> int:
    """``info``: show image information and the computed geometry."""
    image = _check_image(args.image)
    geometry = _load_geometry(image, force=False, allow_force=False)
    size = geometry.source_size

    print("image:      {0}".format(image))
    print("size:       {0} bytes ({1})".format(size, _human_size(size)))
    print("sector:     {0} bytes".format(SECTOR_SIZE))
    print("sectors:    {0}".format(geometry.total_sectors))
    print("geometry:   {0}".format(geometry.describe()))
    print("chs:        {0}".format(geometry.chs_string))
    if geometry.remainder_sectors:
        print(
            "partial:    {0} sectors in the trailing incomplete cylinder".format(
                geometry.remainder_sectors
            )
        )
    return EXIT_OK


def cmd_create(args: argparse.Namespace) -> int:
    """``create``: write descriptor + .vmx, then register the VM via vmrun."""
    image = _check_image(args.image)
    geometry = _load_geometry(image, force=args.force, allow_force=True)
    name = _validate_name(args.name)

    vm_dir = Path(args.vm_dir)
    if vm_dir.exists() and not vm_dir.is_dir():
        raise CommandError("--dir is not a directory: {0}".format(vm_dir))

    descriptor_path = Path(args.output) if args.output else vm_dir / "{0}.vmdk".format(name)
    vmx_path = vm_dir / "{0}.vmx".format(name)
    _ensure_writable(descriptor_path, vmx_path, args.force)

    vm_dir.mkdir(parents=True, exist_ok=True)
    if descriptor_path.parent.resolve() != vm_dir.resolve():
        print(
            "warning: descriptor {0} is outside --dir; the .vmx references the "
            "bare name {1!r}".format(descriptor_path, descriptor_path.name),
            file=sys.stderr,
        )

    try:
        written_descriptor = write_text_file(
            descriptor_path,
            build_descriptor(image, geometry, adapter=args.adapter),
            force=args.force,
        )
        written_vmx = write_text_file(
            vmx_path,
            build_vmx(
                display_name=name,
                descriptor_name=descriptor_path.name,
                adapter=args.adapter,
                ram_mb=args.ram,
                guest_os=args.guest,
            ),
            force=args.force,
        )
    except OutputError as exc:
        raise CommandError(str(exc), hint="use -f/--force to overwrite") from exc

    print("descriptor: {0}".format(written_descriptor))
    print("vmx:        {0}".format(written_vmx))
    print("geometry:   {0}".format(geometry.describe()))
    print("extent:     {0}".format(extent_line(geometry.total_sectors, image)))

    vmrun = vmware.find_vmrun()
    if not vmrun:
        print("error: vmrun not found (VMware 未安装或不在 PATH 中)", file=sys.stderr)
        print(vmware.install_hint(), file=sys.stderr)
        print(
            "note: descriptor and .vmx were written; open it from the\n"
            "      VMware Workstation GUI (File > Open) or run it directly:\n"
            "      vmrun -T ws start {0} nogui".format(written_vmx),
            file=sys.stderr,
        )
        return EXIT_VMWARE

    print("vmrun:      {0}".format(vmrun))
    if not _register_enabled():
        print("registration: skipped ({0})".format(REGISTER_ENV))
        return EXIT_OK

    version = vmware.get_version(vmrun)
    if version:
        print("vmrun version: {0}".format(version))
    registered, detail = vmware.register_vm(written_vmx, vmrun=vmrun)
    if registered:
        print("registration: ok")
    else:
        print(
            "warning: vmrun register failed: {0}".format(detail or "unknown error"),
            file=sys.stderr,
        )
        print(
            "hint: the VM is not in the Workstation library; open it with\n"
            "      File > Open, or start it directly:\n"
            "      vmrun -T ws start {0} nogui".format(written_vmx),
            file=sys.stderr,
        )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def _positive_int(value: str) -> int:
    try:
        number = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid integer: {0!r}".format(value)) from None
    if number <= 0:
        raise argparse.ArgumentTypeError(
            "must be a positive number of MB, got {0}".format(number)
        )
    return number


def _non_empty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the ``img-vmware-patch`` argument parser (CLI contract)."""
    parser = argparse.ArgumentParser(
        prog="img-vmware-patch",
        description=(
            "Generate a VMDK descriptor for a raw disk image and optionally "
            "prepare a minimal VMware virtual machine around it."
        ),
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version="%(prog)s {0}".format(__version__),
    )
    subparsers = parser.add_subparsers(
        dest="command", metavar="<command>", required=True
    )

    link_parser = subparsers.add_parser(
        "link",
        help="仅生成 VMDK descriptor 文件",
        description="仅生成 VMDK descriptor 文件（不依赖 VMware）。",
    )
    link_parser.add_argument("image", metavar="IMAGE", help=".img 镜像文件路径")
    link_parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        default=None,
        help="输出 descriptor 路径（默认 <IMAGE 同目录同名>.vmdk）",
    )
    link_parser.add_argument(
        "-a",
        "--adapter",
        choices=ADAPTER_CHOICES,
        default="ide",
        help="磁盘适配器类型 {ide,sata,scsi}（默认 ide）",
    )
    link_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="覆盖已存在的输出文件，并容忍非 512 对齐的镜像",
    )
    link_parser.set_defaults(func=cmd_link)

    create_parser = subparsers.add_parser(
        "create",
        help="生成 descriptor 并创建/注册虚拟机",
        description="生成 descriptor 与 .vmx，并尝试用 vmrun 注册虚拟机。",
    )
    create_parser.add_argument("image", metavar="IMAGE", help=".img 镜像文件路径")
    create_parser.add_argument(
        "-n",
        "--name",
        metavar="NAME",
        required=True,
        help="虚拟机名称（displayName，同时作为 .vmx/.vmdk 文件名）",
    )
    create_parser.add_argument(
        "-m",
        "--ram",
        metavar="MB",
        type=_positive_int,
        default=DEFAULT_RAM_MB,
        help="内存大小 MB（默认 {0}）".format(DEFAULT_RAM_MB),
    )
    create_parser.add_argument(
        "-g",
        "--guest",
        metavar="GUEST",
        type=_non_empty,
        default=DEFAULT_GUEST_OS,
        help="guestOS 类型（默认 {0}）".format(DEFAULT_GUEST_OS),
    )
    create_parser.add_argument(
        "--dir",
        dest="vm_dir",
        metavar="PATH",
        required=True,
        help="虚拟机目录（.vmx 与 descriptor 所在目录）",
    )
    create_parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        default=None,
        help="输出 descriptor 路径（默认 <DIR>/<NAME>.vmdk，须与 .vmx 同目录）",
    )
    create_parser.add_argument(
        "-a",
        "--adapter",
        choices=ADAPTER_CHOICES,
        default="ide",
        help="磁盘适配器类型 {ide,sata,scsi}（默认 ide）",
    )
    create_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="覆盖已存在的输出文件，并容忍非 512 对齐的镜像",
    )
    create_parser.set_defaults(func=cmd_create)

    info_parser = subparsers.add_parser(
        "info",
        help="显示 .img 文件信息和计算出的几何参数",
        description="显示 .img 文件信息和计算出的几何参数（不依赖 VMware）。",
    )
    info_parser.add_argument("image", metavar="IMAGE", help=".img 镜像文件路径")
    info_parser.set_defaults(func=cmd_info)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point used by ``console_scripts`` and ``python -m``."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except CommandError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        if exc.hint:
            print("hint: {0}".format(exc.hint), file=sys.stderr)
        return exc.code
    except OutputError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        print("hint: use -f/--force to overwrite", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
