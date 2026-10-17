#!/bin/sh
#
# uninstall-native.sh —— 还原 VMware，移除裸 img 直启支持
#
set -e

SUDO=${SUDO:-sudo}
[ "$(id -u)" -eq 0 ] && SUDO=""

# 与 install-native.sh 相同的安装前缀探测（可用 FBIMG_VMWARE_BIN 指定）
detect_bin() {
    for r in /usr/lib/vmware /usr/local/vmware /opt/vmware; do
        [ -x "$r/bin/vmware-vmx" ] && { printf '%s\n' "$r/bin"; return 0; }
    done
    if command -v vmware >/dev/null 2>&1; then
        t=$(readlink -f "$(command -v vmware)" 2>/dev/null) || t=""
        if [ -n "$t" ] && [ -x "$(dirname "$t")/vmware-vmx" ]; then
            printf '%s\n' "$(dirname "$t")"; return 0
        fi
    fi
    return 1
}
BIN=${FBIMG_VMWARE_BIN:-$(detect_bin)} || {
    echo "找不到 VMware 的 bin 目录（可用 FBIMG_VMWARE_BIN=/path/to/vmware/bin 指定）" >&2
    exit 1
}
ROOT=$(cd "$BIN/.." && pwd)
echo "VMware 安装目录：$ROOT"

restore_one() {
    t=$1
    if [ -f "$t.orig" ]; then
        # rename 顶掉，避免正在运行时 ETXTBSY
        NEW="$t.new.$$"
        $SUDO cp -a "$t.orig" "$NEW"
        $SUDO chmod --reference="$t.orig" "$NEW"
        $SUDO mv -f "$NEW" "$t"
        echo "已还原 $t（备份 $t.orig 仍然保留）"
    else
        echo "没有找到 $t.orig，跳过还原"
    fi
}

for t in "$BIN/vmware-vmx" "$BIN/vmware-vmx-debug" "$BIN/vmware-vmx-stats" "$BIN/appLoader"; do
    restore_one "$t"
done

$SUDO rm -f /usr/local/lib/libfbimgshim.so
$SUDO ldconfig
echo "已移除 libfbimgshim.so"

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$HERE/patch-vmware-ui-imgfilter.py" ]; then
    echo "还原界面过滤补丁："
    python3 "$HERE/patch-vmware-ui-imgfilter.py" --revert --root "$ROOT" \
        || echo "  ! 还原失败，可手动从 *.bak-imgfilter 恢复"
fi

echo
echo "注意：.img 本体不受任何影响，仍是可直接 dd / qemu-img / 烧录的普通磁盘镜像。"
echo "启动过 VMware 的镜像目录里可能残留隐藏描述块，可一并删掉："
echo "  rm -f <镜像目录>/.fbimg-desc <镜像目录>/.fbimg-data"
