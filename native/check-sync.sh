#!/bin/sh
#
# check-sync.sh —— 校验自包含一键脚本里内嵌的源码与独立文件一致
#
# install-vmware-img.sh 为了让“拷一个文件就能在别的机器跑”而内嵌了一份
# fbimg-shim.c / patch-vmware-ui-imgfilter.py。本脚本把内嵌副本抠出来做 diff，
# 防止两边悄悄漂移。发布前跑一下即可。
#
# 用法： sh native/check-sync.sh
#
set -e

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

awk '/fbimg-shim\.c" <</ {f=1; next} /^FBIMG_C_EOF$/ {f=0} f' \
    "$HERE/install-vmware-img.sh" > "$TMP/fbimg-shim.c"
awk '/patch-vmware-ui-imgfilter\.py" <</ {f=1; next} /^FBIMG_PY_EOF$/ {f=0} f' \
    "$HERE/install-vmware-img.sh" > "$TMP/patch-vmware-ui-imgfilter.py"

rc=0
diff -u "$HERE/fbimg-shim.c" "$TMP/fbimg-shim.c" || rc=1
diff -u "$HERE/patch-vmware-ui-imgfilter.py" "$TMP/patch-vmware-ui-imgfilter.py" || rc=1

if [ "$rc" -eq 0 ]; then
    echo "OK：一键脚本内嵌源码与独立文件完全一致"
else
    echo "不一致：上面是 diff（内嵌副本需要重新生成）" >&2
fi
exit "$rc"
