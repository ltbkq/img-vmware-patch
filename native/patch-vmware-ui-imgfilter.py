#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch-vmware-ui-imgfilter.py —— 让 VMware 图形界面的文件选择框也接受 .img

问题
----
Workstation 的文件选择框按后缀过滤文件。“虚拟磁盘”这一档只挂了两个 glob：
        *.vmdk   *.VMDK
所以裸镜像 xxx.img 根本不出现在列表里（ISO / 软盘那两档反而本来就带 *.img）。
注意：能列出 .img 只是“选得到文件”，真正让 VMware 认这个盘还得靠
libfbimgshim.so（见 install-native.sh / fbimg-shim.c），两者缺一不可。

改的几处（等长或更短的原地改写，全部可逆）
----------------------------------------
1) libvmwareui.so 里构造过滤表的那段代码：把“虚拟磁盘”一档的第二个 glob
   "*.VMDK" 改指到同一 .rodata 里已有的 "*.img"（只改 lea 的 4 字节位移，
   不影响任何别的过滤器；代价只是不再列出大写 .VMDK，实际用不到）。
2) 把“虚拟磁盘”“ISO 映像”两档的英文标签补上 *.img（槽位内更短，安全）。
3) messages/<locale>/vmware.vmsg 里同样两档的本地化文案补上 *.img
   （本机只有 zh_CN，界面是中文时看到的就是它）。

用法:
    sudo python3 patch-vmware-ui-imgfilter.py                 # 打补丁（自动探测 VMware 目录）
    sudo python3 patch-vmware-ui-imgfilter.py --status        # 只报告当前状态
    sudo python3 patch-vmware-ui-imgfilter.py --revert        # 还原
    sudo python3 patch-vmware-ui-imgfilter.py --root /opt/vmware   # 指定安装前缀

不写死路径：VMware 装在 /usr/lib/vmware、/usr/local/vmware、/opt/vmware 等
任意前缀都能用；也可以用 --lib / --messages-glob 单独指定。
"""
import argparse
import glob
import os
import re
import shutil
import struct
import subprocess
import sys

BAK_SUFFIX = ".bak-imgfilter"

# VMware 安装前缀候选（--root 可覆盖）
ROOT_CANDIDATES = ("/usr/lib/vmware", "/usr/local/vmware", "/opt/vmware")
DEFAULT_ROOT = ROOT_CANDIDATES[0]

# 下面两个由 resolve_paths() 依据 --root / --lib / --messages-glob 决定
LIB = os.path.join(DEFAULT_ROOT, "lib/libvmwareui.so/libvmwareui.so")
MSG_GLOB = os.path.join(DEFAULT_ROOT, "messages/*/vmware.vmsg")

# 该补丁依赖的字符串（本版 VMware 里各只出现一次）
GLOB_OLD = b"*.VMDK\x00"
GLOB_NEW = b"*.img\x00"

# .so 内嵌英文标签：原文 -> 新文（新文必须不比原文长，原地覆盖）
SO_LABELS = [
    (b"Virtual Disks (*.vmdk)", b"Disks (*.vmdk, *.img)"),
    (b"ISO Images (*.iso)", b"ISO (*.iso, *.img)"),
]

# 本地化文案要补 *.img 的那些条目
MSG_IDS = ("msg.cui.filter.Disks", "msg.cui.filter.ISOImages")
MSG_RE = re.compile(r'^((?:' + "|".join(re.escape(i) for i in MSG_IDS) +
                    r')\s*=\s*")(.*)("\s*)$')


def sections(path):
    out = subprocess.run(["readelf", "-S", "-W", path],
                         capture_output=True, text=True).stdout
    sects = []
    for line in out.splitlines():
        m = re.match(r"\s*\[\s*\d+\]\s+(\S+)\s+(\S+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)", line)
        if m:
            name, _typ, addr, off, size = m.groups()
            sects.append((name, int(addr, 16), int(off, 16), int(size, 16)))
    return sects


def off2va(sects, off):
    for _n, a, o, s in sects:
        if s and o <= off < o + s:
            return a + (off - o)
    return None


def find_all(data, needle):
    out, i = [], data.find(needle)
    while i >= 0:
        out.append(i)
        i = data.find(needle, i + 1)
    return out


def lea_refs(data, sects, va_target):
    """找出 .text 里所有“RIP 相对 lea 且目标为 va_target”的指令文件偏移"""
    text = None
    for name, a, o, s in sects:
        if name == ".text":
            text = (a, o, s)
    if not text:
        raise SystemExit("错误：没有 .text 段")
    tva, toff, tsize = text
    blob = data[toff:toff + tsize]
    out = []
    for m in re.finditer(rb"\x48\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]", blob):
        insn_off = toff + m.start()
        insn_va = tva + m.start()
        disp = struct.unpack_from("<i", data, insn_off + 3)[0]
        if insn_va + 7 + disp == va_target:
            out.append(insn_off)
    return out


def patch_lib(data, sects):
    """返回 (新数据 或 None, 状态字符串)"""
    hits = find_all(data, GLOB_NEW)
    if not hits:
        raise SystemExit("错误：库里找不到字符串 '*.img'，版本不匹配，放弃")
    va_img = off2va(sects, hits[0])

    olds = find_all(data, GLOB_OLD)
    if len(olds) != 1:
        raise SystemExit(f"错误：'*.VMDK' 出现 {len(olds)} 次（预期 1 次），版本不匹配，放弃")
    va_old = off2va(sects, olds[0])

    b = bytearray(data)
    changed = False

    found = lea_refs(data, sects, va_old)
    if not found:
        if not lea_refs(data, sects, va_img):
            raise SystemExit("错误：既没有引用 '*.VMDK' 也没有引用 '*.img' 的 lea，版本不匹配，放弃")
        print("  - 过滤表 glob 已经改过，跳过")
    else:
        if len(found) != 1:
            raise SystemExit(f"错误：引用 '*.VMDK' 的 lea 有 {len(found)} 条（预期 1 条），放弃")
        insn_off = found[0]
        insn_va = off2va(sects, insn_off)
        struct.pack_into("<i", b, insn_off + 3, va_img - (insn_va + 7))
        print(f"  + 过滤表 glob: '*.VMDK' -> '*.img'  (指令 @ 0x{insn_va:x})")
        changed = True

    for old, new in SO_LABELS:
        assert len(new) <= len(old), f"新标签比原标签长：{new!r}"
        loc = find_all(data, old)
        if len(loc) == 1:
            b[loc[0]:loc[0] + len(old)] = new.ljust(len(old), b"\x00")
            print(f"  + 英文标签: {old.decode()} -> {new.decode()}")
            changed = True
        elif len(loc) == 0:
            print(f"  - 英文标签已是新样子（或不存在）: {old.decode()}")
        else:
            print(f"  ! 英文标签原文出现 {len(loc)} 次，未改: {old.decode()}")

    return (bytes(b), "ok") if changed else (None, "already")


def patch_msgs():
    for path in sorted(glob.glob(MSG_GLOB)):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        changed = False
        for i, line in enumerate(lines):
            m = MSG_RE.match(line.rstrip("\n"))
            if not m:
                continue
            text = m.group(2)
            if "*.img" in text:
                print(f"  - {path}：{m.group(1).split('=')[0].strip()} 已含 *.img，跳过")
                continue
            # 塞进括号里： "xxx (*.vmdk)" -> "xxx (*.vmdk, *.img)"
            if text.rstrip().endswith(")"):
                new_text = f"{text.rstrip()[:-1].rstrip()}, *.img)"
            else:
                new_text = f"{text}, *.img"
            lines[i] = f'{m.group(1)}{new_text}{m.group(3)}\n'
            print(f"  + {path}:\n      - {line.strip()}\n      + {lines[i].strip()}")
            changed = True
        if not changed:
            continue
        if not os.path.exists(path + BAK_SUFFIX):
            shutil.copy2(path, path + BAK_SUFFIX)
            print(f"  + 备份 -> {path}{BAK_SUFFIX}")
        tmp = path + ".new.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)


def revert_all():
    for t in [LIB] + sorted(glob.glob(MSG_GLOB)):
        bak = t + BAK_SUFFIX
        if not os.path.exists(bak):
            print(f"  - {t}：没有备份，跳过")
            continue
        tmp = t + ".revert.%d" % os.getpid()
        shutil.copy2(bak, tmp)
        if os.path.exists(t):
            shutil.copymode(t, tmp)
        os.replace(tmp, t)
        print(f"  + 已还原 {t}")


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="让 VMware 图形界面的“选择磁盘”对话框也能列出 .img",
        add_help=True)
    p.add_argument("--revert", action="store_true", help="还原成原版")
    p.add_argument("--status", action="store_true", help="只报告状态，不做任何修改")
    p.add_argument("--root", metavar="DIR",
                   help="VMware 安装目录（默认自动探测：%s）" % " / ".join(ROOT_CANDIDATES))
    p.add_argument("--lib", metavar="PATH",
                   help="libvmwareui.so 路径（默认 <root>/lib/libvmwareui.so/libvmwareui.so）")
    p.add_argument("--messages-glob", metavar="GLOB",
                   help="本地化文案通配（默认 <root>/messages/*/vmware.vmsg）")
    return p.parse_args(argv)


def resolve_paths(args):
    """按 --root/--lib/--messages-glob 定位目标文件；返回探测到的根目录"""
    global LIB, MSG_GLOB
    root = args.root
    if not root and not args.lib:
        for c in ROOT_CANDIDATES:
            if os.path.isdir(c):
                root = c
                break
    if args.lib:
        LIB = args.lib
    elif root:
        cands = (os.path.join(root, "lib/libvmwareui.so/libvmwareui.so"),
                 os.path.join(root, "lib/libvmwareui.so"))
        LIB = next((c for c in cands if os.path.exists(c)), cands[0])
    if args.messages_glob:
        MSG_GLOB = args.messages_glob
    elif root:
        MSG_GLOB = os.path.join(root, "messages/*/vmware.vmsg")
    return root


def msg_has_img(path):
    """本地化文案里“虚拟磁盘 / ISO 映像”两档是否已含 *.img"""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    for line in lines:
        m = MSG_RE.match(line)
        if m and "*.img" in m.group(2):
            return True
    return False


def report_status():
    print(f"  libvmwareui.so : {LIB}")
    if os.path.exists(LIB):
        data = open(LIB, "rb").read()
        sects = sections(LIB)
        try:
            va_old = off2va(sects, find_all(data, GLOB_OLD)[0])
            va_img = off2va(sects, find_all(data, GLOB_NEW)[0])
        except IndexError:
            print("    ! 找不到 '*.VMDK' / '*.img' 字符串，版本可能不匹配")
        else:
            patched = any(lea_refs(data, sects, va_img)) and not any(lea_refs(data, sects, va_old))
            print(f"    状态        : {'已打补丁（过滤档含 *.img）' if patched else '原版（过滤档只有 *.vmdk）'}")
        print(f"    备份        : {LIB + BAK_SUFFIX if os.path.exists(LIB + BAK_SUFFIX) else '（无）'}")
    else:
        print("    ! 文件不存在")
    for path in sorted(glob.glob(MSG_GLOB)):
        print(f"  {path} : {'已含 *.img' if msg_has_img(path) else '未含 *.img'}")


def main():
    args = parse_args(sys.argv[1:])
    resolve_paths(args)

    if args.status:
        print("界面过滤补丁状态：")
        report_status()
        return

    if os.geteuid() != 0:
        raise SystemExit("需要 root：sudo python3 %s" % os.path.basename(__file__))

    if args.revert:
        print("还原 VMware 界面过滤补丁：")
        revert_all()
        return

    if not os.path.exists(LIB):
        raise SystemExit(f"找不到 {LIB}（没装 VMware Workstation？可用 --lib 指定）")

    print("1) libvmwareui.so（过滤表 glob + 英文标签）")
    data = open(LIB, "rb").read()
    sects = sections(LIB)
    if not os.path.exists(LIB + BAK_SUFFIX):
        shutil.copy2(LIB, LIB + BAK_SUFFIX)
        print(f"  + 备份原版 -> {LIB}{BAK_SUFFIX}")
    new, status = patch_lib(data, sects)
    if status == "already":
        print("  已经打过补丁，跳过")
    else:
        tmp = LIB + ".new.%d" % os.getpid()
        with open(tmp, "wb") as f:
            f.write(new)
        shutil.copymode(LIB, tmp)
        os.replace(tmp, LIB)
        print(f"  已写入 {LIB}")

    print("2) 界面文案 vmware.vmsg（本地化标签）")
    patch_msgs()

    print("完成（重启 VMware 后生效）")


if __name__ == "__main__":
    main()
