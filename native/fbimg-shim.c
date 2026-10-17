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
