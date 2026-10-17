#!/bin/sh
#
# install-native.sh —— 让 VMware(Workstation) 原生直接启动裸 .img 磁盘镜像
#
# 做的事（可重复执行，幂等）：
#   1. 编译磁盘访问层 libfbimgshim.so；
#   2. 装到 /usr/local/lib 并 ldconfig（setuid 程序只能从受信缓存解析依赖）；
#   3. 给下面这些 VMware 二进制各打上 DT_NEEDED libfbimgshim.so：
#        vmware-vmx[-debug|-stats]  —— 真正开虚拟机、读写虚拟磁盘的进程
#        appLoader                  —— Workstation 图形界面本体
#                                      （“添加硬盘/更换磁盘/检索磁盘信息”向导
#                                        就在这个进程里校验磁盘）
#      于是该库成为这些二进制自身的依赖，只随它们加载，
#      不影响系统里任何其它进程（不需要 /etc/ld.so.preload，不需要环境变量）。
#
# 用法：  sudo sh vm/install-native.sh
#
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
SUDO=${SUDO:-sudo}
[ "$(id -u)" -eq 0 ] && SUDO=""
BUILD=$(mktemp -d)

# VMware 安装前缀探测：不同发行版/安装方式可能是 /usr/lib/vmware（发行版包）、
# /usr/local/vmware、/opt/vmware …… 可用 FBIMG_VMWARE_BIN 显式指定。
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

command -v patchelf >/dev/null || { echo "需要 patchelf：sudo apt install patchelf" >&2; exit 1; }
command -v gcc >/dev/null || { echo "需要 gcc" >&2; exit 1; }

echo "VMware 安装目录：$ROOT"

echo "==> 1/4 编译 libfbimgshim.so"
gcc -shared -fPIC -O2 -Wall -Wl,-soname,libfbimgshim.so \
    -o "$BUILD/libfbimgshim.so" "$HERE/fbimg-shim.c" -ldl

echo "==> 2/4 安装并刷新库缓存"
$SUDO install -m 0755 "$BUILD/libfbimgshim.so" /usr/local/lib/libfbimgshim.so
$SUDO ldconfig
rm -rf "$BUILD"

# 要注入的宿主：vmware-vmx 系列 + appLoader（图形界面）
TARGETS="$BIN/vmware-vmx $BIN/vmware-vmx-debug $BIN/vmware-vmx-stats $BIN/appLoader"

inject_one() {
    t=$1
    [ -x "$t" ] || { echo "    - 跳过（不存在）：$t"; return 0; }
    case "$t" in *.orig) return 0 ;; esac

    # 首次执行时留一份原始备份；以后每次都从 .orig 重打，避免叠加多条 NEEDED
    [ -f "$t.orig" ] || $SUDO cp -a "$t" "$t.orig"

    if readelf -d "$t.orig" 2>/dev/null | grep -q 'libfbimgshim\.so'; then
        echo "    - 已注入：$t"
        return 0
    fi

    # 不能就地写正在运行的二进制（ETXTBSY），生成新文件再 rename 顶掉
    new="$t.new.$$"
    $SUDO cp -a "$t.orig" "$new"
    $SUDO chmod u+w "$new"
    $SUDO patchelf --add-needed libfbimgshim.so "$new"
    # 权限（含 vmware-vmx 的 setuid 位）照抄原始文件
    $SUDO chmod --reference="$t.orig" "$new"
    $SUDO mv -f "$new" "$t"
    echo "    + 已注入：$t  (备份：$t.orig)"
}

echo "==> 3/4 把依赖注入 VMware 二进制（各留一份 .orig 备份）"
for t in $TARGETS; do
    inject_one "$t"
done

# 图形界面的文件选择框还要能“列出” .img（DISKLIB 认盘由上面的库负责），
# 否则在“添加硬盘 / 更换磁盘”里根本看不到 .img 文件。
echo "==> 4/4 界面过滤补丁（让“选择磁盘”对话框列出 .img）"
if command -v python3 >/dev/null && [ -f "$HERE/patch-vmware-ui-imgfilter.py" ]; then
    python3 "$HERE/patch-vmware-ui-imgfilter.py" --root "$ROOT" \
        || echo "    ! 界面过滤补丁失败（不影响 DISKLIB 直启）"
else
    echo "    - 跳过（缺 python3 或补丁脚本）"
fi

echo
echo "完成。验证："
echo "  readelf -d $BIN/vmware-vmx  | grep -m1 NEEDED   # 应出现 libfbimgshim.so"
echo "  readelf -d $BIN/appLoader   | grep -m1 NEEDED   # 应出现 libfbimgshim.so"
echo "  然后把 .vmx 里的磁盘写成裸 img 文件名即可："
echo "      scsi0:0.fileName = \"你的镜像.img\""
echo "      scsi0:0.deviceType = \"disk\""
echo "  VMware 会在打开时于同目录自动生成隐藏描述块 .fbimg-desc（及 .fbimg-data 链接），"
echo "  真正的磁盘读/写都发生在 .img 本体上（offset 0，逐字节不变）。"
echo
echo "  注意：图形界面进程（appLoader）要重启 VMware 才会载入新库。"
echo "  排查：每次成功接管都会写一行到 /tmp/fbimg-shim.<uid>.log"
