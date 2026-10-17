# native —— 让 VMware 原生直接启动裸 `.img`

> [English](README.md) ｜ 仓库总览：[../README.zh-CN.md](../README.zh-CN.md)

**一句话**：给 VMware 补上「认识 `.img`」的能力 —— `xxx.img` 直接当虚拟磁盘开机，
**零转换、零拷贝**，镜像本体逐字节不变，仍可直接 `dd` / `qemu-img` / 烧 U 盘 / 给 QEMU 用。

```bash
git clone https://github.com/ltbkq/img-vmware-patch && cd img-vmware-patch/native
sudo ./install-vmware-img.sh --img /path/to/disk.img     # 安装 + 就地自检
# 重启 VMware → 虚拟机设置 → 硬盘 → 更换磁盘 / 添加硬盘 → 选 .img
```

| 本目录的内容 | 作用 |
| --- | --- |
| `install-vmware-img.sh` | **一键脚本**：自带全部源码，单文件即可拷到别的机器运行 |
| `fbimg-shim.c` | 磁盘访问层 `libfbimgshim.so` 的源码（约 530 行 C） |
| `patch-vmware-ui-imgfilter.py` | 图形界面「选择磁盘」对话框的过滤补丁 |
| `install-native.sh` / `uninstall-native.sh` | 拆分版安装 / 卸载（逻辑与一键脚本同一套） |
| `check-sync.sh` | 校验一键脚本里内嵌的源码副本与上面两个源码文件一致 |

---

## 一、为什么需要它：VMware 的两道坎

### 坎 1：磁盘引擎只认 vmdk 容器

VMware 的磁盘引擎（`vmware-vmx` 进程里的 DISKLIB）打开一块虚拟磁盘时，**先读文件开头当
「descriptor（描述块）」解析**，描述块里用 `RW <扇区数> FLAT "<数据文件>" 0` 这样的行
指出真正的数据落在哪个文件、哪个偏移：

```
# Disk DescriptorFile
version=1
encoding="UTF-8"
CID=aedf133b
parentCID=ffffffff
createType="monolithicFlat"

# Extent description
RW 67108864 FLAT ".fbimg-data" 0     ← 数据本体（64×512 = 32 GiB）

# The Disk Data Base
#DDB
ddb.adapterType = "lsilogic"
```

裸 `.img` 里开头是分区表（GPT/MBR），解析器当然读不懂，于是原话是：

```
Failed to open disk: The specified file is not a virtual disk (16063)
Failed to get the list of partitions: The virtual disk specified is invalid.
```

> 注意这不是「格式不支持」，而是「**容器**不支持」：vmdk 只是个 350 字节的文本壳，
> 数据本来就可以放在任意文件里、任意偏移上 —— 这正是本方案要利用的一点。

### 坎 2：图形界面只列出 `*.vmdk`

「添加硬盘 / 更换磁盘 / 检索磁盘信息」对话框里那个「虚拟磁盘」过滤档，只有两个 glob：

| 档位 | 原版 glob |
| --- | --- |
| Virtual Disks | `*.vmdk`　`*.VMDK` |
| ISO Images | `*.iso`　`*.ISO`　**`*.img`** ← 本来就有 |
| Floppy Images | `*.flp`　**`*.img`** ← 本来就有 |

所以 `.img` 在列表里**根本不出现**，连误选的机会都没有。

而且 —— 「无法检索磁盘信息」这句报错，来自**图形界面进程**，不是 `vmware-vmx`。
Workstation 的 GUI（`/usr/bin/vmware` → `appLoader`）自己也会调 DISKLIB 去校验磁盘。
**只给 `vmware-vmx` 打补丁的话，界面照样报错**，这是本方案第一个大坑（见第七节）。

---

## 二、原理

### 2.1 把 `.img` 伪装成 vmdk：现场生成描述块

在 `open()` 上拦一刀：

```
open("<目录>/xxx.img")
   └─ 同目录没有 .fbimg-desc 时，按 .img 的真实大小现场生成一个（合法 vmdk 描述块）
   └─ 改为打开 "<目录>/.fbimg-desc"
其余路径原样放行
```

生成的 `.fbimg-desc` 用一条 extent 指向同目录的隐藏符号链接 `.fbimg-data`，
而 `.fbimg-data` 指向真正的 `xxx.img`：

```
   VMware  ──open──▶  .fbimg-desc (277 B，文本)
                          │  RW 67108864 FLAT ".fbimg-data" 0
                          ▼
                       .fbimg-data ──symlink──▶ xxx.img  ← 数据本体，offset 0
```

于是：

* VMware 解析到的是一个**它自己写的、完全合法的描述块**，以为在开 vmdk；
* VMware 之后所有的磁盘读写都落在 `xxx.img` 的 offset 0 上，**逐字节就是原镜像**；
* 描述块只有 277 字节，随便它重写、加 `CID`、改 `ddb.longContentID` 都碰不到镜像本体。

### 2.2 拦截的不只是 `open()`

| 函数 | 为什么要管 |
| --- | --- |
| `open` / `open64` / `openat` / `openat64`（含 `__open_2` 等 fortify 变体） | 把 `.img` 换成描述块 |
| `creat` / `creat64` | 少数路径用它建盘 |
| `rename` / `renameat` / `renameat2` | **保护镜像本体**：VMware 更新描述块时的写法是 `write "xxx~"` → `rename("xxx~", "xxx")`；这里 `xxx` 被 shim 拦过，但 `rename` 的目标是原始路径，如果不管，**它会把裸 `.img` 直接顶掉** |
| `unlink` / `unlinkat` | 删除描述块时不能连带把镜像删了 |
| `truncate` | 别让 VMware 按描述块长度去截断镜像 |

`rename`/`unlink`/`truncate` 这几条是**血泪教训**（见第七节坑 2）：早期版本没管，
一次「修改虚拟机设置」就把一个 20 GiB 的镜像 inode 顶掉、数据全没了。

### 2.3 生效条件（三条同时满足，其余情况是完全透明的空操作）

1. **进程属于 VMware**：可执行文件路径里含 `/vmware/`
   （覆盖 `/usr/lib/vmware`、`/usr/local/vmware`、`/opt/vmware` 等任意前缀），
   或进程名命中已知列表（`vmware-vmx`、`vmware-vmx-debug`、`vmware-vmx-stats`、
   `appLoader`、`vmrun`、`vmcli`、`vmrest`、`emmett`、`mksSandbox`…），
   或显式设置 `FBIMG_ENABLE=1`；
2. **被打开的路径以 `.img` 结尾**；
3. **同目录能生成 / 找到 `.fbimg-desc`**（镜像大小要是 512 的整数倍）。

除此之外，对系统里**任何其它程序、任何其它文件**都是空操作 —— 不碰 `.raw`、`.bin`、
`.qcow2`，也不碰不带后缀的磁盘。

### 2.4 为什么必须注入 `appLoader`

`/usr/bin/vmware`、`vmrun`、`vmware-mount` 都是 **`/usr/lib/vmware/bin/appLoader`
的软链**，图形界面代码 `libvmwareui.so` 是它 `dlopen` 进来的。所以：

| 你想干的事 | 实际跑在哪个进程里 | 需要注入 |
| --- | --- | --- |
| 开机、读写虚拟磁盘 | `vmware-vmx` | `vmware-vmx*` |
| 添加硬盘 / 更换磁盘 / 检索磁盘信息 | `appLoader` | **`appLoader`** |
| `vmware-mount -p`、`vmware-vdiskmanager` | `appLoader` | **`appLoader`** |

漏掉 `appLoader` 的典型症状：命令行把 `.img` 写进 `.vmx` 能开机，但**图形界面里选盘就报
「无法检索磁盘信息」**。

### 2.5 自愈：把整个目录搬走也没事

描述块是「用的时候没有就现生成」，所以把 `.img` 拷到别处、或手工删掉
`.fbimg-desc`/`.fbimg-data`，下次打开会自动重建。数据本体永远不受影响。

---

## 三、为什么用 `DT_NEEDED` 而不是 `LD_PRELOAD`

`vmware-vmx` 带 **setuid root** 位，glibc 在安全执行（secure-execution）模式下会
**丢弃 `LD_PRELOAD` / `LD_LIBRARY_PATH`**，用环境变量注入根本不会生效。
`patchelf --add-needed` 把 `libfbimgshim.so` 写成这些二进制**自身的依赖**，才是干净的走法：

| 方案 | setuid 生效 | 影响范围 | 需要环境变量 | 适配多个进程 |
| --- | --- | --- | --- | --- |
| `LD_PRELOAD` | ❌ 被 secure-execution 丢掉 | 全局（launch 出去的每个进程都中） | ✅ 需要 | 得给每个进程单独设 |
| `/etc/ld.so.preload` | ✅ | **全局**（系统里所有程序都被 hook，风险大） | ❌ | ✅ |
| **`DT_NEEDED`（本方案）** | ✅ | **只随这几个 VMware 二进制加载** | ❌ | ✅ 逐个精确注入 |

配套细节：

* 库必须带 **`SONAME`**（`-Wl,-soname,libfbimgshim.so`）并 `ldconfig`，
  setuid 程序只在**受信缓存**里解析依赖；
* 注入时用 `patchelf --add-needed`（新依赖会被**插到 NEEDED 表最前面**），
  这样在动态符号解析顺序上**早于 libc**，才能抢到 `open`/`rename` 这些符号；
* 不能就地改写正在运行的二进制（`ETXTBSY`）：**复制到新文件 + `rename` 顶替**，
  权限（含 setuid 位）用 `chmod --reference` 照抄。

---

## 四、图形界面那道坎：为什么改的是 `lea` 而不是字符串

过滤档是界面在 `.rodata` 里拼出来的一张表，磁盘那一档用的 glob 串是：

```
1501fcb  *.vmdk        ← 第 1 个 glob
1501fd2  *.VMDK        ← 第 2 个 glob
1501ffd  *.img         ← 别处（ISO/软盘档）已经在用
```

**没有办法把 `*.vmdk` 那个字符串改成 `*.img`**：`*.vmdk` 占了 6 字节（含 `\0`），
`*.img` 只要 6 字节正好，但**过滤表只挂了两个 glob 槽位**，而 `*.vmdk` 和 `*.VMDK`
两个槽位都指向各自独立的字符串、且长度都容不下别的写法（`g_pattern_match_simple`
不支持字符类，写不出 `*.[vV][mM][dD][kK]`）。

所以做法是：**只改「第 2 个槽位」那条 `lea` 指令的 4 字节 RIP 相对位移**，
让它指向 `.rodata` 里**已经存在的** `*.img` 字符串：

```asm
; 改动前（指向 0x1501fd2 "*.VMDK"）
c4cd03: 48 8d 35 ?? ?? ?? ??   lea    <...>(%rip),%rsi

; 改动后（指向 0x1501ffd "*.img"）
c4cd03: 48 8d 35 f3 52 8b 00   lea    0x8b52f3(%rip),%rsi   # 1501ffd
```

代价只有一个：大写的 `*.VMDK` 不再出现在列表里（实际没人这么命名，且第 1 个槽位仍保留 `*.vmdk`）。

顺手把两档的**显示文案**也改准确（原地覆盖，新文案更短或等长，绝对不动相邻数据）：

| 位置 | 改动前 | 改动后 |
| --- | --- | --- |
| `libvmwareui.so` | `Virtual Disks (*.vmdk)` | `Disks (*.vmdk, *.img)` |
| `libvmwareui.so` | `ISO Images (*.iso)` | `ISO (*.iso, *.img)` |
| `messages/zh_CN/vmware.vmsg` | `虚拟磁盘 (*.vmdk)` | `虚拟磁盘 (*.vmdk, *.img)` |
| `messages/zh_CN/vmware.vmsg` | `ISO 映像 (*.iso)` | `ISO 映像 (*.iso, *.img)` |

补丁脚本**先字符串搜索、再校验命中次数**，任何一处对不上（比如换了 VMware 版本）
就**放弃并报错，绝不硬改**，原文件另存 `*.bak-imgfilter`。

---

## 五、用法

### 5.1 一键脚本（推荐）

```bash
sudo ./install-vmware-img.sh                     # 安装（自动探测 VMware 目录）
sudo ./install-vmware-img.sh --img disk.img      # 安装 + 就地自检这张镜像
sudo ./install-vmware-img.sh uninstall           # 完全还原（含界面补丁）
./install-vmware-img.sh check                    # 只看状态，不需要 root
./install-vmware-img.sh selftest disk.img        # 用 VMware 的磁盘引擎读分区表
```

| 选项 | 说明 |
| --- | --- |
| `--root DIR` | VMware 安装目录（默认自动探测 `/usr/lib/vmware` → `/usr/local/vmware` → `/opt/vmware`，再顺着 `vmware` 命令反查） |
| `--bin DIR` | 直接指定 `bin` 目录（优先于 `--root`） |
| `--img FILE` | 安装后顺手对这张镜像做一次 `vmware-mount -p` 自检 |
| `--no-apt` | 缺 `gcc`/`patchelf`/`python3` 时不尝试自动安装 |
| `-h` / `-V` | 帮助 / 版本 |

特点：

* **幂等**：重复执行不会叠加多条 `NEEDED`，也不会重复打补丁；
* **自包含**：C 源码与 Python 补丁都内嵌在脚本里，拷一个文件到别的机器就能跑；
* **可完全还原**：每个被改的二进制都留 `.orig` 备份，卸载就是把它顶回去；
* **安全操作正在运行的 VMware**：写新文件再 `rename`，不碰正在执行的文件。

### 5.2 拆分版（仓库开发用）

```bash
sudo sh install-native.sh      # 编译 + 注入 + 界面补丁（4 步）
sudo sh uninstall-native.sh    # 全部还原
```

### 5.3 手工把虚拟机指向裸镜像

图形界面：**虚拟机设置 → 硬盘 → 更换磁盘 / 添加硬盘 → 选 `.img`**。

或者直接改 `.vmx`：

```
scsi0:0.present    = "TRUE"
scsi0:0.fileName   = "/绝对路径/disk.img"
scsi0:0.deviceType = "disk"
```

首次打开时会在镜像同目录自动出现：

```
.fbimg-desc    277 B   文本描述块（VMware 眼里的"vmdk"）
.fbimg-data    symlink → disk.img
```

> 删掉这两个隐藏文件完全没问题（会重建），**但别把它们当成镜像去动**。
> `disk.img` 本体不受影响。

### 5.4 排查

| 手段 | 看什么 |
| --- | --- |
| `./install-vmware-img.sh check` | 库是否装上、4 个二进制是否注入、补丁是否生效 |
| `readelf -d /usr/lib/vmware/bin/appLoader \| grep fbimg` | 依赖是否真的挂上了 |
| `cat /tmp/fbimg-shim.<uid>.log` | 每次成功接管记一行 `REDIRECT xxx.img -> .fbimg-desc` |
| `FBIMG_DEBUG=1 vmware-mount -p disk.img` | 记录**所有** `open` 调用（看看到底打开了什么） |
| `python3 patch-vmware-ui-imgfilter.py --status` | 界面补丁状态（不需要 root） |

---

## 六、实测验证

验证环境：**VMware Workstation 26.0.1 build 25688693**，宿主 Linux Mint 22.x（ext4）。
测试对象是一张 32 GiB 的 **Linux Mint 22.3 Cinnamon 裸 `.img`**（GPT：BIOS boot + ESP + ext4）。

### 6.1 磁盘引擎 A/B（关键证据：用的是**界面同一个二进制**）

`vmware-mount` 就是 `appLoader`，它做的正是界面「检索磁盘信息」那件事：

```
A) 未注入（卸载后）：
   Failed to open disk: The specified file is not a virtual disk (16063)
   Failed to get the list of partitions: The virtual disk specified is invalid.

B) 注入后：
   [AppLoader] Use shipped Linux kernel AIO access library.      ← 确实是 appLoader 进程
   Nr      Start       Size Type Id Sytem
   -- ---------- ---------- ---- -- ------------------------
    1       2048       4096  GPT EE Unknown
    2       6144    1048576  GPT EE Efi System
    3    1054720   66054111  GPT EE Linux FS              ← 镜像的真实分区表
```

`vmware-vdiskmanager -r disk.img -t 0 out.vmdk`（同样走 DISKLIB）也从
「not a virtual disk」变成 **100% 转换成功**。

### 6.2 注入与权限

```
$ readelf -d /usr/lib/vmware/bin/vmware-vmx | awk '/NEEDED/{print $NF}'
[libfbimgshim.so]      ← 排在第一位，符号解析早于 libc
[librt.so.1]
...

$ ls -l /usr/lib/vmware/bin/vmware-vmx /usr/lib/vmware/bin/appLoader
-rwsr-xr-x  vmware-vmx     ← setuid 位保留
-rwxr-xr-x  appLoader

$ ls -l /usr/local/lib/libfbimgshim.so
-rwxr-xr-x  21712 bytes    ← 编译产物，很小
```

接管日志（`/tmp/fbimg-shim.0.log`）：

```
[shim pid=256163] REDIRECT /media/.../linuxmint-22.3-cinnamon-ltbkq.img -> /media/.../.fbimg-desc
```

### 6.3 界面过滤补丁前后

```
原版：@&!*@*@(msg.cui.filter.Disks)Virtual Disks (*.vmdk)
现版：@&!*@*@(msg.cui.filter.Disks)Disks (*.vmdk, *.img)
原版：@&!*@*@(msg.cui.filter.ISOImages)ISO Images (*.iso)
现版：@&!*@*@(msg.cui.filter.ISOImages)ISO (*.iso, *.img)

$ objdump -d libvmwareui.so | grep -A1 c4cd03
c4cd03: 48 8d 35 f3 52 8b 00   lea 0x8b52f3(%rip),%rsi  # 1501ffd    ← 已指向 "*.img"
```

### 6.4 往返测试（装 → 卸 → 再装）

| 步骤 | `vmware-mount -p disk.img` | 界面补丁 |
| --- | --- | --- |
| 安装后 | 打印出真实分区表 ✅ | 已打补丁（含 `*.img`） |
| `uninstall` 后 | `not a virtual disk (16063)` ✅ | 已还原成原版 |
| 再安装 | 打印出真实分区表 ✅ | 已打补丁 |

> 这条往返是**卸载脚本自身的一个 bug 暴露出来的**：早期版本 `uninstall` 只还原二进制、
> 忘了还原界面补丁（脚本里引用了一个只在安装路径下才赋值的变量），
> 于是「卸载完界面还列 `.img`、但一选就报 not a virtual disk」。
> 现在两个方向都纳入回归测试。

### 6.5 镜像本体无损

* VMware 只会写 `.fbimg-desc`（277 B 文本）与 `.fbimg-data` 链接；
* 镜像本体大小、分区表、inode 不变，`sha256` 仍是构建时那个值。

---

## 七、踩过的坑（都是真踩过的）

| # | 现象 | 原因 | 解法 |
| --- | --- | --- | --- |
| 1 | 命令行把 `.img` 写进 `.vmx` 能开机，**界面里选盘仍报「无法检索磁盘信息」** | 界面是 `appLoader` 进程，只注入了 `vmware-vmx` | 把 `appLoader` 也加进注入列表 |
| 2 | 改一次虚拟机设置，**20 GiB 镜像的 inode 被顶掉、数据全没了** | VMware 更新描述块的写法是 `write xxx~` + `rename(xxx~, xxx)`，`rename` 没被拦 | 一并 hook `rename`/`renameat`/`renameat2`/`unlink`/`truncate` |
| 3 | `LD_PRELOAD` 对 `vmware-vmx` 无效 | 带 setuid，secure-execution 丢掉 `LD_PRELOAD` | 改用 `DT_NEEDED` 注入 + `SONAME` + `ldconfig` |
| 4 | 注入后仍然不生效，或被别的库抢先 | `patchelf --add-needed` 之外的顺序问题 | 用 `--add-needed`（插到 NEEDED 表最前），并验证 `readelf` 里排第一 |
| 5 | 想把「虚拟磁盘」档的 `*.vmdk` 改成 `*.img`，改不动 | 过滤表只有两个 glob 槽位、`*.VMDK` 独立成串、`g_pattern_match_simple` 没有字符类 | 只改第 2 个槽位那条 `lea` 的 4 字节位移，指向已有的 `*.img` 字符串 |
| 6 | 正在跑 VMware 时安装报 `ETXTBSY` | 试图就地改写正在执行的二进制 | 写新文件 + `rename` 顶替，权限用 `chmod --reference` 照抄 |
| 7 | 卸载后界面里还能选 `.img`，选了就报 `not a virtual disk` | 卸载漏还原界面补丁（变量只在安装路径赋值） | 卸载路径也要能取到补丁脚本（同目录或内嵌副本） |
| 8 | 用 `cmp -l` 看二进制差异一片乱码 | GNU `cmp` 输出的是**八进制** | 改用 `objdump -d` + `xxd` 对照着看 |

---

## 八、移植到别的机器 / 别的 VMware 版本

* **安装前缀**：脚本按 `/usr/lib/vmware` → `/usr/local/vmware` → `/opt/vmware`
  依次探测，再顺着 `vmware` 命令反查（`/usr/bin/vmware` 通常是 `appLoader` 的软链），
  也可以 `--root` / `--bin` 或 `FBIMG_VMWARE_BIN=` 指定。
  磁盘访问层的**进程判定**用的是「路径里含 `/vmware/`」+ 已知进程名，同样不写死前缀。
* **免运行环境**：只依赖内核原语（`openat`/`rename`/`unlink`/`stat`），
  不依赖 glibc 内部结构，x86-64 与其它 64 位架构都能编。
* **换 VMware 版本**：
  * 磁盘访问层（`libfbimgshim.so`）**通常不受影响** —— 它只认系统调用，不认 VMware 内部；
    真正要确认的是「VMware 是否还从这几个二进制加载」：装完 `./install-vmware-img.sh check`
    就知道；找不到目标进程名时，加 `FBIMG_ENABLE=1` 也能临时验证。
  * 界面过滤补丁**与版本强相关**（要改 `libvmwareui.so` 的机器码）。
    它自己做**字符串搜索 + 命中次数校验**，对不上就报错放弃，**不会把库改坏**；
    此时只有「界面里列不出 `.img`」这一个功能缺失，命令行/DISKLIB 直启仍然可用。
* **Windows / macOS 宿主**：本目录的脚本只针对 **Linux 宿主**。
  Windows 上用仓库的 `link`（生成描述块）方案更省事。

---

## 九、与 `link` 方案（生成 vmdk 描述块）的取舍

同一个仓库里两条路线是**互补**的：

| | `link` / `create`（Python 工具） | **`native`（本目录）** |
| --- | --- | --- |
| 做法 | 为 `.img` **生成一个 `.vmdk` 描述块文件** | 拦截 VMware 的 `open()`，**打开时现生成隐藏描述块** |
| 目录里留什么 | 多一个（可命名的）`.vmdk` | 多两个隐藏文件 `.fbimg-desc` / `.fbimg-data` |
| 图形界面选盘 | ✅ 选 `.vmdk`（VMware 本来就认） | ✅ 选 `.img`（靠界面补丁） |
| 需要改 VMware | ❌ 不需要 | ✅ 需要（一次性注入 + 补丁） |
| 换 VMware 版本 | 基本不受影响 | 界面补丁可能要跟进 |
| 适合 | 不想碰 VMware、要给别人分发 `.vmdk`、Windows/macOS 宿主 | 想**直接开机裸 `.img`**（`.vmx` 里就写 `.img`）、同一台机器反复用不同镜像 |

两者可以同时存在，互不冲突。

---

## 十、安全边界

* **只对 VMware 进程生效**：其它程序哪怕打开 `foo.img` 也是原样打开，
  库在有别的进程里根本不会被加载（它是那几个二进制的私有依赖）。
* **只对 `.img` 后缀生效**：不碰 `.raw` / `.bin` / `.qcow2`。
* **只影响同目录的描述块**：接管时最多在镜像目录里创建/读取
  `.fbimg-desc`、`.fbimg-data` 两个隐藏文件，**镜像本体只被读写数据区**。
* **完全可逆**：`uninstall` 用 `.orig` 备份原样顶回二进制，删掉库并 `ldconfig`，
  界面补丁用 `.bak-imgfilter` 还原。
* **唯一需要 root 的地方**：写 `/usr/local/lib` 和 VMware 安装目录。

---

## 十一、FAQ

**Q：会不会把我的镜像弄坏？**
正常使用不会：VMware 只往描述块（277 B）和数据区写。唯一的真实风险来自**没装本补丁时**
VMware 的「更新描述块」流程会把 `.img` 顶掉（第七节坑 2）——装了补丁才有保护。

**Q：目录里多出来的 `.fbimg-desc` / `.fbimg-data` 是什么？**
VMware 眼里的「vmdk 描述块」和「数据文件」入口，自动生成、可随时删。

**Q：为什么 `ls` 看不到 `.fbimg-*`？**
点开头的是隐藏文件，`ls -a` 或 `ls -la`。

**Q：`.img` 移走了，虚拟机报错怎么办？**
描述块按同目录生成，跟着 `.img` 一起搬就行；`.fbimg-desc` 里用的是相对路径
（`.fbimg-data`），所以整个目录可以随意移动。

**Q：能同时开多个虚拟机吗？**
能，每个镜像目录各有自己的描述块。

**Q：VMware 升级之后要重做吗？**
升级会覆盖 VMware 二进制，需要重跑一次 `install-vmware-img.sh`（幂等）。
先跑 `check` 看状态。

**Q：为什么不干脆每次都生成一个 `.vmdk`？**
那就退化成 `link` 方案了：多一个需要维护的中间文件，且图形界面里选的是 `.vmdk`
而不是原始 `.img`。本方案的目标是「`.vmx` 里就写裸 `.img`，所见即所得」。

**Q：支持 `.qcow2` / `.raw` / `.bin` 吗？**
目前只认 `.img` 后缀。想扩到别的后缀，改 `fbimg-shim.c` 里的后缀判断即可
（`.raw`、`.bin` 这类裸镜像可以直接加；`.qcow2` **不行**，它有自己的容器格式，
VMware 读不了）。

---

## 十二、文件清单

```
native/
├── install-vmware-img.sh          # 一键脚本（自带内嵌源码，单文件可分发）
├── fbimg-shim.c                   # 磁盘访问层源码 → libfbimgshim.so
├── patch-vmware-ui-imgfilter.py   # 界面过滤补丁（幂等 / --status / --revert / --root）
├── install-native.sh              # 拆分版安装（4 步）
├── uninstall-native.sh            # 拆分版卸载（全部还原）
├── check-sync.sh                  # 校验一键脚本内嵌源码与独立文件一致
├── README.md                      # 英文版
└── README.zh-CN.md                # 本文件
```

运行期落在系统里的东西（**都可还原**）：

```
/usr/local/lib/libfbimgshim.so                     ← 新建
/usr/lib/vmware/bin/{vmware-vmx,vmware-vmx-debug,
                    vmware-vmx-stats,appLoader}    ← 加一条 NEEDED，各留一份 .orig
/usr/lib/vmware/lib/libvmwareui.so/libvmwareui.so  ← 改 1 条 lea + 2 处文案，留 .bak-imgfilter
/usr/lib/vmware/messages/*/vmware.vmsg             ← 文案补 *.img，留 .bak-imgfilter
<镜像目录>/.fbimg-desc / .fbimg-data                ← 运行期自动生成，可删
/tmp/fbimg-shim.<uid>.log                          ← 接管日志
```

## 许可

MIT —— 见 [../LICENSE](../LICENSE)。
