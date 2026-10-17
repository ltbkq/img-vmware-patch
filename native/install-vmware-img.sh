#!/bin/bash
# ============================================================================
#  install-vmware-img.sh —— 一条命令让 VMware 直接使用裸 .img 磁盘镜像
#
#  给 Linux 版 VMware（Workstation / Player）补上“认识裸镜像”的能力：
#  把 xxx.img 直接当虚拟磁盘开机，全程零转换，.img 本体逐字节保持原样
#  （仍可 dd / qemu-img / 烧 U 盘 / 给 qemu 用）。
#
#  两件事，缺一不可：
#    1) 磁盘访问层 libfbimgshim.so —— 以 DT_NEEDED 注入
#         vmware-vmx / vmware-vmx-debug / vmware-vmx-stats / appLoader
#       VMware 打开 xxx.img 时改开同目录自动生成的隐藏描述块 .fbimg-desc，
#       所有读写仍落在 .img 本体的 offset 0。
#       appLoader 是 Workstation 的图形界面本体，“添加硬盘 / 更换磁盘 /
#       检索磁盘信息”都在它里面校验磁盘，漏了它界面就会报
#       “The file specified is not a virtual disk”。
#    2) 界面过滤补丁 —— 让“选择磁盘”对话框的文件列表能看见并选中 .img
#       （原本只挂 *.vmdk）。
#
#  本脚本自带全部源码（C + Python），单文件即可拷到别的机器运行；
#  幂等，可反复执行；所有被改动的二进制都留 .orig 备份，随时可还原。
#
#  用法：
#    sudo ./install-vmware-img.sh                    安装（自动探测 VMware 目录）
#    sudo ./install-vmware-img.sh --img disk.img     安装并就地自检这张镜像
#    sudo ./install-vmware-img.sh uninstall          还原（含界面补丁）
#         ./install-vmware-img.sh check              只看状态（不需要 root）
#         ./install-vmware-img.sh selftest disk.img  用 VMware 的 DISKLIB 读分区表
#    sudo ./install-vmware-img.sh --root /opt/vmware install
#
#  常用选项：--root DIR  --bin DIR  --img FILE  --no-apt  -y
# ============================================================================
set -euo pipefail

VERSION="1.0.0"
PROG=${0##*/}

# ---------------------------------------------------------------- 输出
if [ -t 1 ]; then
    c_r=$'\033[0m'; c_ok=$'\033[1;32m'; c_in=$'\033[1;36m'; c_wn=$'\033[1;33m'; c_er=$'\033[1;31m'
else
    c_r=""; c_ok=""; c_in=""; c_wn=""; c_er=""
fi
log()  { printf '%s==>%s %s\n' "$c_in" "$c_r" "$*"; }
ok()   { printf '%s  +%s %s\n' "$c_ok" "$c_r" "$*"; }
warn() { printf '%s  !%s %s\n' "$c_wn" "$c_r" "$*" >&2; }
die()  { printf '%s错误：%s %s\n' "$c_er" "$c_r" "$*" >&2; exit 1; }

usage() {
    sed -n '3,31p' "$0" | sed -e 's/^# \{0,1\}//' -e 's/^#\{1,\}$//'
}

# ---------------------------------------------------------------- 参数
MODE=""
OPT_ROOT=""
OPT_BIN=""
OPT_IMG=""
ALLOW_APT=1

while [ $# -gt 0 ]; do
    case $1 in
        install|uninstall|check|selftest) MODE=$1 ;;
        --root)      shift; OPT_ROOT=${1:?--root 需要目录} ;;
        --root=*)    OPT_ROOT=${1#*=} ;;
        --bin)       shift; OPT_BIN=${1:?--bin 需要目录} ;;
        --bin=*)     OPT_BIN=${1#*=} ;;
        --img)       shift; OPT_IMG=${1:?--img 需要文件} ;;
        --img=*)     OPT_IMG=${1#*=} ;;
        --no-apt)    ALLOW_APT=0 ;;
        -y|--yes)    : ;;
        -h|--help)   usage; exit 0 ;;
        -V|--version) echo "$PROG $VERSION"; exit 0 ;;
        -*)          die "未知选项：$1（-h 看帮助）" ;;
        *)           # 裸参数：selftest 的镜像路径
                     if [ -z "$OPT_IMG" ] && [ -e "$1" ]; then OPT_IMG=$1
                     else die "未知参数：$1（-h 看帮助）"; fi ;;
    esac
    shift
done
[ -n "$MODE" ] || MODE=install

# ---------------------------------------------------------------- 工具
SRCDIR=""
TMPBASE=""
cleanup() { [ -n "$TMPBASE" ] && rm -rf "$TMPBASE" || true; }
trap cleanup EXIT

have() { command -v "$1" >/dev/null 2>&1; }

need_root() {
    [ "$(id -u)" -eq 0 ] || die "需要 root：sudo $PROG $*"
}

detect_bin() {
    if [ -n "$OPT_BIN" ]; then printf '%s\n' "$OPT_BIN"; return 0; fi
    if [ -n "$OPT_ROOT" ]; then
        [ -x "$OPT_ROOT/bin/vmware-vmx" ] || return 1
        printf '%s\n' "$OPT_ROOT/bin"; return 0
    fi
    # 常见安装前缀（发行版包 / 官方 bundle 装到别处的情况都覆盖）
    for r in /usr/lib/vmware /usr/local/vmware /opt/vmware; do
        [ -x "$r/bin/vmware-vmx" ] && { printf '%s\n' "$r/bin"; return 0; }
    done
    # 退路：顺着 vmware 命令反查（/usr/bin/vmware 通常软链到 appLoader）
    if have vmware; then
        local t; t=$(readlink -f "$(command -v vmware)" 2>/dev/null || true)
        if [ -n "$t" ] && [ -x "$(dirname "$t")/vmware-vmx" ]; then
            printf '%s\n' "$(dirname "$t")"; return 0
        fi
    fi
    return 1
}

PKG_MGR=""
detect_pkg_mgr() {
    local m
    for m in apt-get dnf yum pacman zypper; do
        if have "$m"; then PKG_MGR=$m; return 0; fi
    done
    return 1
}

ensure_tools() {
    local missing=() t
    have gcc        || missing+=(gcc)
    have patchelf   || missing+=(patchelf)
    have python3    || missing+=(python3)
    have readelf    || missing+=(binutils)
    [ ${#missing[@]} -gt 0 ] || return 0

    [ "$ALLOW_APT" = 1 ] || die "缺少工具：${missing[*]}（--no-apt 下请自行安装）"
    detect_pkg_mgr || die "缺少工具：${missing[*]}，且未找到可用的包管理器"
    log "安装依赖：${missing[*]}（$PKG_MGR）"
    case $PKG_MGR in
        apt-get)          apt-get update -qq && apt-get install -y "${missing[@]}" ;;
        dnf|yum)          "$PKG_MGR" install -y "${missing[@]}" ;;
        pacman)           pacman -Sy --noconfirm "${missing[@]}" ;;
        zypper)           zypper --non-interactive install "${missing[@]}" ;;
        *)                die "不支持的包管理器：$PKG_MGR" ;;
    esac
    for t in gcc patchelf python3 readelf; do
        have "$t" || die "依赖 $t 仍不可用"
    done
}

vmware_running() {
    pgrep -x vmware-vmx >/dev/null 2>&1 || pgrep -x appLoader >/dev/null 2>&1
}

TARGET_NAMES="vmware-vmx vmware-vmx-debug vmware-vmx-stats appLoader"

# ---------------------------------------------------------------- 内嵌源码
# 下面两个 heredoc 与仓库里的 fbimg-shim.c / patch-vmware-ui-imgfilter.py
# 是同一份内容（native/check-sync.sh 会校验，避免两边漂移）。
extract_sources() {
    TMPBASE=$(mktemp -d "${TMPDIR:-/tmp}/vmware-img.XXXXXX")
    SRCDIR=$TMPBASE
    cat > "$SRCDIR/fbimg-shim.c" <<'FBIMG_C_EOF'
/*
 * fbimg-shim.c —— 让 VMware 直接启动裸 .img 镜像的磁盘访问层
 *
 * 背景
 * ----
 * VMware 的磁盘引擎（vmware-vmx 内的 DISKLIB）只接受 vmdk 容器：它打开磁盘时先
 * 读文件开头当 "descriptor（描述块）" 解析出 extent 行，再打开 extent 指向的数据文件。
 * 直接指向裸 .img 会报 "The file specified is not a virtual disk"。
 *
 * 做法（改造 VMware 的磁盘访问入口）
 * --------------------------------
 * 在 open()/openat() 处拦一刀：
 *
 *   open("<...>/xxx.img")
 *      -> 同目录的隐藏描述文件 ".fbimg-desc" 不存在时先按 img 实际大小现生成
 *         （VMware 看到的就是一个合法 vmdk 描述块）
 *      -> 改为打开该描述文件；其余路径原样放行。
 *
 * 描述块里的 extent 指向同目录的隐藏符号链接 ".fbimg-data"，它指向真正的 xxx.img。
 * 于是：
 *   * VMware 解析到的是我们给的描述块（它以为在开 vmdk）；
 *   * VMware 之后所有的磁盘读 / 写都发生在 xxx.img 本体上（offset 0，逐字节不变）；
 *   * VMware 对描述块的重写 / 截断只落在 ~350 字节的隐藏描述文件上，永远碰不到 img。
 *
 * 生效条件（重要）
 * --------------
 *   1) 进程属于 VMware：可执行文件路径里带 /vmware/（覆盖 /usr/lib/vmware、
 *      /usr/local/vmware、/opt/vmware 等各种安装前缀），或名字命中已知列表
 *      （vmware-vmx[-debug|-stats]、appLoader、vmrun、vmcli、vmrest …），
 *      或显式设置环境变量 FBIMG_ENABLE=1（/etc/ld.so.preload 兜底方式）。
 *      注意 —— 图形界面（“添加硬盘 / 更换磁盘 / 检索磁盘信息”）跑在 appLoader
 *      进程里，vmware-vmx 只管真正开虚拟机，两个都得挂上本库，缺一个就会分别
 *      在“向导里选盘”或“开机时”报 not a virtual disk；
 *   2) 被打开的路径以 ".img" 结尾，且同目录能找到 / 能生成 ".fbimg-desc"。
 *   对系统里其它任何程序 / 其它文件都是完全透明的空操作。
 *   每次真正接管都会记一行到 /tmp/fbimg-shim.<uid>.log；
 *   FBIMG_DEBUG=1 时连未接管的 open 也记录（排查用）。
 *
 * 加载方式（原生集成）
 * ------------------
 *   patchelf --add-needed libfbimgshim.so /usr/lib/vmware/bin/vmware-vmx
 *   patchelf --add-needed libfbimgshim.so /usr/lib/vmware/bin/appLoader
 * 这样本库成为这些二进制自身的依赖，只随它们加载，不碰系统里其它进程，
 * 也不再需要 /etc/ld.so.preload。
 *
 * 编译：
 *   gcc -shared -fPIC -O2 -Wall -o fbimg-shim.so fbimg-shim.c -ldl
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <unistd.h>

#define DESC_NAME ".fbimg-desc"
#define DATA_NAME ".fbimg-data"

static int (*real_open)(const char *, int, ...);
static int (*real_open64)(const char *, int, ...);
static int (*real_openat)(int, const char *, int, ...);
static int (*real_openat64)(int, const char *, int, ...);
static int (*real_open_2)(const char *, int);
static int (*real_open64_2)(const char *, int);
static int (*real_openat_2)(int, const char *, int);
static int (*real_openat64_2)(int, const char *, int);
static int (*real_creat)(const char *, mode_t);
static int (*real_creat64)(const char *, mode_t);
static int (*real_rename)(const char *, const char *);
static int (*real_renameat)(int, const char *, int, const char *);
static int (*real_renameat2)(int, const char *, int, const char *, unsigned int);
static int (*real_unlink)(const char *);
static int (*real_unlinkat)(int, const char *, int);
static int (*real_truncate)(const char *, off_t);

static int g_init_done;
static int g_debug;
static int g_enabled;

static void shim_debug(const char *fmt, const char *a, const char *b)
{
    if (!g_debug)
        return;
    /* 文件名带真实 uid：vmware-vmx 是 setuid root，但 getuid() 仍是启动它的用户，
       这样图形界面进程（普通用户）和被 setuid 的 vmware-vmx 能写同一个日志文件。 */
    char logp[64];
    snprintf(logp, sizeof(logp), "/tmp/fbimg-shim.%u.log", (unsigned)getuid());
    int fd = syscall(SYS_openat, AT_FDCWD, logp,
                     O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0)
        return;
    char buf[PATH_MAX * 2 + 128];
    int n = snprintf(buf, sizeof(buf), "[shim pid=%d] ", (int)getpid());
    n += snprintf(buf + n, sizeof(buf) - n, fmt, a, b);
    n += snprintf(buf + n, sizeof(buf) - n, "\n");
    syscall(SYS_write, fd, buf, (size_t)n);
    syscall(SYS_close, fd);
}

/* 成功接管时无条件记一行（不依赖 FBIMG_DEBUG），方便排查
   “为什么图形界面认不出 .img / 为什么能认出来”。 */
static void shim_log_redirect(const char *path, const char *np)
{
    int keep = g_debug;
    g_debug = 1;
    shim_debug("REDIRECT %s -> %s", path, np);
    g_debug = keep;
}

/* 兜底：dlsym 拿不到符号时直接走系统调用，绝不让宿主机任何程序崩掉 */
static int raw_open(const char *path, int flags, mode_t mode)
{
    return (int)syscall(SYS_openat, AT_FDCWD, path, flags, mode);
}

static void init_once(void)
{
    if (g_init_done)
        return;
    g_init_done = 1;

    real_open      = dlsym(RTLD_NEXT, "open");
    real_open64    = dlsym(RTLD_NEXT, "open64");
    real_openat    = dlsym(RTLD_NEXT, "openat");
    real_openat64  = dlsym(RTLD_NEXT, "openat64");
    real_open_2    = dlsym(RTLD_NEXT, "__open_2");
    real_open64_2  = dlsym(RTLD_NEXT, "__open64_2");
    real_openat_2  = dlsym(RTLD_NEXT, "__openat_2");
    real_openat64_2 = dlsym(RTLD_NEXT, "__openat64_2");
    real_creat     = dlsym(RTLD_NEXT, "creat");
    real_creat64   = dlsym(RTLD_NEXT, "creat64");
    real_rename    = dlsym(RTLD_NEXT, "rename");
    real_renameat  = dlsym(RTLD_NEXT, "renameat");
    real_renameat2 = dlsym(RTLD_NEXT, "renameat2");
    real_unlink    = dlsym(RTLD_NEXT, "unlink");
    real_unlinkat  = dlsym(RTLD_NEXT, "unlinkat");
    real_truncate  = dlsym(RTLD_NEXT, "truncate");

    const char *d = getenv("FBIMG_DEBUG");
    g_debug = (d && d[0] == '1') ? 1 : 0;

    const char *e = getenv("FBIMG_ENABLE");
    g_enabled = (e && e[0] == '1') ? 1 : 0;

    /* 原生集成：本库被 patch 进 VMware 二进制自身的 DT_NEEDED，只随它们加载。
     *
     * 需要接管的进程：
     *   vmware-vmx[-debug|-stats] —— 真正跑虚拟机、打开虚拟磁盘的进程；
     *   appLoader                 —— Workstation 图形界面本体：文件管理器里
     *                                “添加硬盘 / 更换磁盘 / 检索磁盘信息” 都是
     *                                在这个进程里调 DISKLIB，所以它也必须生效，
     *                                否则界面会报 “The file specified is not a
     *                                virtual disk / 无法检索磁盘信息”；
     *   其余 appLoader 形态的 vmware* / vmrun / vmcli / vmrest 工具同理。
     *
     * 判定规则：可执行文件路径里带 /vmware/，或名字命中已知列表。
     * 用路径片段而不是写死 /usr/lib/vmware/，这样把本库拷到别的机器
     * （VMware 装在 /usr/local/vmware、/opt/vmware 等任意前缀）也一样生效。 */
    if (!g_enabled) {
        char exe[PATH_MAX];
        ssize_t k = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
        if (k > 0) {
            exe[k] = '\0';
            const char *b = strrchr(exe, '/');
            b = b ? b + 1 : exe;
            static const char *names[] = {
                "vmware-vmx", "vmware-vmx-debug", "vmware-vmx-stats",
                "appLoader", "vmrun", "vmcli", "vmrest", "emmett",
                "mksSandbox", "vmware-remotemks", NULL
            };
            if (strstr(exe, "/vmware/") != NULL)
                g_enabled = 1;
            for (int i = 0; !g_enabled && names[i] != NULL; i++) {
                if (strcmp(b, names[i]) == 0)
                    g_enabled = 1;
            }
        }
    }
}

/*
 * 现场生成隐藏描述块（自愈）：用户把整个目录搬走、或描述文件被删，都能自动重建。
 * 全程使用裸系统调用，避免再次进入本库自己的 open/rename 钩子。
 */
static void ensure_desc(const char *dir, const char *imgpath)
{
    char desc[PATH_MAX], data[PATH_MAX], tmp[PATH_MAX];

    if (snprintf(desc, sizeof(desc), "%s%s", dir, DESC_NAME) >= (int)sizeof(desc))
        return;

    struct stat st;
    if (stat(desc, &st) == 0)          /* 已有描述块，不重写（保留 VMware 的 CID 等） */
        return;
    if (stat(imgpath, &st) != 0 || !S_ISREG(st.st_mode))
        return;

    if (snprintf(data, sizeof(data), "%s%s", dir, DATA_NAME) >= (int)sizeof(data))
        return;
    struct stat lst;
    if (lstat(data, &lst) != 0) {       /* 数据入口：同目录相对符号链接 -> img */
        const char *base = strrchr(imgpath, '/');
        base = base ? base + 1 : imgpath;
        if (syscall(SYS_symlink, base, data) != 0) {
            /* 已存在则忽略；其它错误也继续尝试写描述块 */
        }
    }

    if (snprintf(tmp, sizeof(tmp), "%s.tmp%d", desc, (int)getpid()) >= (int)sizeof(tmp))
        return;
    int fd = (int)syscall(SYS_openat, AT_FDCWD, tmp,
                          O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0)
        return;

    char buf[512];
    int n = snprintf(buf, sizeof(buf),
        "# Disk DescriptorFile\n"
        "version=1\n"
        "encoding=\"UTF-8\"\n"
        "CID=%08x\n"
        "parentCID=ffffffff\n"
        "createType=\"monolithicFlat\"\n"
        "\n"
        "# Extent description\n"
        "RW %llu FLAT \"" DATA_NAME "\" 0\n"
        "\n"
        "# The Disk Data Base\n"
        "#DDB\n"
        "ddb.adapterType = \"lsilogic\"\n",
        (unsigned)(getpid() * 2654435761u),
        (unsigned long long)(st.st_size / 512));
    syscall(SYS_write, fd, buf, (size_t)n);
    syscall(SYS_close, fd);
    syscall(SYS_rename, tmp, desc);

    /* 归属真实用户，别在用户目录里留下 root 文件 */
    if (geteuid() == 0 && getuid() != 0) {
        if (syscall(SYS_chown, desc, getuid(), getgid()) != 0) { /* 忽略 */ }
    }
    shim_debug("GENERATE %s %s", desc, imgpath);
}

/*
 * 若 path 是需要接管的裸镜像，返回应改开的描述文件路径（malloc 出来的，调用者 free），
 * 否则返回 NULL。
 */
static char *map_path(const char *path)
{
    if (!path)
        return NULL;

    /* 只在明确开启（VMware 进程）时接管；其它程序完全透明 */
    if (!g_enabled)
        return NULL;

    size_t n = strlen(path);
    if (n < 5 || strcmp(path + n - 4, ".img") != 0)
        return NULL;

    /* 取目录部分（含结尾 '/'），没有 '/' 则视为当前目录 */
    char dir[PATH_MAX];
    const char *slash = strrchr(path, '/');
    if (slash) {
        size_t dl = (size_t)(slash - path) + 1;
        if (dl >= sizeof(dir))
            return NULL;
        memcpy(dir, path, dl);
        dir[dl] = '\0';
    } else {
        dir[0] = '\0';
    }

    char cand[PATH_MAX];
    if (snprintf(cand, sizeof(cand), "%s%s", dir, DESC_NAME) >= (int)sizeof(cand))
        return NULL;

    struct stat st;
    if (stat(cand, &st) != 0)
        ensure_desc(dir, path);        /* 缺失则现场生成 */
    if (stat(cand, &st) != 0)
        return NULL;                   /* 生成失败：放行，让 VMware 报它自己的错 */

    return strdup(cand);
}

/* 统一的“接管判断”日志：命中接管必记一行（无条件），其余仅在 FBIMG_DEBUG=1 时记录 */
static char *map_path_logged(const char *path)
{
    init_once();
    char *np = map_path(path);
    if (np)
        shim_log_redirect(path, np);
    else if (g_debug)
        shim_debug("passthru %s %s", path, "");
    return np;
}

int open(const char *path, int flags, ...)
{
    mode_t mode = 0;
    if (flags & (O_CREAT | O_TMPFILE)) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd = real_open ? real_open(p, flags, mode) : raw_open(p, flags, mode);
    free(np);
    return fd;
}

int open64(const char *path, int flags, ...)
{
    mode_t mode = 0;
    if (flags & (O_CREAT | O_TMPFILE)) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd = real_open64 ? real_open64(p, flags, mode) : raw_open(p, flags, mode);
    free(np);
    return fd;
}

int openat(int dirfd, const char *path, int flags, ...)
{
    mode_t mode = 0;
    if (flags & (O_CREAT | O_TMPFILE)) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd = real_openat ? real_openat(dirfd, p, flags, mode) : raw_open(p, flags, mode);
    free(np);
    return fd;
}

int openat64(int dirfd, const char *path, int flags, ...)
{
    mode_t mode = 0;
    if (flags & (O_CREAT | O_TMPFILE)) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd = real_openat64 ? real_openat64(dirfd, p, flags, mode) : raw_open(p, flags, mode);
    free(np);
    return fd;
}

/* glibc 的 fortify 包装（_FORTIFY_SOURCE）会走这两个符号 */
int __open_2(const char *path, int flags)
{
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd = real_open_2 ? real_open_2(p, flags) : raw_open(p, flags, 0);
    free(np);
    return fd;
}

int __open64_2(const char *path, int flags)
{
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd = real_open64_2 ? real_open64_2(p, flags)
                           : (real_open64 ? real_open64(p, flags, 0) : raw_open(p, flags, 0));
    free(np);
    return fd;
}

int __openat_2(int dirfd, const char *path, int flags)
{
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd = real_openat_2 ? real_openat_2(dirfd, p, flags)
                           : (real_openat ? real_openat(dirfd, p, flags, 0) : raw_open(p, flags, 0));
    free(np);
    return fd;
}

int __openat64_2(int dirfd, const char *path, int flags)
{
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd = real_openat64_2 ? real_openat64_2(dirfd, p, flags)
                             : (real_openat64 ? real_openat64(dirfd, p, flags, 0) : raw_open(p, flags, 0));
    free(np);
    return fd;
}

int creat(const char *path, mode_t mode)
{
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd = real_creat ? real_creat(p, mode) : raw_open(p, O_CREAT | O_WRONLY | O_TRUNC, mode);
    free(np);
    return fd;
}

int creat64(const char *path, mode_t mode)
{
    char *np = map_path_logged(path);
    const char *p = np ? np : path;
    int fd;
    if (real_creat64)
        fd = real_creat64(p, mode);
    else if (real_creat)
        fd = real_creat(p, mode);
    else
        fd = raw_open(p, O_CREAT | O_WRONLY | O_TRUNC, mode);
    free(np);
    return fd;
}

/*
 * 关键补充
 * --------
 * VMware 更新描述块的方式是：
 *     先写 "<磁盘文件名>~"  ->  再 rename() 原子覆盖 "<磁盘文件名>"
 * 如果只拦 open()，这一步 rename 会把裸 img 本体替换成描述块（实测踩过）。
 * 所以这里把 rename/unlink/truncate 也接管：凡是落在裸 img 上的这些操作，
 * 一律改到隐藏描述文件上 —— VMware 的元数据更新永远碰不到 img 本体。
 */
int rename(const char *oldpath, const char *newpath)
{
    init_once();
    char *no = map_path(oldpath);
    if (no) {                       /* 绝不允许把 img 本体改名/搬走 */
        shim_debug("BLOCK-rename-from %s %s", oldpath, no);
        free(no);
        errno = EPERM;
        return -1;
    }
    char *nn = map_path_logged(newpath);   /* 目的路径重定向到描述文件 */
    const char *d = nn ? nn : newpath;
    int r = real_rename ? real_rename(oldpath, d)
                        : (int)syscall(SYS_rename, oldpath, d);
    free(nn);
    return r;
}

int renameat(int olddirfd, const char *oldpath, int newdirfd, const char *newpath)
{
    init_once();
    char *no = map_path(oldpath);
    if (no) {
        shim_debug("BLOCK-renameat-from %s %s", oldpath, no);
        free(no);
        errno = EPERM;
        return -1;
    }
    char *nn = map_path_logged(newpath);
    const char *d = nn ? nn : newpath;
    int r = real_renameat ? real_renameat(olddirfd, oldpath, newdirfd, d)
                          : (int)syscall(SYS_renameat, olddirfd, oldpath, newdirfd, d);
    free(nn);
    return r;
}

int renameat2(int olddirfd, const char *oldpath, int newdirfd, const char *newpath,
              unsigned int flags)
{
    init_once();
    char *no = map_path(oldpath);
    if (no) {
        shim_debug("BLOCK-renameat2-from %s %s", oldpath, no);
        free(no);
        errno = EPERM;
        return -1;
    }
    char *nn = map_path_logged(newpath);
    const char *d = nn ? nn : newpath;
    int r;
    if (real_renameat2)
        r = real_renameat2(olddirfd, oldpath, newdirfd, d, flags);
    else
        r = (int)syscall(SYS_renameat2, olddirfd, oldpath, newdirfd, d, flags);
    free(nn);
    return r;
}

int unlink(const char *path)
{
    char *np = map_path(path);
    if (np) {                       /* 保护 img 本体：假装删掉了 */
        shim_debug("BLOCK-unlink %s %s", path, np);
        free(np);
        return 0;
    }
    return real_unlink ? real_unlink(path) : (int)syscall(SYS_unlink, path);
}

int unlinkat(int dirfd, const char *path, int flags)
{
    char *np = map_path(path);
    if (np) {
        shim_debug("BLOCK-unlinkat %s %s", path, np);
        free(np);
        return 0;
    }
    return real_unlinkat ? real_unlinkat(dirfd, path, flags)
                         : (int)syscall(SYS_unlinkat, dirfd, path, flags);
}

int truncate(const char *path, off_t length)
{
    char *np = map_path(path);
    if (np) {                       /* 保护 img 本体：拒绝截断 */
        shim_debug("BLOCK-truncate %s %s", path, np);
        free(np);
        return 0;
    }
    return real_truncate ? real_truncate(path, length)
                         : (int)syscall(SYS_truncate, path, length);
}
FBIMG_C_EOF

    cat > "$SRCDIR/patch-vmware-ui-imgfilter.py" <<'FBIMG_PY_EOF'
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
FBIMG_PY_EOF
}

# 界面补丁脚本的位置：优先用同目录的独立文件，否则用内嵌副本
ui_script() {
    local d; d=$(dirname "$0")
    if [ -f "$d/patch-vmware-ui-imgfilter.py" ]; then
        printf '%s\n' "$d/patch-vmware-ui-imgfilter.py"; return 0
    fi
    [ -n "$SRCDIR" ] || extract_sources
    printf '%s\n' "$SRCDIR/patch-vmware-ui-imgfilter.py"
}

# ---------------------------------------------------------------- 动作
build_shim() {
    log "编译磁盘访问层 libfbimgshim.so"
    gcc -shared -fPIC -O2 -Wall -Wl,-soname,libfbimgshim.so \
        -o "$SRCDIR/libfbimgshim.so" "$SRCDIR/fbimg-shim.c" -ldl
    install -m 0755 "$SRCDIR/libfbimgshim.so" /usr/local/lib/libfbimgshim.so
    ldconfig
    ok "已安装 /usr/local/lib/libfbimgshim.so（已 ldconfig）"
}

shim_marked() {   # 该二进制是否已挂上 libfbimgshim.so
    readelf -d "$1" 2>/dev/null | grep -q 'libfbimgshim\.so'
}

inject_one() {
    local t=$1
    [ -x "$t" ] || { warn "跳过（不存在）：$t"; return 0; }
    if shim_marked "$t" && [ ! -f "$t.orig" ]; then
        ok "已注入：${t##*/}（无 .orig 备份，视为已就绪）"; return 0
    fi
    if [ ! -f "$t.orig" ]; then
        cp -a "$t" "$t.orig"
        ok "原始备份 -> $t.orig"
    fi
    if shim_marked "$t.orig"; then
        ok "已注入：${t##*/}"; return 0
    fi
    # 不能就地写正在运行的二进制（ETXTBSY）：生成新文件再 rename 顶掉
    local new="$t.new.$$"
    cp -a "$t.orig" "$new"
    chmod u+w "$new"
    patchelf --add-needed libfbimgshim.so "$new"
    chmod --reference="$t.orig" "$new"      # 含 vmware-vmx 的 setuid 位
    mv -f "$new" "$t"
    ok "注入 -> $t"
}

do_install() {
    need_root install
    ensure_tools
    extract_sources

    local bin root
    bin=$(detect_bin) || die "找不到 VMware 安装目录（用 --root / --bin 指定）"
    root=$(cd "$bin/.." && pwd)
    log "VMware 安装目录：$root"
    vmware_running && warn "VMware 正在运行：图形界面/虚拟机需要重启后才会载入新库"

    build_shim

    log "把依赖注入 VMware 二进制（首次会各留一份 .orig）"
    local n
    for n in $TARGET_NAMES; do inject_one "$bin/$n"; done

    log "界面过滤补丁：让“选择磁盘”对话框列出 .img"
    local ui; ui=$(ui_script)
    if python3 "$ui" --root "$root"; then
        ok "界面过滤补丁已就绪"
    else
        warn "界面过滤补丁失败（不影响 DISKLIB 直启，可稍后单跑该脚本）"
    fi

    log "验证"
    local n_ok=0
    for n in $TARGET_NAMES; do
        if [ -x "$bin/$n" ] && shim_marked "$bin/$n"; then
            printf '    %-18s %s\n' "$n" "NEEDED: $(readelf -d "$bin/$n" | awk '/NEEDED/{printf "%s ", $NF}' | cut -c1-60)"
            n_ok=$((n_ok+1))
        fi
    done
    [ "$n_ok" -gt 0 ] || die "没有任何二进制注入成功，请检查上面的报错"
    if [ -x "$bin/vmware-vmx" ]; then
        printf '    %-18s %s\n' "权限" "$(ls -ld "$bin/vmware-vmx" | awk '{print $1}') （vmware-vmx 应保留 setuid）"
    fi

    [ -n "$OPT_IMG" ] && do_selftest "$OPT_IMG"

    cat <<EOF

下一步
  1) 重启 VMware —— 界面进程 appLoader 只在启动时载入库；老进程还拿着旧代码段。
  2) 让虚拟机用裸镜像，两种方式都行：
       · 图形界面：虚拟机设置 → 硬盘 → 更换磁盘 / 添加硬盘 → 选 .img
       · 改 .vmx ：scsi0:0.fileName = "/绝对路径/你的镜像.img"
                   scsi0:0.deviceType = "disk"
  3) 首次打开时会在镜像同目录自动生成隐藏描述块 .fbimg-desc（及 .fbimg-data 链接）；
     .img 本体逐字节不变，随时可以删掉描述块重新来。
  4) 每次成功接管都会记一行到 /tmp/fbimg-shim.<uid>.log（排查用）。
     FBIMG_DEBUG=1 可记录所有 open 调用。

卸载：sudo $PROG uninstall      状态：$PROG check
EOF
}

do_uninstall() {
    need_root uninstall
    local bin root n
    bin=$(detect_bin) || die "找不到 VMware 安装目录（用 --root / --bin 指定）"
    root=$(cd "$bin/.." && pwd)

    log "还原 VMware 二进制（用 .orig 顶回）"
    for n in $TARGET_NAMES; do
        local t="$bin/$n"
        if [ -f "$t.orig" ]; then
            local new="$t.new.$$"
            cp -a "$t.orig" "$new"
            chmod --reference="$t.orig" "$new"
            mv -f "$new" "$t"
            ok "已还原 $t"
        else
            warn "没有 $t.orig，跳过"
        fi
    done

    log "移除磁盘访问层"
    rm -f /usr/local/lib/libfbimgshim.so && ldconfig
    ok "已删除 /usr/local/lib/libfbimgshim.so"

    if have python3; then
        log "还原界面过滤补丁"
        local ui; ui=$(ui_script)
        python3 "$ui" --revert --root "$root" \
            || warn "还原失败，可手动用 *.bak-imgfilter 覆盖回去"
    else
        warn "缺 python3，界面过滤补丁没还原（可手动用 *.bak-imgfilter 覆盖）"
    fi

    cat <<EOF

注意：.img 本体不受任何影响，仍是可直接 dd / qemu-img 使用的普通磁盘镜像。
镜像目录里可能残留隐藏描述块，可一并清掉（不会动 .img）：
  rm -f <镜像目录>/.fbimg-desc
EOF
}

do_check() {
    local bin root n
    bin=$(detect_bin) || { warn "没找到 VMware 安装目录（可用 --root / --bin 指定）"; return 1; }
    root=$(cd "$bin/.." && pwd)
    log "VMware 安装目录：$root"

    echo "  磁盘访问层："
    if [ -f /usr/local/lib/libfbimgshim.so ]; then
        echo "    /usr/local/lib/libfbimgshim.so  $(stat -c '(%s 字节, %y)' /usr/local/lib/libfbimgshim.so)"
    else
        echo "    未安装"
    fi
    echo "  注入状态："
    for n in $TARGET_NAMES; do
        if [ ! -x "$bin/$n" ]; then
            printf '    %-18s %s\n' "$n" "（这台机器没有）"
        elif shim_marked "$bin/$n"; then
            printf '    %-18s %s\n' "$n" "已注入"
        else
            printf '    %-18s %s\n' "$n" "未注入"
        fi
    done
    echo "  界面过滤补丁："
    if have python3; then
        python3 "$(ui_script)" --status --root "$root" | sed 's/^/  /'
    else
        echo "    （缺 python3，无法显示）"
    fi
    vmware_running && echo "  进程：VMware 正在运行（界面相关改动要重启后生效）" || true
}

do_selftest() {
    local img=$1 bin out
    [ -n "$img" ] || die "selftest 需要镜像路径：$PROG selftest disk.img"
    [ -f "$img" ] || die "找不到镜像：$img"
    bin=$(detect_bin) || die "找不到 VMware（用 --bin 指定）"
    [ -x "$bin/vmware-mount" ] || die "找不到 $bin/vmware-mount"

    log "自检：让 VMware 的磁盘引擎（appLoader 进程）读 $img 的分区表"
    if ! out=$("$bin/vmware-mount" -p "$img" 2>&1); then
        printf '%s\n' "$out"
        die "读盘失败 —— 未注入 / VMware 版本变化 / 路径不可读，都可能"
    fi
    printf '%s\n' "$out"
    if printf '%s\n' "$out" | grep -qE '^[[:space:]]*[0-9]+[[:space:]]+[0-9]+'; then
        ok "成功：VMware 已能直接把这张 .img 当磁盘读"
    else
        warn "没看到分区行（空盘？或 VMware 需要重启）"
    fi
    ls -la "$(dirname "$img")/.fbimg-desc" 2>/dev/null || true
}

case $MODE in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    check)     do_check ;;
    selftest)  do_selftest "${OPT_IMG:-}" ;;
esac
