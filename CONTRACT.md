# 实现契约（各流水工位共用，勿擅自改动 CLI 语义）

项目名：`img-vmware-patch`
语言：Python 3.9+，**仅标准库**（argparse / pathlib / os / platform / json）
许可证：MIT
包布局：

```
img-vmware-patch/
├── LICENSE
├── README.md              # 英文为主，附 zh-CN 版 README.zh-CN.md
├── CHANGELOG.md
├── .gitignore
├── pyproject.toml         # console_scripts: img-vmware-patch
├── src/img_vmware_patch/
│   ├── __init__.py        # __version__ = "0.1.0"
│   ├── cli.py             # argparse 入口
│   ├── descriptor.py      # 扇区/CHS 计算 + VMDK descriptor 生成
│   ├── vmx.py             # .vmx 最小配置生成
│   ├── geometry.py        # 纯计算：bytes -> Geometry
│   └── vmware.py          # 检测 vmrun/vmware-vdiskmanager 路径与版本
└── tests/
    ├── test_geometry.py
    ├── test_descriptor.py
    ├── test_vmx.py
    └── test_cli.py
```

## CLI（必须逐字一致）

```
img-vmware-patch <command> [options]
Commands:
  link      仅生成 VMDK descriptor 文件
  create    生成 descriptor 并创建/注册虚拟机
  info      显示 .img 文件信息和计算出的几何参数
Global: -h/--help  -v/--version
link:   -o/--output PATH   -a/--adapter {ide,sata,scsi}   -f/--force
create: -n/--name  -m/--ram MB(默认2048)  -g/--guest(默认 otherlinux-64)
        --dir PATH   [-o/--output 同 link]  [-a/--adapter 同 link]  [-f/--force]
```

返回码：`0` 成功；`1` 通用错误；`2` 参数错误；`3` VMware 兼容性问题。

## 几何计算（geometry.py，纯函数）

- `total_sectors = file_size // 512`；`file_size % 512 != 0` → stderr 告警 + 退出码 1
  （除非 `--force`，此时按截断到 512 边界处理并告警）。
- `heads=16`, `sectors=63`, `cylinders = total_sectors // (16*63)`，余数仍由最后不完整柱面承载
  （cylinders 至少为 1，否则报错「镜像太小」）。

## descriptor（descriptor.py）

**单条 extent 行**（关键修正，勿照抄设计稿里那两行示例）：
```
RW <total_sectors> FLAT "<abs-path-to-img>" 0
```
- 绝对路径；Windows 输出为 `C:/path/to/img.img`（**始终用正斜杠**），Linux 为 `/path/img.img`。
- 路径中的 `"` 需转义为 `\"`；空格无需引号外转义（整条路径已用双引号包裹）。
- `createType="monolithicFlat"`。
- ddb 段：
  - `ddb.adapterType`: ide→`ide`，sata→`sata`，scsi→`lsilogic`
  - `ddb.geometry.cylinders/heads/sectors`
  - `ddb.virtualHWVersion = "4"`

## vmx（vmx.py）

- `ide` → `ide0:0.*`；`sata` → `sata0:0.*`；`scsi` → `scsi0:0.*` + `scsi0.virtualDev = "lsilogic"`。
- `ide0:0.fileName = "<descriptor 文件名>"`（相对名，descriptor 与 vmx 同目录）。
- `virtualHW.version = "19"`（WS17）；`memsize`、`displayName`、`guestOS` 由参数给。
- `create` 额外写 `ide0:0.present = "TRUE"` 等，`floppy0.present = "FALSE"`。

## vmware.py

- Linux 探测顺序：`/usr/bin/vmrun`、`which vmrun`；Windows：`C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe` 等。
- 找不到 → `create` 报退出码 3 并打印安装提示；`link`/`info` 不依赖 VMware，**必须能在没装 VMware 的机器上跑**（单测即如此）。
- `create` 完成后可选调用 `vmrun -T ws register <vmx>`；失败不致命，打印警告仍退出 0。

## 测试要求

- `python3 -m pytest tests/` 全绿；无 pytest 依赖时可用 `unittest`。
- 覆盖：几何边界（512B、1MB+1B、非 512 倍数）、路径规范化（含空格/中文/Windows 盘符）、
  --force、输出文件已存在、CLI 三个子命令的返回码。
- 不得在单测里调用真实 vmrun。
