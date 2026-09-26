# 端到端验收报告（ACCEPTANCE）

- **项目**：`img-vmware-patch` 0.1.0（Python CLI，仅标准库）
- **验收日期**：2026-09-26
- **验收人**：独立验收子代理（真实工具链，非 mock）
- **环境**：Linux（kernel 7.x，Python 3.13.7），VMware Workstation **17.6.3**
  （`vmrun 1.17.0.25688693`，`vmware-vdiskmanager build 25688693`，均位于 `/usr/bin/`）
- **实验位置**：`/tmp/opencode/e2e/`（结束后已整体删除；**未触碰 `~/.vmware` 用户已有虚拟机**）
- **输入镜像**：`/tmp/opencode/e2e/disk.img`，`dd if=/dev/urandom bs=1M count=200`
  → 209715200 字节（200 MiB = 409600 扇区），`md5 = 7399fdf458cc0b7007376d9081667de2`
- **依据文档**：`CONTRACT.md`（CLI/返回码契约）、`VERIFICATION.md`（技术验证结论）

---

## 1. 验收项总表

| # | 步骤 | 命令 | 期望 | 实际 | 结论 |
|---|---|---|---|---|---|
| 1 | 造随机测试镜像 | `dd if=/dev/urandom of=/tmp/opencode/e2e/disk.img bs=1M count=200` | exit=0，200 MiB 随机数据 | exit=0，209715200 字节，md5 `7399fdf…de2` | ✅ 通过 |
| 2a | `info` | `PYTHONPATH=src python3 -m img_vmware_patch.cli info disk.img` | exit=0，输出几何 | exit=0：`sectors=409600`、`geometry: 406 cylinders x 16 heads x 63 sectors`、`chs: 406/16/63`、`partial: 352 sectors`（406×1008=409248，余 352，与 CONTRACT 公式一致） | ✅ 通过 |
| 2b | `link`（默认 ide） | `… cli link disk.img` | exit=0，生成与镜像同名的 descriptor | exit=0，生成 `/tmp/opencode/e2e/disk.vmdk`；extent = `RW 409600 FLAT "/tmp/opencode/e2e/disk.img" 0`；`adapter: ide -> ddb.adapterType=ide` | ✅ 通过 |
| 2c | `create` | `… cli create disk.img -n E2E-VM --dir /tmp/opencode/e2e/vm` | exit=0，写出 descriptor + vmx，尝试 register | exit=0；产物 `vm/E2E-VM.vmdk`(341B) + `vm/E2E-VM.vmx`(238B)；stdout 打出 `vmrun: /usr/bin/vmrun`、`vmrun version: 1.17.0.25688693`；**register 返回警告**（见问题 1），按 CONTRACT「失败不致命」仍 exit=0 | ⚠️ 通过（含已知问题） |
| 3a | 真机启动 | `vmrun -T ws start …/E2E-VM.vmx nogui` | exit=0 | exit=0 | ✅ 通过 |
| 3b | 确认在跑 | `vmrun -T ws list` | 列出该 VM | `Total running VMs: 1` + `/tmp/opencode/e2e/vm/E2E-VM.vmx`，exit=0 | ✅ 通过 |
| 3c | 磁盘被正确打开 | `grep vmware.log` | 日志显示磁盘打开与 poweredOn | `DISK: OPEN ide0:0 '…/E2E-VM.vmdk' persistent R[]`、`DISK: OPEN … Geo (406/16/63) BIOS Geo (0/0/0)`、`DISKUTIL: ide0:0 : capacity=409600 logical sector size=512`、`Transitioned vmx/execState/val to poweredOn`（几何 406/16/63 与我们写入的 DDB 完全一致） | ✅ 通过 |
| 3d | 运行 10 秒后再查 | `sleep 10` + `vmrun -T ws list` | 仍在运行 | `Total running VMs: 1`，exit=0 | ✅ 通过 |
| 3e | 强制停机 | `vmrun -T ws stop …/E2E-VM.vmx hard` | exit=0 且退出列表 | stop exit=0；随后 `list` → `Total running VMs: 0` | ✅ 通过 |
| 4 | disk chain 一致性 | `vmware-vdiskmanager -e …/E2E-VM.vmdk` | exit=0 | `Disk chain is consistent.`，**exit=0**（另测 `link` 产出的 `disk.vmdk` 与转换后的 `converted.vmdk`，均 exit=0） | ✅ 通过 |
| 5a | 转换回归 | `vmware-vdiskmanager -r …/E2E-VM.vmdk -t 1 /tmp/opencode/e2e/converted.vmdk` | exit=0，说明我们写的 descriptor 是工具链合法输入 | `Convert: 100% done.` + `Virtual disk conversion successful.`，**exit=0**；生成 `converted.vmdk` + `converted-s001.vmdk` | ✅ 通过 |
| 5b | 转换继承字段 | `cat converted.vmdk` | DDB 几何/适配器被继承 | `ddb.adapterType="ide"`、`cylinders="406" heads="16" sectors="63"`（与源 descriptor 一致）、`createType="twoGbMaxExtentSparse"`、extent `RW 409600 SPARSE "converted-s001.vmdk"` | ✅ 通过 |
| 5c | 数据层零损坏 | `md5sum` 对比转换数据层与源 img | 完全一致 | `converted-s001.vmdk` 共 209780736 字节 = 65536B 元数据头 + 209715200B 数据；`tail -c +65537 converted-s001.vmdk` → md5 **`7399fdf458cc0b7007376d9081667de2`**，与 `disk.img` **完全相同** | ✅ 通过 |
| 5d | 源文件未被改写 | 对比实验前后 `disk.img` 的 md5 / mtime | 不变 | md5 仍为 `7399fdf…de2`，mtime 仍为 `09:22:54`（早于所有 VMware 操作） | ✅ 通过 |
| 6a | 负例：非 512 倍数不带 `-f` | `… cli link unaligned.img`（67108865 字节 = 64MiB+1） | **exit=1** | exit=1：`error: file size 67108865 is not a multiple of 512 bytes (镜像未按 512 字节对齐)` + hint | ✅ 通过 |
| 6a′ | （附）`info` 同镜像 | `… cli info unaligned.img` | 1（info 无 `-f`） | exit=1，hint：`this command has no --force…` | ✅ 通过 |
| 6a″ | （附）非对齐 + `-f` | `… cli link unaligned.img -f -o …` | 0，告警并截断 | exit=0：`warning: … truncated to 67108864 bytes (131072 sectors)`，extent 写 131072 扇区（floor，符合 VERIFICATION 结论） | ✅ 通过 |
| 6b | 负例：文件不存在 | `… cli link/info/create nope.img` | **exit=1** | 三个子命令均 exit=1：`error: image file not found: …`（`create` 未创建任何目录/文件） | ✅ 通过 |
| 6c | 负例：非法 adapter | `… cli link disk.img --adapter bogus` | **exit=2** | exit=2：`error: argument -a/--adapter: invalid choice: 'bogus' (choose from ide sata scsi)` | ✅ 通过 |
| 6d | 负例：删除 `version=1` 后启动 | `sed -i '/^version=1$/d' NOVER.vmdk` 后 `vmrun -T ws start NOVER.vmx nogui` | **vmrun start 失败**（证明 `version=1` 必需） | exit=**255**（`Error: The operation was canceled`）；日志：`DISKLIB-DSCPTR: DescriptorOpenInt: Unknown descriptor content.`、`The file specified is not a virtual disk (15)`、`[msg.disklib.INVALIDDISK]`、`Module 'Disk' power on failed.`；`list` = 0 | ✅ 通过 |
| 7 | 全量单测 | `python3 -m unittest discover -s tests -t .` | 全绿 | **`Ran 120 tests in 0.059s — OK`**，exit=0（含 geometry/descriptor/vmx/cli 四个模块，无真实 vmrun 调用） | ✅ 通过 |
| 8 | 清理 | `rm -rf /tmp/opencode/e2e`；`vmrun -T ws list` | 目录删除；**运行中 VM = 0** | 删除 exit=0；**清理时点** `Total running VMs: 0`，exit=0；`~/.vmware` 下 `inventory.vmls`(06:06)/`preferences`(06:17)/`shortcuts`(06:17) mtime 均**早于**本次验收开始时间(09:22)，未被改动（09:27 收尾复查时出现的 `designdemo_vm` 系他方新建，见第 4 节） | ✅ 通过 |

**统计：18 个验收项，17 项完全通过，1 项（2c create 的 register）按契约通过但暴露环境限制（问题 1）。**

---

## 2. 关键产物原文

### 2.1 `link` / `create` 生成的 descriptor（`E2E-VM.vmdk`）

```
# Disk DescriptorFile
version=1
encoding="UTF-8"
CID=fffffffe
parentCID=ffffffff
createType="monolithicFlat"

# Extent description
RW 409600 FLAT "/tmp/opencode/e2e/disk.img" 0

# The Disk Data Base
#DDB

ddb.virtualHWVersion = "4"
ddb.adapterType = "ide"
ddb.geometry.cylinders = "406"
ddb.geometry.heads = "16"
ddb.geometry.sectors = "63"
```

要点：单条 extent、绝对路径、扇区数 = 209715200//512 = 409600（**floor**）、`version=1` 齐备。

### 2.2 `create` 生成的 `.vmx`（`E2E-VM.vmx`）

```
.encoding = "UTF-8"
config.version = "8"
displayName = "E2E-VM"
guestOS = "otherlinux-64"
memsize = "2048"
virtualHW.version = "19"

ide0.present = "TRUE"
ide0:0.present = "TRUE"
ide0:0.fileName = "E2E-VM.vmdk"

floppy0.present = "FALSE"
```

要点：`ide0:0.fileName` 用**相对裸文件名**（与 vmx 同目录），`virtualHW.version=19`，未声明 `usb_xhci`（规避 VERIFICATION §4.1 的上电失败陷阱）。

### 2.3 启动日志关键行（`vmware.log`）

```
DISK: OPEN ide0:0 '/tmp/opencode/e2e/vm/E2E-VM.vmdk' persistent R[]
DISK: OPEN '/tmp/opencode/e2e/vm/E2E-VM.vmdk' Geo (406/16/63) BIOS Geo (0/0/0)
DISKUTIL: ide0:0 : capacity=409600 logical sector size=512
Transitioned vmx/execState/val to poweredOn
```

### 2.4 删除 `version=1` 后的失败日志

```
DISKLIB-DSCPTR: DescriptorOpenInt: Unknown descriptor content.
DISK: Cannot open disk '…/NOVER.vmdk': The file specified is not a virtual disk (15).
[msg.disklib.INVALIDDISK] The file specified is not a virtual disk
[msg.moduletable.powerOnFailed] Module 'Disk' power on failed.
```

---

## 3. 发现的问题

### 问题 1（中等）：`vmrun register` 在 Workstation 17.6.3 上不受支持，VM 不会进入图形界面库

- **现象**：`create` 每次都打印
  `warning: vmrun register failed: Error: register VM not supported for the specified host type`，
  手工执行 `vmrun -T ws register <vmx>`（及 `-T player`）同样返回 exit=255 同样报错；
  `vmrun` 自带帮助文本中**根本没有 `register` 这条命令**（只有 start/stop/clone/list 等）。
- **影响**：VM **不会**出现在 VMware Workstation 图形界面的虚拟机库中；只能按路径启动
  （`vmrun -T ws start <vmx> nogui`，本报告步骤 3 实测 exit=0 可用）。
- **契约符合性**：CONTRACT §vmware.py 明确「register 失败不致命，打印警告仍退出 0」，
  实测 `create` exit=0 —— **符合契约**，因此判定为「按契约通过」。
- **建议**：
  1. README / 帮助文案不要宣称「已注册到 VMware 库」，改为「生成并可通过 vmrun 按路径启动；
     注册尝试失败时会给出提示」；
  2. 可考虑在警告后追加一行更明确的说明（Workstation 的 vmrun 不支持 register，属平台限制，
     需在 GUI 中手动「打开」该 .vmx 或用 `vmware` 命令行）。

### 问题 2（轻微）：任务预期的 `converted-flat.vmdk` 实际不存在

- `-t 1`（growable split）对本 200 MiB 镜像只产出 **单个 `converted-s001.vmdk`**，
  且它是 **SPARSE（KDMV 头）** 格式而非 flat，比源镜像多 **65536 字节**元数据头。
- 因此 md5 对比必须跳过前 65536 字节（`tail -c +65537`）才能比数据层；
  直接 `md5sum converted-s001.vmdk` 会得到不同结果，**不代表数据损坏**。
- 本项已按上述方式验证：**数据层 md5 与源 img 完全一致**。
- 建议：README/验收脚本若要固化该回归，注明「SPARSE 盘需剔除 64 KiB 头后再比对」，
  或改用 `vmware-vdiskmanager -r … -t 0` 产出单文件再另行校验。

### 问题 3（提示，非缺陷）：`create` 输出顺序

- `warning: vmrun register failed…`（stderr）会出现在 descriptor/vmx 行（stdout）**之前**，
  系 stdout 缓冲所致，不影响语义；建议后续统一加 `flush` 或全部走 stderr 分类输出。

### 其它观察

- `create` 对已存在输出文件不带 `-f` → exit=1（`output file already exists`），符合契约。
- 实验中 VM 目录会残留 `nvram / *.vmxf / *.vmsd / *.scoreboard / vmware.log`（VMware 自写），
  `disk.img` 与生成的 `.vmdk` 的大小、mtime、md5 均未被改动。
- 未发现任何与 CONTRACT.md 冲突的 CLI 语义问题（子命令、选项、返回码 0/1/2/3 全部实测吻合）。

---

## 4. 清理记录

| 动作 | 结果 |
|---|---|
| 停止所有实验 VM（`vmrun -T ws stop … hard`） | E2E-VM 与 NOVER 均已停（NOVER 启动即失败，本来未运行） |
| `rm -rf /tmp/opencode/e2e` | exit=0，目录已删除 |
| 清理时点 `vmrun -T ws list`（09:25） | **`Total running VMs: 0`**（exit=0），本任务创建的实验 VM 已全部退出 |
| `~/.vmware` 检查 | `inventory.vmls`(06:06)、`preferences`(06:17)、`shortcuts`(06:17) 等 mtime 均早于验收开始时间(09:22)，**未改动、未删除**；未执行任何 `git init` / 上传操作 |
| 收尾复查（09:27） | `list` 显示 `/tmp/opencode/designdemo_vm/DesignCheck.vmx` —— 该 VM 于 **09:27:11 由其它会话创建并启动**（`vmware.log` 首行时间戳 01:27:11Z，晚于本任务清理完成时刻 09:25），**不属于本验收任务**，按「不得动用户/他人已有虚拟机」要求**未做任何停止或删除**；本任务自身产物目录 `/tmp/opencode/e2e` 确认已不存在 |

---

## 5. 结论

**验收通过（PASS，含 1 项需文档跟进的环境限制）。**

1. `info` / `link` / `create` 三个子命令在真实镜像上全部按契约返回 exit=0，
   产出的 descriptor（绝对路径单 extent、floor 扇区数、`version=1`、DDB 几何 406/16/63、
   `adapterType=ide`）与 vmx（`ide0:0` 相对文件名、`virtualHW.version=19`）完全符合 CONTRACT.md。
2. **真实工具链认可**：`vmrun start` 上电成功并按我们写入的几何打开磁盘
   （`Geo (406/16/63)`、`capacity=409600`）；`vmware-vdiskmanager -e` 判 `Disk chain is consistent.`；
   `-r -t 1` 转换成功且数据层 md5 与源 img **逐字节一致**（零转换链路无数据损坏）。
3. **负例全部命中期望返回码**：非 512 倍数无 `-f` → 1；文件不存在 → 1；非法 adapter → 2；
   删除 `version=1` → `vmrun start` exit=255 + `INVALIDDISK`（证明该字段硬必需）。
4. **单测 120/120 全绿**。
5. **唯一待办**：`vmrun register` 在 Workstation 17.6.3 不可用 → 需要修正文档措辞
   （问题 1），不构成代码缺陷。
6. **清理**：本任务全部实验 VM 已停止，`/tmp/opencode/e2e` 已删除；清理时点
   `vmrun -T ws list` = 0。此后出现的 `designdemo_vm/DesignCheck.vmx` 系其它会话于
   09:27 新建，与本任务无关，未触碰。
