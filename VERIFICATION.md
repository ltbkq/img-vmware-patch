# raw .img 直挂 VMware Workstation —— VMDK Descriptor 技术验证报告

- **验证日期**：2026-09-26
- **环境**：Linux Mint 22.3 / kernel 7.3.0-070300rc3-generic，VMware Workstation **17.6.3**（`vmrun 1.17.0.25688693`，`vmware-vdiskmanager build 25688693`）
- **实验位置**：`/tmp/opencode/imgverify/`（全部实验 VM 均在此目录下创建，未触碰 `~/.vmware` 中用户已有虚拟机，未删除用户家目录文件）
- **对应设计文档**：第十节「待解决问题」、5.2 / 5.3 节
- **原始日志**：`/tmp/opencode/imgverify/results.log`（含每次 `vmrun start` 退出码、`vmware.log` 关键行摘录）

## 1. 方法与判定标准

测试镜像：`dd if=/dev/zero bs=1M count=64`（67108864 字节 = 131072 扇区）；CHS 验证另造 512 MB。

descriptor（`*.vmdk`）为纯文本，`createType="monolithicFlat"`，extent 行形如：

```
RW 131072 FLAT "test.img" 0
```

vmx 只挂一块盘（`ide0:0.fileName = "test.vmdk"`，或 `sata0:0` / `scsi0:0`），最小配置：

```
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "20"
displayName = "exp"
guestOS = "other3xlinux-64"
numvcpus = "1"
memsize = "256"
ide0:0.present = "TRUE"
ide0:0.fileName = "test.vmdk"
ethernet0.present = "TRUE"
ethernet0.virtualDev = "e1000"
powerType = "suspend"
```

**成功判定**：`vmrun -T ws start <vmx> nogui` 退出码 = 0 **且** `vmware.log` 中出现
`Transitioned vmx/execState/val to poweredOn`，且日志中 `DISK: OPEN … / DISKUTIL: … capacity=…` 表明磁盘被正确解析。
每台 VM 实验后立即 `vmrun -T ws stop <vmx> hard`，并用 `vmrun -T ws list` 确认退出。

> **限制说明**：测试 img 全为 0，无引导扇区，故 BIOS 不会真正载入操作系统；本文"能开机"指
> **虚拟机成功上电、磁盘被 VMware 接受并打开**（关机时 `ide0:0: numIOs = 2` 说明 BIOS 阶段确实读了该盘扇区），
> 未验证 OS 级启动。

---

## 2. 实验结果总表

### 2.1 路径形态（问题 1、2）

| 实验项 | 配置 | 结果 | 结论 | VMware 报错原文 |
|---|---|---|---|---|
| EXP1a 相对路径 | descriptor 在 VM 目录，extent 写 `RW 131072 FLAT "test.img" 0`（相对） | **成功**（exit=0，poweredOn，`DISKUTIL: ide0:0 : capacity=131072`） | **相对路径可用**，以 **descriptor 所在目录**为基准解析 | 无 |
| EXP1b 绝对路径 | extent 写 `RW 131072 FLAT "/tmp/opencode/imgverify/exp1b_abspath/test.img" 0` | **成功**（exit=0，poweredOn） | **绝对路径可用** | 无 |
| EXP2 路径含空格 | img 放在 `my img dir/my img.img`，extent 写 `RW 131072 FLAT "my img dir/my img.img" 0`（双引号包裹、相对） | **成功**（exit=0，poweredOn，capacity=131072） | **双引号包裹的空格路径可用**，目录名与文件名含空格均可 | 无 |

### 2.2 三种控制器 / ddb.adapterType（问题 3）

| 实验项 | 配置 | 结果 | 结论 | VMware 报错原文 |
|---|---|---|---|---|
| EXP3a IDE | `ide0:0.present` + `ddb.adapterType = "ide"` | **成功** | IDE 基线可用 | 无 |
| EXP3b SATA + `"sata"` | `sata0.present`/`sata0:0` + `ddb.adapterType = "sata"` | **成功**（exit=0，`DISK: OPEN sata0:0 …`，capacity=131072） | **写 `"sata"` 完全不报错**，Workstation 接受该字面值 | **无任何报错**（无 warning/error 行） |
| EXP3b2 SATA + `"ide"` | 同上，`ddb.adapterType = "ide"` | **成功** | 对照：控制器类型与 adapterType **不匹配也不报错** | 无 |
| EXP3b3 SATA + `"lsilogic"` | 同上，`ddb.adapterType = "lsilogic"` | **成功** | 同上 | 无 |
| EXP3c SCSI | `scsi0.present`、`scsi0.virtualDev = "lsilogic"`、`scsi0:0` + `ddb.adapterType = "lsilogic"` | **成功**（`DISK: OPEN scsi0:0 …`，capacity=131072） | **SCSI + lsilogic 可用** | 无 |
| EXP3d SCSI + `"ide"` | SCSI 控制器，但 `ddb.adapterType = "ide"` | **成功** | 对照：**不匹配也不报错** | 无 |
| EXP7e 非法 adapterType | `ddb.adapterType = "bogus-adapter"` | **成功**（poweredOn） | Workstation **完全不校验** `ddb.adapterType` 取值 | 无 |

**要点**：Workstation 17.6.3 开机路径上不校验 `ddb.adapterType`，`"sata"`、`"ide"`、`"lsilogic"`、
乱写一个值，四种情况**全部正常上电**；控制器实际类型只由 vmx 的 `ide0:0 / sata0:0 / scsi0:0` 决定。
另外 `vmware-vdiskmanager -c -s 64MB -a sata -t 0` 命令行虽接受了 `-a sata`（exit=0），
但它在生成的 descriptor 里实际写的是 `ddb.adapterType = "lsilogic"`（见 2.6），
说明 **`"sata"` 不是 VMware 自己会写入的取值**，只在 ESX/vSphere 等场景可能有意义。

### 2.3 容量非 512 倍数（问题 4）

| 实验项 | 配置 | 结果 | 结论 | VMware 报错原文 |
|---|---|---|---|---|
| EXP4a 64 MB+1 字节，**floor** | img=67108865 字节，extent 写 `RW 131072 …`（floor(67108865/512)=131072） | **成功**（exit=0，poweredOn，capacity=131072） | **扇区数向下取整即可正常开机**，多余 1 字节被忽略 | 无 |
| EXP4b 64 MB+1 字节，**ceil（对照）** | extent 写 `RW 131073 …`（多报 1 个扇区） | **失败**（exit=255） | **扇区数不能超过文件实际大小**，超出即判为"不是虚拟盘" | `The file specified is not a virtual disk`<br>`[msg.disklib.INVALIDDISK] The file specified is not a virtual disk`<br>`[msg.disk.noBackEnd] Cannot open the disk '/tmp/opencode/imgverify/exp4b_plus1_ceil/test.vmdk' or one of the snapshot disks it depends on.`<br>`[msg.moduletable.powerOnFailed] Module 'Disk' power on failed.`<br>`[msg.vmx.poweron.failed] Failed to start the virtual machine.`<br>日志：`DISK: Cannot open disk '…': The file specified is not a virtual disk (15).`<br>vmrun：`Error: The operation was canceled` |
| EXP4c 64 MB−1 字节 | img=67108863 字节，extent 写 `RW 131071 …`（floor） | **成功**（capacity=131071） | 同 floor 规则，末尾不足 512 字节直接丢弃 | 无 |

**规则**：`扇区数 = floor(文件字节数 / 512)`，**绝不向上取整**。

### 2.4 vmware-vdiskmanager 兼容性（问题 5）

| 实验项 | 配置 | 结果 | 结论 | 输出原文 / 退出码 |
|---|---|---|---|---|
| EXP5a 一致性检查 | `vmware-vdiskmanager -e exp1a_relpath/test.vmdk`（手写 monolithicFlat） | **成功** | 手写 descriptor 被 vdiskmanager 完全认可 | `Disk chain is consistent.` / **exit=0** |
| EXP5b 对照 | `-e` 对 VMware 自产 sparse 盘 | 成功 | 一致性 | `Disk chain is consistent.` / **exit=0** |
| EXP5c 转换 | `vmware-vdiskmanager -r test.vmdk -t 1 out.vmdk`（手写 descriptor） | **成功** | 可直接把 raw img 转成标准 split 盘；**源 img/descriptor 未被修改** | `Creating disk '…/out.vmdk'`<br>`  Convert: 12% done. … 100% done.`<br>`Virtual disk conversion successful.` / **exit=0** |
| EXP5d 对照 | `-r ref64.vmdk -t 1 conv.vmdk` | 成功 | 与自产盘行为一致 | `Virtual disk conversion successful.` / **exit=0** |
| EXP5e 创建（-a sata） | `vmware-vdiskmanager -c -s 64MB -a sata -t 0 ref/sata.vmdk` | **成功**，但生成盘内写的是 `ddb.adapterType = "lsilogic"` | 命令行接受 `sata`，实际落盘归一化为 `lsilogic` | `Creating disk '…'`<br>`Virtual disk creation successful.` / **exit=0** |
| EXP5f 对 sata 值检查 | `-e exp3b_sata_sata/test.vmdk`（`ddb.adapterType="sata"`） | **成功** | `sata` 取值不破坏 disk chain 检查 | `Disk chain is consistent.` / **exit=0** |

### 2.5 只读权限（问题 6）

| 实验项 | 配置 | 结果 | 结论 | VMware 报错原文 |
|---|---|---|---|---|
| EXP6 img 只读 | `chmod 444 test.img`（descriptor 644，extent 写 `RW`） | **失败**（exit=255） | **VMware 要求 img 文件可写**（以 O_RDWR 打开） | `[msg.disklib.FILENOPERMISSION] Insufficient permission to access file`<br>`[msg.disk.noBackEnd] Cannot open the disk '/tmp/opencode/imgverify/exp6_ro/test.vmdk' or one of the snapshot disks it depends on.`<br>`[msg.moduletable.powerOnFailed] Module 'Disk' power on failed.`<br>`[msg.vmx.poweron.failed] Failed to start the virtual machine.`<br>日志：`DISK: Cannot open disk '…': Insufficient permission to access file (38).`<br>vmrun：`Error: The operation was canceled` |
| EXP6b img+descriptor 都只读 | `chmod 444 test.img test.vmdk` | **失败** | 同上（报错点在 img） | 同上（`FILENOPERMISSION` / error 38） |
| EXP6c extent 写 `RO` + img 444 | `RO 131072 FLAT "test.img" 0`，img 444 | **失败** | `RO` 关键字本身就被拒 | `[msg.disklib.INVALIDDISK] The file specified is not a virtual disk`（error 15，同 2.3 EXP4b） |
| EXP6d extent 写 `RO` + img 可写 | `RO 131072 FLAT …`，img 644 | **失败** | **证明与权限无关**：extent 访问模式必须写 `RW`，`RO` 会被判为非法盘 | `[msg.disklib.INVALIDDISK] The file specified is not a virtual disk`（error 15） |
| EXP6e **仅 descriptor 只读** | `chmod 444 test.vmdk`，img 644 | **成功**（exit=0，poweredOn） | **descriptor 文件本身可以是只读的** | 无 |
| EXP6f **目录不可写** | `chmod 555` VM 目录（img/descriptor 可写） | **失败**（exit=255，**连 vmware.log 都没生成**） | **VM 目录必须可写**（要写日志/锁），否则连启动都进不去 | vmrun：`Error: A file access error occurred on the host or guest operating system` |

**只读需求结论**：`img` 必须可写、VM 目录必须可写；descriptor 可以只读；
extent 访问位必须是 `RW`（写 `RO` 直接失败）。**没有任何"真只读挂载"可用的开关**。

### 2.6 descriptor 最小字段集（问题 7）

| 实验项 | descriptor 内容 | 结果 | 结论 | VMware 报错原文 |
|---|---|---|---|---|
| EXP7a 去掉整个 DDB 段 | `version=1` / `CID` / `parentCID` / `createType` / extent | **成功**（poweredOn） | **DDB 段完全可省**；但几何回退为 `Geo (64/64/32)` | 无 |
| EXP7b 去掉 CID | 保留 `parentCID` + DDB，删 `CID=` | **成功** | **`CID` 可省** | 无 |
| EXP7c 去掉 createType | 保留 version/CID/parentCID/extent/DDB | **成功** | **`createType` 可省** | 无 |
| EXP7d 只剩 extent 一行 | `RW 131072 FLAT "test.img" 0` 单行 | **失败** | 缺 `version=1` 判为非法盘 | `[msg.disklib.INVALIDDISK] The file specified is not a virtual disk`<br>`[msg.disk.noBackEnd] Cannot open the disk '…' or one of the snapshot disks it depends on.`<br>日志：`DISK: Cannot open disk '…': The file specified is not a virtual disk (15).` |
| EXP7f 无 `version=1`（有 CID/parentCID/createType/DDB） | — | **失败** | 同上，**`version=1` 是硬要求** | 同上（error 15） |
| EXP7g **仅 `version=1` + extent** | `version=1`<br><br>`RW 131072 FLAT "test.img" 0` | **成功**（exit=0，poweredOn） | **最小可用字段集 = `version=1` + extent 行** | 无 |
| EXP7h 仅头注释 + extent（无 version） | `# Disk DescriptorFile` + extent | **失败** | 头注释不能替代 `version=1` | 同上（error 15） |
| EXP7i 头 + version + createType + extent | 无 CID/parentCID/DDB | **成功** | 推荐的"轻量但规范"写法 | 无 |
| EXP7e `ddb.adapterType="bogus-adapter"` | 完整 DDB，adapterType 为乱写 | **成功** | adapterType 不参与校验 | 无 |

### 2.7 CHS 几何（52 MB / 512 MB 验证，对应 5.2/5.3）

| 实验项 | 配置 | 结果 | 结论 |
|---|---|---|---|
| 对照：VMware 自产 64 MB 盘 | `vmware-vdiskmanager -c -s 64MB -a ide -t 0` | `ddb.geometry.cylinders="130" heads="16" sectors="63"` | VMware 自己的公式：`cyl = floor(扇区数 / (16×63))`，131072/1008 = 130.03 → **130** |
| 对照：VMware 自产 512 MB 盘 | `vmware-vdiskmanager -c -s 512MB -a ide -t 0` | `cylinders="1040" heads="16" sectors="63"` | 1048576/1008 = 1040.25 → **1040**，公式复验通过 |
| EXP8 手写 512 MB raw | `RW 1048576 FLAT "test.img" 0`，DDB 写 `1040/16/63` | **成功**（exit=0，poweredOn） | 日志 `DISK: OPEN '…' Geo (1040/16/63)` —— **VMware 确实采用 descriptor 里手写的几何** |
| EXP7a/7g/7i 无 DDB 几何 | 131072 扇区 | `Geo (64/64/32)` | 无 DDB 时回退：`heads=64, sectors=32, cyl=floor(扇区数/2048)` |
| EXP7j（g1）无 DDB，131071 扇区 | — | `Geo (63/64/32)` | 复验回退公式（floor(131071/2048)=63） |
| EXP7j（g2）无 DDB，1048576 扇区 | — | `Geo (512/64/32)` | 复验回退公式（1048576/2048=512） |

**要点**：省略 DDB 能开机，但 CHS 会变成 64/32 的回退几何，与 VMware/ESX 自产盘的 16/63 不一致；
依赖 BIOS CHS 的老系统（DOS、部分 Windows 安装程序、某些 bootloader）可能因此读盘失败。
**建议始终写出 `ddb.geometry`（16 heads / 63 sectors，`cyl = floor(扇区数/1008)`）**。
日志里 `BIOS Geo (0/0/0)` 在所有实验中恒为 0，属 Workstation 日志字段，未见影响。

---

## 3. 结论汇总（可直接回填设计文档第十节）

| 待解决问题 | 结论 |
|---|---|
| 相对路径 vs 绝对路径 | **两者都能开机**。相对路径以 **descriptor 所在目录** 为基准，跨机器迁移更稳，推荐相对路径。 |
| 路径含空格 | **可以**，extent 行里用双引号包裹即可，目录名/文件名含空格都验证通过。 |
| 三种 adapter | **ide / sata / scsi 全部能开机**。`ddb.adapterType` 写 `"sata"` **不报错**（Workstation 不校验该字段，写乱值也照样开机）；真正的控制器类型由 vmx 的 `ide0:0 / sata0:0 / scsi0:0` 决定。`vmware-vdiskmanager -c -a sata` 接受参数但落盘为 `lsilogic`。 |
| 非 512 倍数 | **必须 floor**：`扇区数 = 文件字节数 // 512`，多报 1 个扇区就会 `INVALIDDISK` 起不来。 |
| vdiskmanager 兼容性 | **完全兼容**：`-e` 报 `Disk chain is consistent.`(0)，`-r … -t 1` 报 `Virtual disk conversion successful.`(0)，且不改写源 img/descriptor。 |
| 只读 img | **不行**。img 必须可写（444 → `FILENOPERMISSION`/error 38）；extent 写 `RO` 也直接失败（`INVALIDDISK`）；VM 目录必须可写；**descriptor 文件本身可以只读**。 |
| 最小字段集 | **`version=1` + 一行 extent 即可开机**；`CID` / `parentCID` / `createType` / 整个 DDB 段都可省；但缺 `version=1` 必失败。建议仍写全 DDB 以固定 CHS 与 adapterType。 |

### 推荐的最终 descriptor 模板（验证通过）

```
# Disk DescriptorFile
version=1
CID=fffffffe
parentCID=ffffffff
createType="monolithicFlat"

# Extent description
RW <扇区数=文件字节数//512> FLAT "<img路径，相对或绝对，可含空格，须双引号>" 0

# The Disk Data Base
#DDB

ddb.adapterType = "ide"            ; SATA/SCSI 时可写 "sata"/"lsilogic"，不校验
ddb.geometry.cylinders = "<扇区数//(16*63)>"
ddb.geometry.heads = "16"
ddb.geometry.sectors = "63"
ddb.virtualHWVersion = "13"
```

---

## 4. 实验过程中的额外发现（环境/配置陷阱）

1. **vmx 缺少 PCIe bridge 时写 `usb_xhci.present = "TRUE"` 会导致整机上电失败**，与磁盘无关，报错原文：
   `[msg.pci.noslotavail] No PCIe slot available for usb_xhci.  Remove usb_xhci and try again.`
   `Module 'DevicePowerOn' power on failed.`，vmrun 返回 `Error: The operation was canceled`。
   最小 vmx 不要声明 `usb_xhci`。
2. 无软驱时日志恒有 `[msg.floppy.badConnectLong] Could not connect to floppy "/dev/fd0"` +
   `[msg.device.startdisconnected] Virtual device 'floppy0' will start disconnected.`，**属无害警告，不影响开机**。
3. 多次开机后 VM 目录会残留 `nvram` / `test.vmsd` / `test.vmxf` / `test.scoreboard` / `vmware.log`，
   **但 `test.img` 与 `test.vmdk` 的大小、内容、mtime 均未被改动**（已核对），锁目录 `.lck` 在关机后自动清除。
4. 转换生成的 `out.vmdk` 中，源 descriptor 的 DDB 几何（130/16/63）与 `adapterType=ide` 被完整继承。

## 5. 清理记录

- 每台实验 VM 均执行 `vmrun -T ws stop <vmx> hard`；
- 实验结束后删除全部含 `.vmx` 的实验目录 `/tmp/opencode/imgverify/exp*`；
- 最终 `vmrun -T ws list` 输出：`Total running VMs: 0`；
- `~/.vmware/` 下 `inventory.vmls`、`preferences`、`shortcuts` 等文件 mtime 均早于本次实验开始时间，未被改动。
