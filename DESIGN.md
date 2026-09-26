# DESIGN.md — img-vmware-patch 设计文档

- **文档版本**：1.0（2026-09-26）
- **状态**：与 `src/img_vmware_patch/` 当前实现同步
- **事实来源与优先级**（冲突时以序号小者为准）：
  1. `src/img_vmware_patch/` 实现代码（**最终事实**）
  2. `CONTRACT.md`（CLI 语义、几何规则的实现契约）
  3. `VERIFICATION.md`（VMware Workstation 17.6.3 真机实验，30+ 台实验机的实测结论）
  4. `README.md` / `CHANGELOG.md`（面向用户的说明）
  5. 早期设计稿（**已被本文档取代**；其中被证伪的论断见[附录 A 勘误表](#附录-a原设计稿勘误表)）

> 本文档按「必须以真机验证结论修正早期设计稿」的要求重写：凡涉及 VMware 实际行为的论断，
> 一律以 `VERIFICATION.md` 的实验编号（EXP*n*）为证据，不再沿用未经验证的推断。

---

## 1. 文档范围与目标读者

### 1.1 范围

**在范围内**：

- 问题定义与现有方案对比；
- 核心思路：为什么一个几十行的文本 descriptor 就能把 raw 镜像直接挂进 VMware，
  以及正确的 descriptor / `.vmx` 结构；
- 模块级详细设计（几何计算、路径规范化、适配器映射、vmx 集成、错误处理），**以实现代码为准**；
- 对外接口（CLI 全文以实际 `--help` 输出为准，另附可编程 API）；
- 真机验证结论汇总与证据索引；
- 替代方案、风险与限制、未来工作。

**不在范围内**：

- 逐行讲解实现代码（直接读 `src/img_vmware_patch/*.py`，每个模块都有模块级 docstring）；
- 逐条罗列 120 个单元测试用例（见 `tests/`）；
- 安装、日常使用教程（见 `README.md` / `README.zh-CN.md`）；
- 项目管理类内容（许可、发布流程、CHANGELOG 维护）。

### 1.2 目标读者

| 读者 | 建议阅读 |
| --- | --- |
| 维护/评审本项目的开发者 | 全文 |
| 验收设计的技术负责人 | 第 4、5、6、8、10 节 + 附录 A |
| 想把 raw 镜像挂进 VMware 的运维/取证/逆向工程师 | 第 2、5、7、8 节即可 |
| 后续流水线/集成方（要把本工具嵌进脚本） | 第 7 节 |

### 1.3 与其他文档的分工

| 文档 | 回答的问题 |
| --- | --- |
| `README.md` | 怎么装、怎么用、退出码含义 |
| `CONTRACT.md` | CLI 与算法**必须**长什么样（实现契约） |
| `VERIFICATION.md` | VMware **实际上**怎么表现（实验记录与报错原文） |
| `DESIGN.md`（本文） | 为什么这样设计、怎么实现的、验证结论如何约束设计 |

---

## 2. 项目背景

### 2.1 问题

用户手里有 raw 磁盘镜像（`.img` / `.bin` / `.raw`，来自 `dd`、`qemu-img`、
整盘备份、取证镜像、嵌入式固件导出），希望在 VMware Workstation / Player / Fusion
里**直接启动或挂载**它。

### 2.2 现实矛盾

1. **VMware 不接受 raw 文件**。它的磁盘后端只认 `.vmdk`：
   一个文本 descriptor（声明 extent）+ 后端数据文件。
2. **常规"转换"路线代价高**。`qemu-img convert -O vmdk`、
   `vmware-vdiskmanager -r` 都要完整复制数据：TB 级镜像意味着数十分钟到数小时的
   拷贝与一倍的磁盘占用，而用户只是"想挂上去看一眼"。
3. **手工写 descriptor 极易出错**。descriptor 语法没有任何官方规格书公开，
   常见错误（extent 行数量与 offset、扇区取整方式、extent 访问位写 `RO`、
   路径形态、省略 `version=1`）都会让 VMware 报出 `INVALIDDISK` /
   `FILENOPERMISSION` 这类与真实原因相距甚远的错误。
   **早期设计稿本身就带着这样的错误**（见附录 A）。

### 2.3 关键洞察

`monolithicFlat` descriptor 是纯文本，VMware 通过其中**一条 extent 行**
把虚拟 LBA 线性映射到数据文件的字节偏移。当该行是「单条、offset=0、
扇区数 = 文件扇区数」时，映射是**恒等**的：

```
虚拟盘字节 ≡ raw 镜像字节
```

于是"接入 raw 镜像"退化为一个 **O(1) 的文本生成问题**，而不是数据转换问题。
本项目即把这一事实工程化为一个可脚本化、经过真机验证的小工具。

### 2.4 约束（来自 `CONTRACT.md`）

- Python 3.9+，**仅标准库**（契约举例：`argparse` / `pathlib` / `os` / `platform`；
  实现实际还用到 `re` / `shutil` / `subprocess` / `dataclasses` / `ntpath` / `posixpath`，
  **并未导入契约行文中提到的 `json`**）；
- 三个子命令 `link` / `create` / `info`，CLI 文案与退出码逐字固定；
- `link` / `info` **不得依赖 VMware**，必须能在没装 Workstation 的机器上运行；
- `geometry` / `descriptor` 为纯计算模块（无 I/O、无 stdout/stderr、不 `sys.exit`）；
- 单测不得启动真实 `vmrun`；
- MIT 许可。

### 2.5 术语

| 术语 | 含义 |
| --- | --- |
| raw 镜像 | 内容即磁盘扇区字节序列的普通文件，无任何封装头 |
| descriptor | `.vmdk` 文本文件，声明版本、extent、DDB（磁盘数据库） |
| extent | descriptor 中的一行 `RW/RO <num> FLAT <file> <offset>`，描述一段虚拟盘到文件的映射 |
| `monolithicFlat` | 单文件 flat 盘：descriptor + 一个平铺数据文件（此处即 raw 镜像） |
| 分卷盘 | `image-s001.vmdk`/`image-s002.vmdk`… 多个 extent 指向多个文件 |
| DDB | Disk Data Base，`ddb.*` 元数据段（几何、adapterType 等） |
| CHS | Cylinders/Heads/Sectors，老式 BIOS 寻址用的磁盘几何 |
| LBA | 逻辑块地址，现代寻址方式；extent 行末尾的偏移字段以扇区计 |
| vmx | 虚拟机配置文件，声明硬件与磁盘挂载点 |
| `vmrun` / `vmware-vdiskmanager` | VMware 命令行工具，分别用于虚拟机生命周期与磁盘管理 |

---

## 3. 现有方案对比

在本项目出现之前，把 raw 镜像弄进 VMware 只有下面几条路：

| # | 方案 | 原理 | 时间 / 空间成本 | 依赖 | 主要问题 |
| --- | --- | --- | --- | --- | --- |
| A | 转换式（`qemu-img convert -O vmdk`、StarWind V2V、`vmware-vdiskmanager -r`） | 把 raw 复制成 VMDK | 与镜像体积线性：TB 级 = 小时级 + **100% 额外空间** | qemu-img 或 VMware | 慢、占空间；转换后原 raw 与 vmdk 两份数据需自行同步 |
| B | VMware GUI「新建虚拟机 → 使用现有磁盘」 | 向导里选一个已存在的 `.vmdk` | 取决于 A | 图形界面 | **只能选 `.vmdk`**，raw 必须先走 A；无法脚本化、无法在无头主机用 |
| C | 手写 descriptor + 手写 `.vmx` | 本项目的思路原型 | 秒级、零拷贝 | 无 | 纯手工、易错（早期设计稿即反例：extent 两行、取整规则、路径论断均错） |
| D | 物理磁盘直通（raw disk access / 设备 passthrough） | 虚拟机直接持有整块设备 | 秒级 | 主机有裸块设备 | 只针对**块设备**，不针对镜像文件；权限与破坏性极高 |
| E | 事后转换 + 保留 raw（A + 归档） | 平时用 vmdk，raw 归档 | 两份存储 | A | 不解决"临时挂载"场景 |

**结论**：只有 C 是零拷贝路线，而 C 的全部难点在于"写对 descriptor、写对 vmx、
知道哪些坑会被 VMware 拒绝"。本项目做的事就是把 C 工程化：
**(E′) 脚本化生成 descriptor/vmx + 用真机实验把 C 的隐性知识固化下来**。

---

## 4. 目标 / 非目标

### 4.1 目标

| 编号 | 目标 | 如何度量 |
| --- | --- | --- |
| G1 | **零拷贝**：不复制镜像数据，操作耗时与镜像大小无关 | `link` 仅写一个 <1 KiB 文本文件 |
| G2 | **正确性**：生成物能被 Workstation 真实打开并上电 | `VERIFICATION.md` 30+ 台实验 + 第 8.4 节冒烟验证 |
| G3 | **无 VMware 依赖**：`link` / `info` 在未安装 VMware 的机器可用 | 单测中 `vmrun` 探测全部 mock |
| G4 | **可脚本化**：稳定退出码、stdout/stderr 分离、CI 友好 | 第 7.2 节退出码表；`IMG_VMWARE_PATCH_REGISTER` 开关 |
| G5 | **跨平台路径正确性**：POSIX / Windows（盘符、UNC、反斜杠） | `normalize_path` 双平台语义 + 单测 |
| G6 | **可解释的失败**：每类错误给出原因 + 可执行的 hint | 第 6.7 节错误表；实测消息见第 7 节 |

### 4.2 非目标

| 编号 | 非目标 | 理由 |
| --- | --- | --- |
| N1 | 不做格式转换、压缩、分卷、加密、稀疏化 | 与 G1 冲突；转换交给 `vdiskmanager`/`qemu-img` |
| N2 | 不提供快照 / undo / redo 写保护 | `FLAT` 后端为 persistent，VMware 不对其做写时拷贝（见 R1） |
| N3 | 不保证客体操作系统能引导 | 只保证「磁盘被 VMware 接受并上电」，引导依赖镜像自身（见 R6） |
| N4 | 不管理虚拟机生命周期（启动、停止、克隆） | 仅提供可选的 `vmrun -T ws register`；失败不致命 |
| N5 | 不支持 ESXi / vSphere / Fusion 专有特性 | 实验环境为 Workstation 17.6.3（见 R7） |
| N6 | 不支持非 512 字节逻辑扇区的镜像 | `SECTOR_SIZE` 契约固定为 512 |
| N7 | 不做 GUI / 向导 | 定位是可组合的 CLI |
| N8 | 不修改、不移动、不改名用户的镜像文件 | 只读取 `st_size`，写入范围限于 descriptor/vmx 输出路径 |

---

## 5. 核心思路

### 5.1 为什么一条 extent 行就能工作

VMware 打开磁盘的调用链（真机日志可见，见 `VERIFICATION.md` §1 判定标准）：

```
vmx: <controller>:0.fileName = "disk.vmdk"
        │  （descriptor 与 vmx 同目录 → 用裸文件名即可）
        ▼
解析 descriptor：version / createType / extent 行 / DDB
        │
        ▼
对每条 extent：虚拟 LBA ──(offset + LBA) × 512──> 数据文件字节偏移
        │
        ▼
以读写方式打开数据文件（raw 镜像）
        │
        ▼
DISK: OPEN '<desc>' Geo (130/16/63)
DISKUTIL: ide0:0 : capacity=131072 logical sector size=512
Transitioned vmx/execState/val to poweredOn
```

映射公式（`FLAT` 类型，`offset` 单位是**扇区**）：

```
byte_offset(虚拟扇区 LBA) = (offset + LBA) × 512
```

只要满足三件事，raw 镜像就"原地"变成一块可用虚拟盘：

1. **单条 extent，`offset = 0`**，段长 `num = 文件字节数 // 512`
   → 公式退化为 `byte_offset = LBA × 512`，即**恒等映射**，虚拟盘字节序列与镜像完全一致；
2. **`version=1` 存在** → 否则解析器直接判非法盘（EXP7d/7f/7h）；
3. **extent 访问位写 `RW`** → 否则 `INVALIDDISK`（EXP6c/6d）。

其余所有字段（`CID`、`createType`、整个 DDB 段）都是**可省的**（EXP7a/7b/7c），
详见 5.2.4 与 EXP7g 的"最小字段集"。

> **`createType="monolithicFlat"` 的含义**：单文件 flat 盘 —— descriptor 描述布局，
> 数据全部平铺在另一个数据文件里（本项目中就是 raw 镜像本身），extent 类型写 `FLAT`。
> 该类型被官方工具认可：`vmware-vdiskmanager -e` 对本项目式的手写 descriptor 报
> `Disk chain is consistent.`（EXP5a），`-r … -t 1` 也能以它为源完成转换（EXP5c）。

### 5.2 正确的 descriptor 结构

#### 5.2.1 完整模板（本项目实际产出，64 MiB 镜像、`-a scsi`）

```
# Disk DescriptorFile
version=1
encoding="UTF-8"
CID=fffffffe
parentCID=ffffffff
createType="monolithicFlat"

# Extent description
RW 131072 FLAT "/tmp/designdemo/demo.img" 0

# The Disk Data Base
#DDB

ddb.virtualHWVersion = "4"
ddb.adapterType = "lsilogic"
ddb.geometry.cylinders = "130"
ddb.geometry.heads = "16"
ddb.geometry.sectors = "63"
```

（该文件由 `img-vmware-patch link … -a scsi` 实际生成，非示意。）

#### 5.2.2 extent 行逐字段

```
RW <num> FLAT "<数据文件路径>" <offset>
```

| 字段 | 含义 | 本项目的取值与依据 |
| --- | --- | --- |
| 访问位 | `RW` / `RO` | **必须 `RW`**：写 `RO` 即使文件可读可写也判非法盘（EXP6d：`INVALIDDISK`）。**不是权限声明，是格式硬约束** |
| `<num>` | 该 extent 覆盖的扇区数 | `文件字节数 // 512`，**向下取整**（见 6.2，EXP4b：向上取整 → `INVALIDDISK`） |
| `FLAT` | 映射类型：文件字节即盘扇区字节 | 恒定；这是零拷贝的关键 |
| `<路径>` | 数据文件位置 | 绝对路径 + 正斜杠 + `"` 转义（见 5.3）；相对路径同样可用（EXP1a） |
| `<offset>` | 该 extent 起点在文件中的扇区偏移 | `0`（单文件 flat 的唯一合理取值） |

#### 5.2.3 ⚠ 勘误一：extent 必须是**一行**，不能两行

早期设计稿示例：

```
RW 63 FLAT "image-flat.vmdk" 0
RW 4194304 FLAT "image-flat.vmdk" 63      ← 错误，不要照抄
```

这条示例同时犯了两个错：

1. **它不是单文件 flat 盘的真实写法**。单文件盘的 extent 永远只有**一行**：
   段长等于整个数据文件、偏移为 0（或省略 offset 字段）。证据：

   - 本项目实际产出（6.3.3）：`RW 131072 FLAT "/tmp/designdemo/demo.img" 0`；
   - `VERIFICATION.md` §1 的全部实验模板均为单行 extent；
   - 实验目录中 `vmware-vdiskmanager` 的自产物同样是单行（只是类型为 `SPARSE`）：
     `ref/ref64.vmdk` → `RW 131072 SPARSE "ref64.vmdk"`，
     `ref/ref512.vmdk` → `RW 1048576 SPARSE "ref512.vmdk"`，均无 offset 字段（缺省 0）。

   多行 extent 是**分卷盘**（`image-s001.vmdk`、`image-s002.vmdk`…，每条指向**不同文件**、
   各覆盖一段连续数据）的写法；把两条 extent 指向**同一个文件**、第二条再给一个
   `offset`，等于把同一份数据区间交给两个段重新划分，一旦 offset / 段长与文件实际布局
   不匹配，就会出现区间重叠或越界。
2. **容量口径必然对不上，产生重叠/越界**。两条 extent 相加声明
   `63 + 4194304 = 4194367` 个虚拟扇区，同时第二条要求数据文件至少有
   `63 + 4194304 = 4194367` 个扇区：
   - 若文件是 2 GiB（`4194304` 扇区），第二段越过文件末尾 **63 个扇区**，
     VMware 越界读取 → 与 EXP4b 同型的 `INVALIDDISK`；
   - 若文件确实有 `4194367` 扇区，则磁盘容量与文件大小、
     以及作者"整盘 4194304 扇区"的本意三者互不相等，
     前 63 个扇区被单独切成一个"磁道段"，后续段从文件偏移 63 重新覆盖 ——
     **两段对文件区间的划分与文件实际布局错位，任何一条的 `offset` 与段长不匹配
     就会落成区间重叠或空洞**。

顺带一提：`63` 恰好等于本项目采用的 `sectors/track`（16 heads × 63 sectors 中的 63），
说明原稿把 **CHS 磁道边界**误当成了 extent 的切分点 —— extent 切分与 CHS 无关。

**正确写法（唯一）**：

```
RW <total_sectors> FLAT "<img 路径>" 0
```

其中 `total_sectors = img 字节数 // 512`。实现中该行由
`descriptor.extent_line()` 单独渲染，且有单测 `test_single_extent_line_format`
与 `test_extent_sectors_match_total_sectors` 锁死。

#### 5.2.4 字段可省略性与本项目的取舍

`VERIFICATION.md` §2.6 实测的字段矩阵：

| 字段 | 可否省略 | 证据 |
| --- | --- | --- |
| `version=1` | **不可**（硬要求） | EXP7d / 7f / 7h → `INVALIDDISK`（error 15） |
| extent 行 | **不可** | 没有 extent 就没有数据 |
| `CID` | 可 | EXP7b 成功 |
| `parentCID` | 可 | EXP7a 成功（整段 DDB 已省） |
| `createType` | 可 | EXP7c 成功 |
| 整个 DDB 段 | 可 | EXP7a 成功，**但 CHS 回退为 64/32** |
| **最小可用集** | `version=1` + 一行 extent | **EXP7g 成功上电** |

**取舍**：本项目仍写全 `CID` / `parentCID` / `createType` / DDB 段，原因是：

1. 省略 DDB 会让 CHS 回退成 VMware 的 `64 heads / 32 sectors` 内部默认值，
   与 VMware 自产 IDE 盘（及本项目采用）的 `16/63` 不一致（EXP7a/7j），依赖 BIOS CHS 的老系统
   （DOS、部分 Windows 安装程序、某些 bootloader）可能读盘失败；
2. 显式写出 `ddb.geometry` 后 VMware 确实采用手写几何
   （EXP8：日志 `Geo (1040/16/63)`）；
3. 完整模板让产物可被 `vmware-vdiskmanager -e` 判为一致（EXP5a），
   也让人工排查时一眼看清参数。

### 5.3 路径策略：相对 vs 绝对（实测结论 + 推荐）

#### 5.3.1 早期设计稿的错误论断

原稿称"descriptor 中数据文件路径需为绝对路径，否则 VMware 可能无法定位"。
**真机实验直接证伪**：

| 实验 | extent 中的路径 | 结果 | 结论 |
| --- | --- | --- | --- |
| EXP1a | `RW 131072 FLAT "test.img" 0`（相对） | **成功**，`DISKUTIL: ide0:0 : capacity=131072` | **相对路径可用**，以 **descriptor 所在目录**为基准解析（结论引自 `VERIFICATION.md`；实验分辨极限见 5.3.2） |
| EXP1b | `RW 131072 FLAT "/tmp/…/test.img" 0`（绝对） | **成功** | 绝对路径可用 |
| EXP2 | `RW 131072 FLAT "my img dir/my img.img" 0`（相对 + 空格） | **成功** | 双引号包裹的空格路径可用（目录名与文件名含空格均可） |

#### 5.3.2 实测结论与推荐策略

- **两者都能开机**。`VERIFICATION.md` 对 EXP1a 的判定是：相对路径以
  **descriptor 所在目录**为基准解析。
  **如实说明实验的分辨极限**：全部实验中 descriptor、`.vmx` 与镜像都放在同一目录，
  因此实验能证明的只是"相对**该目录**"解析，**无法进一步区分基准是 descriptor 目录
  还是 vmx 目录**（两者重合），也无法证明与进程工作目录无关；
  本项目强制 descriptor 与 `.vmx` 同目录（见 6.5.3），这一歧义在本项目内不产生任何差异。
- **本项目默认输出绝对路径**（`CONTRACT.md` 规定：绝对路径 + 正斜杠 +
  `"` 转义为 `\"`）。理由：
  - 无歧义：不依赖"descriptor 被谁移动到哪"，镜像可以在任意目录；
  - 与 `-o` 指定任意输出路径的场景天然兼容（`link -o /elsewhere/x.vmdk` 时
    相对路径会指向错误位置）；
  - 单测覆盖 Windows 盘符 / UNC / 中文 / 空格 / `..` 折叠等全部分支。
- **代价与对策**：绝对路径使"整目录搬迁"后需要手工改 descriptor 中的一行。
  若目标是可搬迁的 VM 包，把 extent 改成相对路径即可（实测可用，EXP1a），
  解析基准为 descriptor 所在目录。未来工作提供 `--relative` 开关（见第 11 节）。
- **空格**：整条路径已用双引号包裹，无需额外转义（EXP2 通过）；
  **`"` 必须转义为 `\"`**（`escape_path()`）。

### 5.4 `ddb.adapterType` 的真实角色（勘误三）

早期设计稿称"`ddb.adapterType` 会因 `sata` 而影响兼容性"。**实测不成立**：

| 实验 | 配置 | 结果 |
| --- | --- | --- |
| EXP3b | `sata0:0` + `ddb.adapterType = "sata"` | 成功，**无任何 warning/error 行** |
| EXP3b2 | `sata0:0` + `ddb.adapterType = "ide"`（不匹配） | 成功 |
| EXP3b3 | `sata0:0` + `ddb.adapterType = "lsilogic"`（不匹配） | 成功 |
| EXP3d | `scsi0:0` + `ddb.adapterType = "ide"`（不匹配） | 成功 |
| EXP7e | `ddb.adapterType = "bogus-adapter"`（乱写） | **成功上电** |

**结论**：VMware Workstation 17.6.3 的开机路径**完全不校验** `ddb.adapterType`；
**控制器实际类型只由 `.vmx` 的 `ide0:0` / `sata0:0` / `scsi0:0` 决定**
（日志证据：`DISK: OPEN sata0:0 …` / `DISK: OPEN scsi0:0 …`）。
旁证：`vmware-vdiskmanager -c -s 64MB -a sata -t 0` 命令行接受了 `-a sata`（exit=0），
但落盘的 descriptor 里写的却是 `ddb.adapterType = "lsilogic"`（EXP5e），
说明 `"sata"` 根本不是 VMware 自己会写入的取值。

**因此本项目中 `-a/--adapter` 是双通道开关**：

1. **真正起作用的通道**：决定 `.vmx` 的控制器键（`ide0` / `sata0` / `scsi0`
   及 `scsi0.virtualDev = "lsilogic"`）——见 6.4；
2. **元数据通道**：把对应字面值写进 `ddb.adapterType`，用于标注与生态兼容
   （`vmware-vdiskmanager -e`、ESX/vSphere 场景、人工阅读）。
   它**不参与兼容性判定**，写错也不会让 VM 起不来 —— 但本项目仍按契约写正确值。

### 5.5 `.vmx` 集成的核心决策

最小可上电 `.vmx` 的字段集如下。**充分性**（写这些就能开机）由两处证据支撑：
`VERIFICATION.md` §1 的实验配置（30+ 台实验机均按此上电）与本项目 8.4 冒烟
（更精简、无网卡的版本）。最后一列如实区分：哪些行有**省略对照**，哪些只是沿用实验配置、**未做省略对照**：

| 键 | 为什么 | 必要性的证据边界 |
| --- | --- | --- |
| `.encoding = "UTF-8"` | 非 ASCII 值的编码声明 | 实验配置均含，无省略对照 |
| `config.version = "8"` | 配置格式版本 | 同上 |
| `virtualHW.version = "19"` | 硬件兼容性版本（19 = Workstation 17；实验配置用 20） | 实验配置均含；8.4 冒烟证明 19 可上电 |
| `displayName` / `guestOS` / `memsize` | 身份与资源（VMware 对缺失项的行为未做省略对照） | 实验配置均含 |
| `<controller>:0.present = "TRUE"` + `<controller>:0.fileName = "<descriptor 裸文件名>"` | **挂载磁盘的开关与指向**；`fileName` 用裸名 → descriptor 与 `.vmx` 必须同目录（6.5.3） | 实验配置与冒烟均含 |
| `<controller>.present = "TRUE"`（父级控制器行） | 控制器整体开关 | **有省略对照**：`VERIFICATION.md` §1 的 IDE 最小配置并未写 `ide0.present`，仍成功上电；sata/scsi 实验均写了。本项目三种控制器一律写，属"写上更稳"而非硬要求 |
| `scsi0.virtualDev = "lsilogic"`（仅 scsi） | 指定 SCSI 控制器型号 | scsi 实验配置含此行；"不写时由 VMware 补默认值"**未做省略对照** |
| `floppy0.present = "FALSE"` | 显式关闭软驱，规避实验中恒定出现的 `Could not connect to floppy "/dev/fd0"` 警告（该警告本身无害，见 8.2 #17） | **可选**：只影响告警噪音，不影响上电 |

**明确不写**：`usb_xhci.present = "TRUE"`（以及任何 PCIe 桥设备）。
最小配置缺少 PCIe bridge 时该键会让**整机上电失败**（勘误六）：

```
[msg.pci.noslotavail] No PCIe slot available for usb_xhci.  Remove usb_xhci and try again.
Module 'DevicePowerOn' power on failed.
Error: The operation was canceled          ← vmrun 的返回
```

> 该失败与磁盘完全无关，是最容易被误判成"descriptor 写错了"的陷阱。

---

## 6. 详细设计

### 6.1 模块划分与数据流

```
                ┌────────────────────────────────────────────┐
                │ cli.py  (argparse 编排 / 错误→退出码映射)   │
                └───┬──────────┬──────────┬──────────┬───────┘
                    │          │          │          │
        compute_geometry   build_descriptor build_vmx  find_vmrun / get_version /
                    │          │          │          register_vm / install_hint
                    ▼          ▼          ▼          ▼
             geometry.py  descriptor.py  vmx.py   vmware.py
             (纯计算)     (纯计算+写文件) (纯计算)  (路径探测 + 可注入 runner 的子进程)
```

| 模块 | 职责 | I/O 边界 |
| --- | --- | --- |
| `geometry.py` | 字节数 → `Geometry`（扇区/CHS） | **纯函数**：不打开文件、不打印、不退出 |
| `descriptor.py` | 路径规范化、extent 行渲染、descriptor 全文渲染、写文本文件 | 只有 `write_text_file()` 做写盘 |
| `vmx.py` | 最小 `.vmx` 渲染与写盘 | 写盘复用 `write_text_file()` |
| `vmware.py` | 定位 `vmrun` / `vmware-vdiskmanager`、取版本、注册 | 探测阶段**只查路径不执行**；子进程只在显式调用 `get_version`/`register_vm` 时发生，且 `runner` 可注入（单测注入假实现） |
| `cli.py` | 参数解析、校验顺序、错误分类与退出码、用户可见输出 | 唯一的 stdout/stderr 出口 |

**模块不变式**（单测强制）：

- `geometry` / `descriptor.build_*` / `vmx.build_vmx` 无 I/O、无 `sys.exit`；
- `descriptor` 与 `vmx` 不导入 `subprocess`；
- `vmware.find_*` 不执行任何程序（纯 `os.path.isfile` + `shutil.which`）；
- `cli` 中只有 `CommandError` / `OutputError` / `OSError` 会被转换成退出码。

### 6.2 几何计算（`geometry.py`）

#### 6.2.1 常量

```python
SECTOR_SIZE     = 512      # 字节/扇区（契约固定）
HEADS           = 16       # 磁头数（契约固定）
SECTORS         = 63       # 每磁道扇区数（契约固定）
CYLINDER_SECTORS = 16*63 = 1008
```

#### 6.2.2 算法

```python
def compute_geometry(file_size: int, *, force: bool = False) -> Geometry:
    if not isinstance(file_size, int):      raise GeometryError(...)   # 类型防护
    if file_size < 0:                       raise GeometryError(...)

    effective = file_size
    if file_size % 512:
        if not force:  raise ImageAlignmentError(file_size)   # exit 1
        effective = file_size - (file_size % 512)             # ⚠ 向下取整截断

    total_sectors = effective // 512                # ⚠ floor，绝不是 ceil
    cylinders     = total_sectors // 1008           # ⚠ 只计整柱面
    if cylinders < 1:
        raise ImageTooSmallError(total_sectors, file_size)   # exit 1

    return Geometry(file_size=effective, total_sectors=total_sectors,
                    cylinders=cylinders, heads=16, sectors=63,
                    original_size=file_size)
```

`Geometry` 是 `frozen dataclass`，附带属性：`source_size`（截断前）、`truncated`、
`remainder_sectors`（尾部不完整柱面的扇区数）、`chs` / `chs_string` / `describe()`。

#### 6.2.3 三条硬规则与证据

| 规则 | 依据 | 违反后果（实测报错原文） |
| --- | --- | --- |
| **扇区数 = `file_size // 512`（floor）** | EXP4a：64 MiB+1 B 写 `RW 131072 …` → **成功**，`capacity=131072`；EXP4c：64 MiB−1 B 写 `RW 131071 …` → 成功 | EXP4b（ceil，多报 1 扇区）→ exit 255：`[msg.disklib.INVALIDDISK] The file specified is not a virtual disk`，日志 `DISK: Cannot open disk '…': The file specified is not a virtual disk (15).` |
| **非 512 倍数默认拒绝（exit 1）** | `CONTRACT.md`；真机"多报一个扇区即非法"佐证了**不能静默向上补齐** | `error: file size 67108865 is not a multiple of 512 bytes (镜像未按 512 字节对齐)` + hint |
| **`--force` 时向下截断到 512 边界** | 与 floor 规则同源：截断后 `total_sectors` 恰为文件可支持的最大值 | stderr 告警：`warning: file size 67108865 is not a multiple of 512 bytes; truncated to 67108864 bytes (131072 sectors)` |

> **勘误四**：早期设计稿的"文件大小不是 512 倍数 → 截断处理"描述不完整且方向危险——
> 关键不是"要不要截断"，而是**扇区数必须向下取整**。向上取整（哪怕只多 1 个扇区）
> 会让 extent 声明的字节数超过文件实际大小，VMware 直接判为非虚拟盘。
> 另外默认行为是**拒绝**（exit 1），只有显式 `--force` 才截断。

#### 6.2.4 与 VMware 自产盘的公式一致性（为什么取 `16/63` 与 floor）

`VERIFICATION.md` §2.7 用 `vmware-vdiskmanager -c` 复验了 VMware 自己的算法：

| 对照盘 | 扇区数 | VMware 写出的 CHS | 公式核对 |
| --- | --- | --- | --- |
| 64 MB | 131072 | `130 / 16 / 63` | `131072 // 1008 = 130.03 → 130` ✅ |
| 512 MB | 1048576 | `1040 / 16 / 63` | `1048576 // 1008 = 1040.25 → 1040` ✅ |

即对 **IDE 盘**，VMware 自己用的正是 `cyl = floor(扇区数 / (16×63))`、16 heads / 63 sectors。
本项目与之逐字一致；EXP8 进一步证明手写的几何会被真正采用
（日志 `DISK: OPEN '…' Geo (1040/16/63)`）。

> **一处如实记录的差异**：同轮实验用 `vmware-vdiskmanager -c -s 64MB -a sata -t 0`
> 生成的对照盘（EXP5e，产物 `ref/sata.vmdk`）落盘的 DDB 几何是 **`64 / 64 / 32`**
> （`floor(131072 / 2048) = 64`），而不是 16/63 —— 与"descriptor 省略 DDB 时的回退几何"
> 相同（见 8.2 #14）。这说明该字段的取值与创建参数有关，**并非只有一种合法答案**；
> 但只要 descriptor 里显式写出 `16/63`，VMware 就照单全收
> （EXP3a/3b/3c、EXP8 与本项目三控制器冒烟均以 16/63 成功上电）。
> 这正是"**始终显式写 `ddb.geometry`**"的理由：不给它回退的机会。

**余数处理**：`remainder_sectors = total_sectors − cylinders × 1008`
仍留在 extent 内，由"尾部不完整柱面"承载，**不被丢弃、不参与 `cylinders` 计数**
（64 MiB → 130 柱面 + 32 余扇区，`info` 会打印 `partial: 32 sectors …`）。
这样 `ddb.geometry` 描述整柱面数、extent 覆盖全部数据，与 VMware 产物语义一致。

#### 6.2.5 边界行为表（实现 + 单测锁定）

| 输入 | `force` | 结果 | 退出码 |
| --- | --- | --- | --- |
| 512 B（1 扇区） | — | `ImageTooSmallError`（不足 1 个完整柱面） | 1 |
| 516096 B（1008 扇区） | — | `Geometry(1/16/63)`，无 partial 行 | 0 |
| 67108864 B（64 MiB） | — | `Geometry(130/16/63)`，partial = 32 | 0 |
| 67108865 B | 否 | `ImageAlignmentError` | 1 |
| 67108865 B | 是 | `Geometry(130/16/63)`，`original_size=67108865` + 告警 | 0 |
| 67108863 B | 是 | `Geometry(130/16/63)`：截断到 67108352 B = 131071 扇区，`131071 // 1008 = 130`，余 31 | 0 |
| 负数 / 非 int | — | `GeometryError` | 1 |

> 注：64 MiB（131072 扇区，余 32）与 64 MiB−1 B（131071 扇区，余 31）的 CHS
> 同为 `130/16/63` —— 柱面数只计**完整**柱面，尾部余数扇区仍全部包含在 extent 中。
> 与 EXP4c 对照可见取整方向的决定性影响：131071 扇区的镜像写 `RW 131071 …` 正常上电
> （`capacity=131071`），写 `RW 131072 …`（ceil）即 `INVALIDDISK`。

### 6.3 路径规范化与 descriptor 渲染（`descriptor.py`）

#### 6.3.1 `normalize_path(path, *, base=None, windows=None) -> str`

| 步骤 | POSIX | Windows（`windows=True` 或 `os.name == "nt"`） |
| --- | --- | --- |
| 相对路径拼接 | `posixpath.join(base, text)`（`base` 默认 `os.getcwd()`） | `ntpath.join(base, text)` |
| 已是绝对路径 | 原样保留 | 原样保留（含盘符 `C:\…` 与 UNC `\\server\share`） |
| 规范化 | `posixpath.normpath`（词法折叠 `.`/`..`/重复 `/`，**不解析符号链接、不触碰文件系统**） | `ntpath.normpath` 后再把 `\` **全部替换为 `/`** |
| 输出 | `/path/img.img` | `C:/path/img.img`、`//server/share/img.img` |

要点：

- **输入相对 → 输出绝对**（相对 `base`，默认 cwd）；因此 `extent_line()` 的输出
  永远是绝对路径，符合契约；
- **始终正斜杠**：VMware descriptor 使用正斜杠，反斜杠会被当作转义字符；
- `..` 折叠是**词法**的：不验证路径是否存在、不展开符号链接（避免 I/O、保持纯函数）；
- 字节型路径（`bytes`）经 `os.fsdecode` 解码。

#### 6.3.2 转义与 extent 行

```python
escape_path(p)            # '"' -> '\"'   仅此而已
extent_line(n, path)      # 'RW {n} FLAT "{normalized}" 0'
```

- 路径整体已用 `"` 包裹，**空格、中文、`[`、`]` 等无需转义**（EXP2 实测空格可用；
  中文由单测覆盖渲染，真机未测 → 见 R5）；
- `"` 若不转义会截断字符串字面量，导致 extent 解析失败。

#### 6.3.3 `build_descriptor()` 输出顺序（与 5.2.1 模板逐行对应）

```
# Disk DescriptorFile
version=1
encoding="UTF-8"
CID=fffffffe
parentCID=ffffffff
createType="monolithicFlat"
<空行>
# Extent description
RW <total_sectors> FLAT "<abs path>" 0
<空行>
# The Disk Data Base
#DDB
<空行>
ddb.virtualHWVersion = "4"
ddb.adapterType = "<ide|sata|lsilogic>"
ddb.geometry.cylinders = "<n>"
ddb.geometry.heads = "16"
ddb.geometry.sectors = "63"
```

- 固定值：`CREATE_TYPE = "monolithicFlat"`、`VIRTUAL_HW_VERSION = "4"`、
  `CID = fffffffe`（新盘未关联父盘）、`parentCID = ffffffff`（无父盘）；
- 结尾强制单个 `\n`；写文件用 `newline="\n"`（**跨平台一律 LF**，
  避免 Windows CRLF 干扰解析器）。

#### 6.3.4 `write_text_file(path, content, *, force=False)`

校验顺序与异常：

1. `Path(path).is_dir()` → `OutputIsDirectoryError`（**即使 `--force` 也报错**）；
2. 存在且 `force=False` → `OutputExistsError`；
3. `target.parent.mkdir(parents=True, exist_ok=True)`（自动创建 `--dir` 层级）；
4. `open("w", encoding="utf-8", newline="\n")` 写入 → 由 `cli` 捕获 `OSError`
   并映射为退出码 1。

`cli._ensure_writable()` 在 `create` 中会**先**对 descriptor 与 vmx 两个目标做同样的
存在性/目录检查，避免"写完 descriptor 才发现 vmx 已存在"的部分写入状态。

### 6.4 适配器映射（`descriptor.py` + `vmx.py`）

| CLI `-a` | `ddb.adapterType`（**元数据，不校验**） | `.vmx` 控制器键（**真正生效**） | 额外 vmx 键 | 真机状态 |
| --- | --- | --- | --- | --- |
| `ide`（默认） | `ide` | `ide0:0.*` | — | EXP3a ✅ / 冒烟 ✅ |
| `sata` | `sata` | `sata0:0.*`（并写 `sata0.present`） | — | EXP3b ✅（`DISK: OPEN sata0:0`）/ 冒烟 ✅ |
| `scsi` | `lsilogic` | `scsi0:0.*` | `scsi0.virtualDev = "lsilogic"` | EXP3c ✅（`DISK: OPEN scsi0:0`）/ 冒烟 ✅ |
| 非法值 | — | — | — | `ValueError` → `cli` 报参数错误（argparse `choices` 已先行拦截，退出码 2） |

`descriptor.adapter_type()` 与 `vmx.build_vmx()` 各自独立校验取值（白名单来自
`ADAPTER_TYPES` / `ADAPTER_CONTROLLER`），未知值抛 `ValueError`，与契约一致。

### 6.5 `.vmx` 集成（`vmx.py`）

#### 6.5.1 字段清单

| 键 | 值来源 | 备注 |
| --- | --- | --- |
| `.encoding` | 常量 `UTF-8` | |
| `config.version` | 常量 `"8"` | |
| `displayName` | `-n/--name` | 经 `_quote()`：`\` → `\\`、`"` → `\"` |
| `guestOS` | `-g/--guest`，默认 `otherlinux-64` | |
| `memsize` | `-m/--ram`，默认 `2048` | 必须为正整数，否则 `ValueError` |
| `virtualHW.version` | 常量 `"19"`（Workstation 17） | 与 descriptor 的 `ddb.virtualHWVersion="4"` 是两个不同字段 |
| `<controller>.virtualDev` | 仅 `scsi` → `"lsilogic"` | |
| `<controller>.present` / `<controller>:0.present` | 由 `with_present=True` 输出 | `create` 用 `True` |
| `<controller>:0.fileName` | `descriptor_path.name`（**裸文件名**） | 恒输出 |
| `floppy0.present` | `"FALSE"`（仅 `with_present=True`） | |

#### 6.5.2 `with_present` 的两种形态

| 调用方 | `with_present` | 用途 |
| --- | --- | --- |
| `cli.cmd_create` | `True`（默认） | 生成可直接启动的完整最小配置 |
| 库用户 / 模板渲染 | `False` | 只输出通用键 + `fileName`，省略 `*.present` 与 `floppy0` 行 |

`scsi0.virtualDev` **在 `with_present=False` 时仍然输出**（单测
`test_scsi_virtualdev_survives_without_present` 锁定）—— 它与"是否 present"无关。

#### 6.5.3 同目录约束

`fileName` 是裸名 → descriptor 必须与 `.vmx` 同目录。`cli.cmd_create` 的处理：

1. `descriptor_path` 默认 `--dir/<NAME>.vmdk`，`vmx_path` 恒为 `--dir/<NAME>.vmx`；
2. 若用户用 `-o` 把 descriptor 指到别处，比较
   `descriptor_path.parent.resolve() != vm_dir.resolve()`，不一致则
   **stderr 打印警告**（不失败、退出码不变），提示 `.vmx` 引用的只是裸文件名；
3. `README` 同步告知"两者必须同目录"。

### 6.6 VMware 探测与注册（`vmware.py`）

| 函数 | 行为 | 关键约束 |
| --- | --- | --- |
| `default_candidates(system, tool)` | 按 `platform.system()` 返回硬编码候选路径 | Linux（`vmrun` / `vmware-vdiskmanager` 各一条）：`/usr/bin/vmrun`、`/usr/bin/vmware-vdiskmanager`；Windows `vmrun` 四条：`C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe`、`C:\Program Files\VMware\VMware Workstation\vmrun.exe` 及对应两条 Player 路径；Windows `vmware-vdiskmanager` 两条 Workstation 路径 |
| `find_tool` / `find_vmrun` / `find_vdiskmanager` | 依次 `os.path.isfile(candidate)`，再 `shutil.which(tool)` | **不执行任何程序**；探测阶段零副作用，可安全用于 `link`/`info`（`cli` 虽在模块级 `import vmware`，但 `link`/`info` 的代码路径**从不调用** `find_vmrun()`，单测 `test_never_probes_vmware` 锁定） |
| `get_version(path, runner=None)` | 执行工具一次（无参数调用），从其 stdout/stderr 中抽取首个形如 `1.17.0.25688693` 的版本号 | `runner` 可注入；10 s 超时；失败返回 `None`（不抛） |
| `register_vm(vmx, vmrun=None, runner=None, timeout=60)` | `vmrun -T ws register <vmx>` | **永不抛异常**：超时/OSError/非 0 一律返回 `(False, detail)` |
| `install_hint(system)` | 未找到 `vmrun` 时的多行安装提示 | 明确"只有 create 需要 VMware" |

**设计取舍**：注册失败**不致命**（契约要求）。本机实测（见 7.1.5 的 `create` 样例）中
`vmrun -T ws register` 返回 `Error: register VM not supported for the specified host type`，
工具打印 warning + 手工注册提示后**仍以 0 退出** —— 文件已经落盘，
注册是可后补的独立动作。

### 6.7 CLI 编排与错误处理（`cli.py`）

#### 6.7.1 校验与执行顺序（`create` 为例）

```
1. _check_image        → 文件不存在 / 不是常规文件        → exit 1
2. _load_geometry      → 非 512 对齐（无 -f）/ 太小       → exit 1
3. _validate_name      → 空名 / 非法字符                 → exit 2
4. --dir 与 -o 检查    → 目录不存在可创建；输出已存在无 -f → exit 1
5. mkdir --dir（含父目录）
6. 写 descriptor → 写 vmx（任一 OutputError/OSError）     → exit 1
7. find_vmrun          → 未找到                          → exit 3（文件已写好）
8. 注册开关 / get_version / register_vm → 失败仅告警      → exit 0
```

顺序说明：**镜像存在性与几何（退出码 1）先于虚拟机名校验（退出码 2）**，
即"镜像不存在 + 虚拟机名非法"会返回 1；与 VMware 有关的问题（退出码 3）
**一律排在所有文件写入之后**，保证"能写就先写完，缺少 VMware 不影响产物"。
descriptor 与 `.vmx` 的写入在同一个 `try` 块内，且写入前已由 `_ensure_writable()`
统一做过存在性检查（避免"写完 descriptor 才发现 vmx 已存在"的部分写入状态）。

#### 6.7.2 错误分类表（实测消息）

| 错误情形 | 异常类型 | 退出码 | stderr 实测输出 |
| --- | --- | --- | --- |
| 镜像不存在 | `CommandError` | 1 | `error: image file not found: …` |
| 路径不是常规文件 | `CommandError` | 1 | `error: image path is not a regular file: …` |
| 非 512 倍数（`link`/`create` 无 `-f`） | `ImageAlignmentError` → `CommandError` | 1 | `error: file size 67108865 is not a multiple of 512 bytes (镜像未按 512 字节对齐)` + `hint: use -f/--force to truncate the size to a 512-byte boundary` |
| 非 512 倍数（`info`，本命令无 `-f`） | 同上 | 1 | 同上，hint 为 `this command has no --force; align the image or use link/create with -f/--force` |
| 镜像太小（< 1 柱面） | `ImageTooSmallError` | 1 | `error: image too small (镜像太小): … one cylinder needs 1008 sectors …` |
| 输出已存在（无 `-f`） | `OutputExistsError` → `CommandError` | 1 | `error: output file already exists: …` + `hint: use -f/--force to overwrite` |
| 输出路径是目录 | `OutputIsDirectoryError` | 1 | `error: output path is a directory: …` |
| `--dir` 已存在但不是目录 | `CommandError` | 1 | `error: --dir is not a directory: …` |
| 写盘权限/IO 失败 | `OSError` | 1 | `error: [Errno 13] Permission denied: …` |
| VM 名含 `/ \ : * ? " < > \|` 或控制字符 | `CommandError(code=2)` | 2 | `error: invalid VM name 'bad/name': forbidden character(s) '/'` + `hint: avoid path separators and Windows-reserved characters` |
| VM 名为空 | `CommandError(code=2)` | 2 | `error: VM name must not be empty` |
| 缺必填参数 / 未知子命令 / `-a` 非法值 | argparse | 2 | argparse usage 到 stderr |
| `-m 0` / `-m abc` / `-g ""` | argparse `ArgumentTypeError` | 2 | `must be a positive number of MB, got 0` / `value must not be empty` |
| `create` 未找到 `vmrun` | — | **3** | `error: vmrun not found (VMware 未安装或不在 PATH 中)` + 安装提示 + `note: descriptor and .vmx were written; register later with: vmrun -T ws register …` |
| `vmrun register` 失败 | — | **0** | `warning: vmrun register failed: …` + `hint: register manually with: …` |

> 退出码 3 在本机无法实测（`/usr/bin/vmrun` 存在），由单测
> `CreateTest.test_without_vmrun_returns_three_but_writes_files`
> （mock `find_vmrun → None`，断言 `code == EXIT_VMWARE` 且两个文件仍已写入）覆盖；
> 其余退出码 0/1/2 均已实机复现（第 7.2 节）。

#### 6.7.3 输出约定

- **stdout**：结构化结果（`descriptor:` / `image:` / `extent:` / `geometry:` /
  `adapter:` / `vmx:` / `vmrun:` / `registration:` 等键值行），
  固定列对齐，便于 `grep`/`awk`；
- **stderr**：一切诊断（`warning:` / `error:` / `hint:` / `note:`）；
- 截断告警、descriptor 越出 `--dir` 的告警、注册失败告警 → stderr；
- `main()` 只捕获 `CommandError` / `OutputError` / `OSError`，
  其余异常照常抛出（属于 bug，不应被吞掉）。

#### 6.7.4 注册开关

`IMG_VMWARE_PATCH_REGISTER` ∈ `{0, false, no, off, skip, disabled}`（大小写不敏感）
→ 跳过注册并打印 `registration: skipped (IMG_VMWARE_PATCH_REGISTER)`，退出码 0。
其余值（含未设置）→ 正常注册。用于无 VMware 或禁止注册的 CI 环境。

### 6.8 测试设计（`tests/`，120 例，`python3 -m unittest discover -s tests -t .` 全绿）

| 文件 | 用例数 | 覆盖重点 |
| --- | --- | --- |
| `test_geometry.py` | 17 | 512 B / 1 MB+1 B / 非 512 倍数 / 柱面恰好整除 / `-f` 截断 / 太小 / 负数与类型防护 |
| `test_descriptor.py` | 36 | POSIX 与 Windows 路径规范化（盘符、UNC、反斜杠、`..`、空格、中文、bytes）、`"` 转义、**单条 extent 行格式与扇区一致性**、adapter 映射、完整 descriptor 快照、写文件（已存在 / 目录 / 强制覆盖 / 父目录创建） |
| `test_vmx.py` | 16 | 三控制器键、`fileName` 为裸名、`with_present` 两态、`scsi0.virtualDev` 保留、值转义、非法参数 |
| `test_cli.py` | 51 | 三个子命令的 **0/1/2/3 全部返回码**、`-f` 行为、默认输出路径、`--dir` 警告、注册开关、**`info`/`link` 从不触发 VMware 探测**、`--help`/`--version` |

**铁律**：单测中 `vmrun` 探测永远被 mock，测试套件不启动任何虚拟机
（真机验证独立于单测，见第 8 节）。

---

## 7. 接口设计

### 7.1 CLI（以下为 `python3 -m img_vmware_patch.cli --help` 的**实际输出**，2026-09-26 运行）

#### 7.1.1 顶层

```
usage: img-vmware-patch [-h] [-v] <command> ...

Generate a VMDK descriptor for a raw disk image and optionally prepare a
minimal VMware virtual machine around it.

positional arguments:
  <command>
    link         仅生成 VMDK descriptor 文件
    create       生成 descriptor 并创建/注册虚拟机
    info         显示 .img 文件信息和计算出的几何参数

options:
  -h, --help     show this help message and exit
  -v, --version  show program's version number and exit
```

`img-vmware-patch --version` → `img-vmware-patch 0.1.0`

#### 7.1.2 `link`

```
usage: img-vmware-patch link [-h] [-o PATH] [-a {ide,sata,scsi}] [-f] IMAGE

仅生成 VMDK descriptor 文件（不依赖 VMware）。

positional arguments:
  IMAGE                 .img 镜像文件路径

options:
  -h, --help            show this help message and exit
  -o, --output PATH     输出 descriptor 路径（默认 <IMAGE 同目录同名>.vmdk）
  -a, --adapter {ide,sata,scsi}
                        磁盘适配器类型 {ide,sata,scsi}（默认 ide）
  -f, --force           覆盖已存在的输出文件，并容忍非 512 对齐的镜像
```

默认输出路径由 `image.with_suffix(".vmdk")` 得到（`disk.raw` → `disk.vmdk`）。

#### 7.1.3 `create`

```
usage: img-vmware-patch create [-h] -n NAME [-m MB] [-g GUEST] --dir PATH
                               [-o PATH] [-a {ide,sata,scsi}] [-f]
                               IMAGE

生成 descriptor 与 .vmx，并尝试用 vmrun 注册虚拟机。

positional arguments:
  IMAGE                 .img 镜像文件路径

options:
  -h, --help            show this help message and exit
  -n, --name NAME       虚拟机名称（displayName，同时作为 .vmx/.vmdk 文件名）
  -m, --ram MB          内存大小 MB（默认 2048）
  -g, --guest GUEST     guestOS 类型（默认 otherlinux-64）
  --dir PATH            虚拟机目录（.vmx 与 descriptor 所在目录）
  -o, --output PATH     输出 descriptor 路径（默认 <DIR>/<NAME>.vmdk，须与 .vmx 同目录）
  -a, --adapter {ide,sata,scsi}
                        磁盘适配器类型 {ide,sata,scsi}（默认 ide）
  -f, --force           覆盖已存在的输出文件，并容忍非 512 对齐的镜像
```

`-n/--name` 与 `--dir` 为**必填**（argparse `required=True`）。

#### 7.1.4 `info`

```
usage: img-vmware-patch info [-h] IMAGE

显示 .img 文件信息和计算出的几何参数（不依赖 VMware）。

positional arguments:
  IMAGE       .img 镜像文件路径

options:
  -h, --help  show this help message and exit
```

`info` **没有** `-f`：非 512 对齐一律 exit 1，并提示改用 `link`/`create` 的 `-f`。

#### 7.1.5 实际输出样例

```
$ img-vmware-patch info /tmp/designdemo/demo.img
image:      /tmp/designdemo/demo.img
size:       67108864 bytes (64.00 MiB)
sector:     512 bytes
sectors:    131072
geometry:   130 cylinders x 16 heads x 63 sectors
chs:        130/16/63
partial:    32 sectors in the trailing incomplete cylinder

$ img-vmware-patch link /tmp/designdemo/demo.img -o /tmp/designdemo/demo.vmdk -a scsi
descriptor: /tmp/designdemo/demo.vmdk
image:      /tmp/designdemo/demo.img
extent:     RW 131072 FLAT "/tmp/designdemo/demo.img" 0
geometry:   130 cylinders x 16 heads x 63 sectors
adapter:    scsi -> ddb.adapterType=lsilogic

$ img-vmware-patch create /tmp/designdemo/demo.img -n DemoVM --dir /tmp/designdemo/vm -a sata
descriptor: /tmp/designdemo/vm/DemoVM.vmdk
vmx:        /tmp/designdemo/vm/DemoVM.vmx
geometry:   130 cylinders x 16 heads x 63 sectors
extent:     RW 131072 FLAT "/tmp/designdemo/demo.img" 0
vmrun:      /usr/bin/vmrun
vmrun version: 1.17.0.25688693
warning: vmrun register failed: Error: register VM not supported for the specified host type
hint: register manually with: vmrun -T ws register /tmp/designdemo/vm/DemoVM.vmx
$ echo $?
0
```

### 7.2 退出码

| 码 | 含义 | 实测状态 |
| --- | --- | --- |
| 0 | 成功（含"注册失败但文件已写好"） | ✅ 实机复现 |
| 1 | 通用错误：未对齐（无 `-f`）、镜像太小、输入缺失、输出已存在、IO 错误 | ✅ 实机复现 |
| 2 | 参数错误：argparse usage、非法 VM 名/内存/ guest 值 | ✅ 实机复现 |
| 3 | `create` 找不到 `vmrun`（descriptor 与 `.vmx` 已写入） | 单测覆盖（本机装有 `vmrun`） |

### 7.3 环境变量

| 变量 | 取值 | 作用 |
| --- | --- | --- |
| `IMG_VMWARE_PATCH_REGISTER` | `0` / `false` / `no` / `off` / `skip` / `disabled`（大小写不敏感） | 跳过 `create` 的 `vmrun -T ws register`，仍写文件，退出 0；其他值等价于开启 |

### 7.4 Python API（`from img_vmware_patch import …`）

```python
# geometry.py —— 纯计算
SECTOR_SIZE: int            # 512
HEADS: int                  # 16
SECTORS: int                # 63
CYLINDER_SECTORS: int       # 1008
@dataclass(frozen=True)
class Geometry:
    file_size: int; total_sectors: int; cylinders: int
    heads: int = 16; sectors: int = 63; original_size: int | None = None
    # properties: cylinder_sectors, source_size, truncated,
    #             remainder_sectors, chs, chs_string; method: describe()
def compute_geometry(file_size: int, *, force: bool = False) -> Geometry
class GeometryError(ValueError); class ImageAlignmentError; class ImageTooSmallError

# descriptor.py —— 渲染与写盘
ADAPTER_TYPES: dict[str, str]     # {"ide": "ide", "sata": "sata", "scsi": "lsilogic"}
DEFAULT_ADAPTER: str              # "ide"
CREATE_TYPE: str                  # "monolithicFlat"
VIRTUAL_HW_VERSION: str           # "4"
def adapter_type(adapter: str) -> str
def normalize_path(path, *, base: str | None = None,
                   windows: bool | None = None) -> str
def escape_path(path: str) -> str
def extent_line(total_sectors: int, image_path, **norm_kwargs) -> str
def build_descriptor(image_path, geometry: Geometry, *,
                     adapter: str = "ide", **norm_kwargs) -> str
def write_text_file(path, content: str, *, force: bool = False) -> pathlib.Path
class OutputError(Exception); class OutputExistsError; class OutputIsDirectoryError

# vmx.py —— 最小配置
ADAPTER_CONTROLLER: dict[str, str]   # ide->ide0, sata->sata0, scsi->scsi0
DEFAULT_GUEST_OS: str                # "otherlinux-64"
DEFAULT_RAM_MB: int                  # 2048
VIRTUAL_HW_VERSION: str              # "19"
def build_vmx(*, display_name: str, descriptor_name: str, adapter: str = "ide",
              ram_mb: int = 2048, guest_os: str = "otherlinux-64",
              with_present: bool = True) -> str
def write_vmx(path, content: str, *, force: bool = False) -> pathlib.Path

# vmware.py —— 探测与注册（link/info 不需要）
LINUX_VMRUN_PATHS / WINDOWS_VMRUN_PATHS / LINUX_VDISK_PATHS / WINDOWS_VDISK_PATHS
def default_candidates(system: str | None = None, tool: str = "vmrun") -> tuple[str, ...]
def find_tool(tool, *, system=None, candidates=None, which=None) -> str | None
def find_vmrun(...) -> str | None
def find_vdiskmanager(...) -> str | None
def get_version(tool_path: str, *, runner=None) -> str | None
def register_vm(vmx_path, *, vmrun=None, runner=None,
                timeout: int = 60) -> tuple[bool, str]
def install_hint(system: str | None = None) -> str

# cli.py
EXIT_OK = 0; EXIT_ERROR = 1; EXIT_USAGE = 2; EXIT_VMWARE = 3
REGISTER_ENV = "IMG_VMWARE_PATCH_REGISTER"
def build_parser() -> argparse.ArgumentParser
def main(argv: Sequence[str] | None = None) -> int
class CommandError(Exception)   # .code / .hint
```

---

## 8. 真机验证结论汇总

### 8.1 验证环境与判定标准（引自 `VERIFICATION.md` §1）

- **日期**：2026-09-26；**环境**：Linux Mint 22.3，VMware Workstation **17.6.3**
  （`vmrun 1.17.0.25688693`，`vmware-vdiskmanager build 25688693`）；
- **实验位置**：`/tmp/opencode/imgverify/`（未触碰用户已有虚拟机）；
- **判定标准**：`vmrun -T ws start … nogui` 退出码 = 0 **且** `vmware.log` 出现
  `Transitioned vmx/execState/val to poweredOn` **且** `DISK: OPEN …` / `DISKUTIL: … capacity=…`
  表明磁盘被正确解析；每台实验后 `stop hard` 并确认退出；
- **规模**：30+ 台实验机，逐项对照（详见原文 2.1–2.7、3、4 节）；
- **已知限制**：测试 img 全为 0、无引导扇区，故"能开机"指**上电成功 + 磁盘被接受并打开**
  （关机时 `ide0:0: numIOs = 2` 说明 BIOS 阶段确实读了扇区），**未验证 OS 级启动**。

### 8.2 主题结论汇总

| # | 主题 | 实测结论 | 证据（`VERIFICATION.md`） | 对设计的落实 |
| --- | --- | --- | --- | --- |
| 1 | **extent 行数** | 单文件 flat 只能一行、`offset=0`、长度 = 文件扇区数 | §1 实验模板；§2.7 `vdiskmanager` 产物 | `extent_line()` 只产一行（6.3.2、5.2.3） |
| 2 | **相对 vs 绝对路径** | **两者都能开机**；相对路径以 **descriptor 所在目录**为基准（实验中 descriptor 与 `.vmx` 同目录，无法区分二者，见 5.3.2） | EXP1a / EXP1b | 实现输出绝对路径；迁移时可改相对（5.3） |
| 3 | **路径含空格** | 可用，双引号包裹即可（目录名/文件名均可含空格） | EXP2 | 路径整体加引号、`"` 转义（6.3.2） |
| 4 | **三种控制器** | `ide` / `sata` / `scsi` **全部能上电** | EXP3a / EXP3b / EXP3c | `-a` 同时决定 vmx 控制器键与 ddb 元数据（6.4） |
| 5 | **`ddb.adapterType`** | **Workstation 完全不校验**，写 `bogus-adapter` 也照常开机；控制器只由 vmx 决定 | EXP3b2 / EXP3b3 / EXP3d / EXP7e | 该字段按元数据处理（5.4） |
| 6 | **`vdiskmanager -a sata`** | 接受参数但落盘写 `lsilogic` | EXP5e | 佐证 `sata` 非 VMware 自产取值（5.4） |
| 7 | **容量非 512 倍数** | **必须 floor**：多报 1 扇区即 `INVALIDDISK` 起不来 | EXP4a（floor ✅）/ EXP4b（ceil ❌，error 15）/ EXP4c | `file_size // 512` + 默认拒绝 + `-f` 截断（6.2.3） |
| 8 | **`vdiskmanager` 兼容性** | `-e` 报 `Disk chain is consistent.`(0)；`-r … -t 1` 转换成功且不改源文件 | EXP5a / EXP5c | 手写 descriptor 被官方工具认可 |
| 9 | **只读 img** | **不可**：444 → `FILENOPERMISSION`(38)，VMware 以 O_RDWR 打开 | EXP6 / EXP6b | 风险 R1：必须可写、无只读挂载 |
| 10 | **extent 访问位** | **必须 `RW`**；写 `RO` 即 `INVALIDDISK`（与文件权限无关） | EXP6c / EXP6d | `extent_line()` 恒写 `RW` |
| 11 | **descriptor 只读** | **可以** | EXP6e | 允许 `chmod 444 *.vmdk` |
| 12 | **VM 目录可写** | **必须**，否则连 `vmware.log` 都不生成、上电失败 | EXP6f | 风险 R2；`create` 自动 `mkdir` 且告警 |
| 13 | **最小字段集** | **`version=1` + 一行 extent 即可开机**；`CID`/`parentCID`/`createType`/DDB 均可省；缺 `version=1` 必失败 | EXP7g ✅ / EXP7d·7f·7h ❌ / EXP7a·7b·7c | 仍写全字段以固定 CHS（5.2.4） |
| 14 | **省略 DDB 的 CHS 回退** | 回退为 **64 heads / 32 sectors**（`cyl = floor(扇区数/2048)`），与 VMware 标准 **16/63** 不符 | EXP7a（`Geo 64/64/32`）、EXP7j（`63/64/32`、`512/64/32`） | **始终写出 `ddb.geometry` 16/63**（6.2.4） |
| 15 | **手写几何被采纳** | 写 `1040/16/63` → 日志 `Geo (1040/16/63)` | EXP8 | DDB 几何确有作用（非装饰） |
| 16 | **`usb_xhci.present="TRUE"`** | **整机上电失败**（与磁盘无关）`[msg.pci.noslotavail] No PCIe slot available for usb_xhci.` | §4.1 | 最小 vmx **不声明任何 PCIe/USB 桥设备**（5.5） |
| 17 | **软驱警告** | `Could not connect to floppy "/dev/fd0"` 属**无害**，不影响开机 | §4.2 | `floppy0.present = "FALSE"` 直接规避（5.5） |
| 18 | **VMware 不改写用户数据** | 多次开机后 `img` 与 `vmdk` 大小/内容/mtime 均未变；`.lck` 关机自清 | §4.3 | 与 N8（不修改用户镜像）一致 |

### 8.3 报错速查表（原文摘录）

| 报错原文 | 触发条件 | 处置 |
| --- | --- | --- |
| `[msg.disklib.INVALIDDISK] The file specified is not a virtual disk`（error 15） | extent 扇区数 > 文件实际扇区数（ceil）；缺 `version=1`；extent 写 `RO` | 扇区数 floor；补 `version=1`；访问位写 `RW` |
| `[msg.disklib.FILENOPERMISSION] Insufficient permission to access file`（error 38） | 数据文件（img）权限只读 | `chmod u+w <img>`；**没有"真只读挂载"开关** |
| `Error: A file access error occurred on the host or guest operating system`（且无 `vmware.log`） | VM 目录不可写 | 换可写目录或放开权限 |
| `[msg.pci.noslotavail] No PCIe slot available for usb_xhci.` | vmx 声明 `usb_xhci.present` 但无 PCIe bridge | 删除该行（本项目不生成） |
| `[msg.disk.noBackEnd] Cannot open the disk '…' or one of the snapshot disks it depends on.` | 上述磁盘类错误的连锁报错 | 看其上一条 `msg.disklib.*` |
| `register VM not supported for the specified host type` | 本机 `vmrun -T ws register` | 非致命：工具打印提示并退出 0，可手工注册或在 GUI 中打开 |
| `Could not connect to floppy "/dev/fd0"` | 无软驱主机 | 无害（本项目已 `floppy0.present=FALSE`） |

### 8.4 本设计文档写作期间的补充冒烟验证

`VERIFICATION.md` 的实验配置与本工具**产出的配置存在若干差异**（实验用
`virtualHW.version = "20"`、`ddb.virtualHWVersion = "13"`、含 `ethernet0`/`numvcpus`/
`powerType`/`ddb.uuid`；工具产出 `virtualHW.version = "19"`、`ddb.virtualHWVersion = "4"`、
更精简、无网卡）。为确认这些差异不引入回归，2026-09-26 在同一台
Workstation 17.6.3 主机上补做端到端冒烟：

```bash
IMG_VMWARE_PATCH_REGISTER=0 PYTHONPATH=src python3 -m img_vmware_patch.cli \
    create /tmp/designdemo/demo.img -n DesignCheck --dir <DIR> -a <adapter>
vmrun -T ws start <DIR>/DesignCheck.vmx nogui
grep -E 'poweredOn|DISK: OPEN|Geo \(' <DIR>/vmware.log
vmrun -T ws stop <DIR>/DesignCheck.vmx hard
```

| 适配器 | `start` 退出码 | 关键日志 | `stop` 退出码 |
| --- | --- | --- | --- |
| `ide` | 0 | `DISK: OPEN ide0:0 '…/DesignCheck.vmdk' persistent R[]`、`Geo (130/16/63)`、`DISKUTIL: ide0:0 : capacity=131072 logical sector size=512`、`Transitioned vmx/execState/val to poweredOn` | 0 |
| `sata` | 0 | `DISK: OPEN sata0:0 …`、`Geo (130/16/63)`、`poweredOn` | 0 |
| `scsi` | 0 | `DISK: OPEN scsi0:0 …`、`Geo (130/16/63)`、`poweredOn` | 0 |

- 镜像 `md5 = 7f614da9329cd3aebf59b91aadc30bf0`，冒烟前后 **mtime 与内容均未变化**；
- 三种适配器用的都是**工具原样产出**的 descriptor 与 `.vmx`（未做任何手工修改）；
- 同一主机上另有一次**未关闭注册**的 `create` 运行（见 7.1.5 样例）复现了
  `vmrun register` 失败提示并**以 0 退出**，实证"注册失败不致命"这一设计；
- 结论：`virtualHW.version=19`、`ddb.virtualHWVersion="4"`、无网卡的精简 `.vmx`
  在 Workstation 17.6.3 上可正常上电；冒烟后 `vmrun -T ws list` = `Total running VMs: 0`，
  实验目录已删除。

---

## 9. 替代方案

### 9.1 产品层面的替代方案

见第 3 节（转换式 / GUI / 手工 / 直通）。它们与本方案的取舍关系：

| 维度 | 转换式（A/B） | 本方案（C/E′） |
| --- | --- | --- |
| 时间 | O(镜像大小) | **O(1)** |
| 空间 | +100% | +1 KiB 文本 |
| 依赖 | qemu-img / VMware / GUI | **无**（`link`/`info`） |
| 可脚本化 | 部分 | **完全** |
| 数据修改 | 只读源 | **guest 直接写穿镜像**（R1） |
| 换宿主机 | 需再迁移大数据 | descriptor 一行路径（或改相对路径） |

**什么时候不该用本方案**：需要保护原始镜像不被 guest 修改、需要快照回滚、
需要把镜像当只读证据时 —— 应使用转换式或先做副本。

### 9.2 设计层面的替代方案（已否决/延后）

| 备选 | 描述 | 结论 |
| --- | --- | --- |
| 始终输出相对路径 | 与 `-o` 指向任意目录冲突，且需保证 img 与 descriptor 的相对关系稳定 | **延后**：作为未来 `--relative` 选项（第 11 节），默认仍绝对 |
| 只输出最小字段集（`version=1` + extent） | 更"精简"，EXP7g 证明可行 | **否决**：省略 DDB 导致 CHS 回退 64/32（EXP7a/7j），与 VMware 标准 16/63 不符，老系统有风险 |
| 用 `vmware-vdiskmanager -q/-r` 作为后端生成 descriptor | 需要 VMware，违背 G3 | **否决**：仅作为可选自检（未来工作） |
| 生成含网卡/USB/声卡的"完整" `.vmx` | 更像真实 VM | **否决**：`usb_xhci` 已证明会致上电失败（§4.1），最小化最稳；网卡等由用户按需添加 |
| 用 `virtualHW.version = "20"`（与实验一致） | 与 VERIFICATION 实验配置完全对齐 | **暂缓**：19 = WS17 契约值，且 8.4 冒烟已证明 19 可上电；若后续在更高版本 Workstation 上出问题再评估 |
| 以软链接/循环挂载伪造 raw | 需要 root，且仍不解决 VMware 只认 vmdk | **否决** |

---

## 10. 风险与限制

| 编号 | 风险 / 限制 | 影响 | 缓解 |
| --- | --- | --- | --- |
| **R1** | **guest 对镜像拥有读写权**：`FLAT` 是 persistent 后端，VMware 以 `O_RDWR` 打开 img（EXP6：444 直接 `FILENOPERMISSION`），**没有任何"只读挂载"开关**；guest 写入直接落到原镜像 | 原镜像被污染/破坏，且**无快照、无 undo** | 使用前自行备份/做副本；只读证据场景不要用本方案；文档显著标注（README + 本文） |
| **R2** | **VM 目录必须可写**（EXP6f 连 `vmware.log` 都不生成） | 目录只读时上电失败，报错与磁盘无关 | `create` 自动 `mkdir`；错误提示指向目录权限 |
| **R3** | **绝对路径绑定**：img 移动或整目录搬迁后 descriptor 失效 | `INVALIDDISK`/找不到文件 | 手工改 extent 为相对路径（EXP1a 证明可行）；未来 `--relative`；`link` 的 `extent:` 输出便于核对 |
| **R4** | **非 512 对齐只能 floor 截断**，尾部最多 511 字节不进虚拟盘 | 尾部数据丢失（若镜像尾部有关键数据） | 默认拒绝 + 显式 `-f` + stderr 告警 |
| **R5** | **非 ASCII（中文）路径未做真机验证**，仅单测覆盖渲染；`encoding="UTF-8"` 依赖 VMware 按 UTF-8 解析 | 中文路径可能在某些主机 locale 下解析失败 | 未来工作：补真机用例；当前建议先用英文路径 |
| **R6** | **只验证"上电 + 磁盘被接受"，未验证 OS 引导**（实验镜像全 0 无引导扇区） | 真实引导问题（引导器、分区表、驱动）不在保证范围 | 引导问题属镜像自身；`numIOs`、`DISKUTIL capacity` 可用于判断磁盘侧是否正常 |
| **R7** | **仅在 Linux + Workstation 17.6.3 实测**；未覆盖 Windows 主机、Player、Fusion、ESXi/vSphere | 其他平台行为未知（如 Windows 路径大小写、盘符） | `normalize_path` 双平台语义有单测；ESX 相关只推断不承诺（如 `ddb.adapterType="sata"` 可能对 ESX 有意义） |
| **R8** | **`virtualHW.version=19` / 精简 vmx 与 VERIFICATION 实验配置不同** | 理论上存在回归风险 | 已由 8.4 三适配器冒烟覆盖（均上电成功） |
| **R9** | **`vmrun register` 在本机返回 `host type` 不支持** | `create` 的注册环节在某些主机上不可用 | 设计上即非致命：警告 + 手工注册提示 + 退出 0；可 `IMG_VMWARE_PATCH_REGISTER=0` 直接关闭 |
| **R10** | **CID 固定为 `fffffffe`**，多台 VM 引用同一 img、或 VM 打开后被写入 | 理论上存在 CID/父盘链语义冲突；本项目不支持快照与父盘 | 单机单 VM 使用；不要对同一 img 并发挂多台 VM；`vdiskmanager -e` 可自检 |
| **R11** | **并发写同一输出文件无锁**（`write_text_file` 非原子写） | 并行任务同时 `link` 到同一路径可能互相覆盖 | 使用 `-f` 的场景由调用方保证串行；未来可写临时文件 + `os.replace` |
| **R12** | **退出码 3 无真机覆盖**（本机装有 `vmrun`） | 该分支只有单测保障 | mock 单测已锁定；CI 中可在无 VMware 容器内跑一次 |

---

## 11. 未来工作

1. **`--relative` / `--path {abs,rel,auto}`**：输出相对 descriptor 目录的 extent 路径
   （EXP1a 已证明可行），解决 R3 的可搬迁性问题；
2. **`--minimal`**：输出 `version=1` + 单行 extent 的最小 descriptor（EXP7g 已验证），
   供只需"能打开"的场景；
3. **`link --verify`**：若 `vmware-vdiskmanager` 存在则自动跑 `-e` 自检
   （EXP5a 证明可判 `Disk chain is consistent.`），把第 8 节的部分结论变成可执行断言；
4. **真机 e2e 冒烟脚本**：把 8.4 的三适配器冒烟固化为 `tests/e2e_vmware.sh`
   （标注为 opt-in，普通单测仍不碰 `vmrun`），覆盖退出码 3 的实机分支；
5. **非 ASCII 路径真机验证**（R5），以及 Windows 主机上的路径语义复验（R7）；
6. **`--adapter` 之外的控制器参数**：`scsi0.virtualDev` 可选值（`lsilogic`/`pvscsi`/
   `buslogic`）、`sata0:0.virtualDev` 等；
7. **guestOS 预设表**：按常见镜像类型给出 `-g` 建议（当前只有 `otherlinux-64` 默认值）；
8. **可选设备开关**：`--ethernet e1000|vmxnet3|none`（默认不生成网卡，需同时验证
   PCIe 相关键不会触发 `noslotavail`）；
9. **原子写入**（R11）：临时文件 + `os.replace`；
10. **文档回流**：把第 8 节结论与附录 A 勘误回填到 `README` 的"Geometry rules"段
    与任何仍引用旧描述的地方（README 现状已与实现一致，仅缺少真机证据引用）。

---

## 附录 A：原设计稿勘误表

下表逐条列出早期设计稿中**已被证实错误或需修正**的论断，以及本文档的改正位置。

| # | 原设计稿论断 | 实测/代码事实 | 本文档位置 |
| --- | --- | --- | --- |
| 1 | §4.1 descriptor 示例含两行 extent：`RW 63 FLAT "image-flat.vmdk" 0` + `RW 4194304 FLAT "image-flat.vmdk" 63` | 既非单文件 flat 的真实写法（VMware 自产只有一行），两段合计 4194367 扇区还会与 2 GiB 文件容量冲突，导致越界/区间重叠；正确写法是单行 `RW <total_sectors> FLAT "<img 路径>" 0` | 5.2.1 / **5.2.3** / 6.3.2 |
| 2 | "descriptor 中数据文件路径需为绝对路径，否则 VMware 可能无法定位" | **相对路径可用**（以 descriptor 所在目录为基准，EXP1a 成功），绝对路径同样可用（EXP1b）；空格路径加双引号即可（EXP2） | **5.3**（含实测结论与推荐策略） |
| 3 | "`ddb.adapterType` 会因 `sata` 而影响兼容性" | Workstation 17.6.3 **完全不校验**该字段（`bogus-adapter` 也能开机，EXP7e；`sata`/`ide`/`lsilogic` 不匹配也不报错 EXP3b2/3b3/3d）；控制器类型**只由 vmx 的 `ide0:0`/`sata0:0`/`scsi0:0` 决定**；`vdiskmanager -a sata` 落盘实际写 `lsilogic`（EXP5e） | **5.4** / 6.4 |
| 4 | "文件大小不是 512 倍数 → 截断处理"（未指明取整方向） | **必须 floor**：`扇区数 = 字节数 // 512`；ceil（哪怕多 1 扇区）→ `[msg.disklib.INVALIDDISK] The file specified is not a virtual disk`(15)，exit 255（EXP4b）；floor 成功（EXP4a/4c）；且默认**拒绝**、仅 `-f` 才截断 | **6.2.2 / 6.2.3** / 6.2.5 |
| 5 | 第十节"待解决问题"：相对/绝对路径、占用、SATA/SCSI 的 `RW` 行参数等 | `VERIFICATION.md` 已给出实测答案（见其 §3 汇总表） | **第 8 节**（"真机验证结论汇总"，全部改为已验证结论 + 证据编号） |
| 6 | （原稿遗漏的实测风险） | ① 只读 img → `FILENOPERMISSION`(38)（EXP6）；② extent 访问位必须 `RW`，写 `RO` → `INVALIDDISK`(15)（EXP6c/6d）；③ 省略 DDB 时 CHS 回退 **64 heads / 32 sectors**，与 VMware 标准 16/63 不符（EXP7a/7j）；④ 最小字段集 = `version=1` + 一行 extent（EXP7g），缺 `version=1` 必失败（EXP7d/7f/7h）；⑤ vmx 写 `usb_xhci.present="TRUE"` → `No PCIe slot available` 整机上电失败（§4.1） | **5.2.4 / 5.5 / 6.2.4 / 8.2（#9、#10、#13、#14、#16）/ 8.3 / 10（R1）** |

---

## 附录 B：参考文件与复现命令

### B.1 参考文件

| 文件 | 作用 |
| --- | --- |
| `CONTRACT.md` | CLI/几何/descriptor/vmx 的实现契约 |
| `VERIFICATION.md` | 真机验证报告（实验编号 EXP1a…EXP8、报错原文、结论汇总） |
| `README.md` / `README.zh-CN.md` | 用户文档 |
| `CHANGELOG.md` | 版本变更 |
| `src/img_vmware_patch/{geometry,descriptor,vmx,vmware,cli}.py` | 实现 |
| `tests/` | 120 例单元测试 |

### B.2 复现命令

```bash
# 单元测试（不依赖 VMware）
cd /home/wang/img-vmware-patch && python3 -m unittest discover -s tests -t .

# 查看接口（本文第 7 节即为其输出）
PYTHONPATH=src python3 -m img_vmware_patch.cli --help
PYTHONPATH=src python3 -m img_vmware_patch.cli link --help
PYTHONPATH=src python3 -m img_vmware_patch.cli create --help
PYTHONPATH=src python3 -m img_vmware_patch.cli info --help

# 生成并检查
dd if=/dev/zero of=/tmp/demo.img bs=1M count=64
PYTHONPATH=src python3 -m img_vmware_patch.cli info /tmp/demo.img
PYTHONPATH=src python3 -m img_vmware_patch.cli link /tmp/demo.img -o /tmp/demo.vmdk -a scsi

# 创建虚拟机（跳过注册）并上电验证
IMG_VMWARE_PATCH_REGISTER=0 PYTHONPATH=src python3 -m img_vmware_patch.cli \
    create /tmp/demo.img -n Demo --dir /tmp/demo-vm -a ide
vmrun -T ws start /tmp/demo-vm/Demo.vmx nogui
grep -E 'poweredOn|DISK: OPEN|capacity=' /tmp/demo-vm/vmware.log
vmrun -T ws stop /tmp/demo-vm/Demo.vmx hard      # 务必停止
```

> 本项目所有实验/冒烟 VM 均在 `/tmp/opencode/` 下创建，完成后 `vmrun -T ws list`
> 确认 `Total running VMs: 0` 并删除实验目录；未触碰 `~/.vmware` 中的用户虚拟机。
