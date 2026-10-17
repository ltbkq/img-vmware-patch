# img-vmware-patch

> [English](README.md) | **简体中文**

为原始磁盘镜像（`.img`、`.bin`、`.raw` …）生成 **VMDK descriptor**，并可选地在它周围
创建一个最小可用的 **VMware Workstation / Player / Fusion** 虚拟机。

不做格式转换、不用向导：镜像文件本身保持原样，通过单条 `FLAT` extent 引用，因此即使
是数 TB 的镜像也是秒级完成。

## 两条路线

| | `link` / `create`（本项目自带工具） | [`native/`](native/README.zh-CN.md) |
| --- | --- | --- |
| 做法 | 为镜像**生成一个 `.vmdk` 描述块文件** | 改造 VMware，让它**直接打开裸 `.img`** |
| 目录里留什么 | 多一个可命名的 `.vmdk` | 隐藏文件 `.fbimg-desc` / `.fbimg-data` |
| 图形界面选盘 | 选 `.vmdk` | 选 `.img`（附界面补丁） |
| 是否改 VMware | 不需要 | 需要 —— 一次性、幂等、可完全还原 |
| 适合 | Windows/macOS 宿主、要把 `.vmdk` 分发出去、零风险场景 | Linux 宿主上**直接开机裸 `.img`** |

两者互补，可同时使用。

* 纯 Python 3.9+，**仅使用标准库**。
* `link` 与 `info` 完全不依赖 VMware，任何机器都能运行。
* `create` 先写 `.vmx`，再在检测到 `vmrun` 时尝试把虚拟机加入 Workstation 库
  （该命令在部分版本上不受支持，失败仅告警，不影响文件生成）。

## 安装

```bash
python3 -m pip install .            # 提供 `img-vmware-patch` 命令
# 或者直接从源码树运行：
PYTHONPATH=src python3 -m img_vmware_patch.cli --help
```

## 用法

```
img-vmware-patch <command> [options]

Commands:
  link      仅生成 VMDK descriptor 文件
  create    生成 descriptor 并创建/注册虚拟机
  info      显示 .img 文件信息和计算出的几何参数

Global: -h/--help  -v/--version
link:   -o/--output PATH   -a/--adapter {ide,sata,scsi}   -f/--force
create: -n/--name  -m/--ram MB(默认 2048)  -g/--guest(默认 otherlinux-64)
        --dir PATH   [-o/--output 同 link]  [-a/--adapter 同 link]  [-f/--force]
```

示例：

```bash
# 查看镜像信息与几何参数
img-vmware-patch info /var/lib/vms/disk.img

# 只生成 descriptor，SCSI/LSI Logic 适配器
img-vmware-patch link disk.img -o disk.vmdk -a scsi

# 在 /srv/vms/MyVM 下生成 descriptor + .vmx，4 GiB 内存，并尝试注册
img-vmware-patch create disk.img -n "MyVM" --dir /srv/vms/MyVM -m 4096 -a scsi

# 覆盖已有 descriptor，并容忍未按 512 字节对齐的镜像
img-vmware-patch link disk.img -o disk.vmdk -f
```

`create` 会在 `--dir` 中写入 `NAME.vmx` 与 `NAME.vmdk`，并让
`<controller>:0.fileName` 只引用 descriptor 文件名（两者必须同目录）；如果用 `-o`
改名，请把输出放在 `--dir` 内。

环境变量：

| 变量 | 作用 |
| --- | --- |
| `IMG_VMWARE_PATCH_REGISTER=0` | `create` 跳过可选的 `vmrun -T ws register` 注册（文件照常生成），适合 CI 机器。 |

## 返回码

| 码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 通用错误：镜像非 512 对齐（未加 `-f`）、镜像太小、输入文件不存在、输出已存在（未加 `-f`）、I/O 错误 |
| `2` | 参数错误（argparse 用法错误、非法虚拟机名称） |
| `3` | VMware 兼容性问题：`create` 找不到 `vmrun`（descriptor 与 `.vmx` 仍会写出，并打印安装提示） |

`vmrun` 注册失败**不致命**：打印警告后 `create` 仍然以 `0` 退出。
注意 Workstation 17.6.3 的 `vmrun` 并未实现 `register` 命令，因此虚拟机通常**不会**
出现在 Workstation 库中——请在图形界面用 **文件 > 打开** 打开 `NAME.vmx`，或直接执行
`vmrun -T ws start NAME.vmx nogui`。

## 几何计算

* `total_sectors = file_size // 512`；非 512 倍数时报错退出码 1
  （`--force` 时按截断到 512 边界处理，并在 stderr 告警）。
* `heads = 16`、`sectors = 63`、`cylinders = total_sectors // (16 * 63)`
  （每柱面 1008 扇区）。
* 余数扇区仍保留在 extent 中，由最后不完整柱面承载；不足一个完整柱面
  （例如 512 B 的文件）即「镜像太小」。

## Descriptor 与 `.vmx`

```
# Disk DescriptorFile
version=1
encoding="UTF-8"
CID=fffffffe
parentCID=ffffffff
createType="monolithicFlat"

# Extent description
RW 131072 FLAT "/abs/path/to/disk.img" 0

# The Disk Data Base
#DDB

ddb.virtualHWVersion = "4"
ddb.adapterType = "lsilogic"      # ide -> ide, sata -> sata, scsi -> lsilogic
ddb.geometry.cylinders = "130"
ddb.geometry.heads = "16"
ddb.geometry.sectors = "63"
```

路径始终是绝对路径且统一使用正斜杠（Windows 下为 `C:/vm/disk.img`），路径中的 `"`
转义为 `\"`。

`.vmx` 为最小配置：`config.version = "8"`、`virtualHW.version = "19"`（WS17）、
`memsize`、`displayName`、`guestOS`、`<controller>:0.*`；SCSI 额外带
`scsi0.virtualDev = "lsilogic"`，并写 `floppy0.present = "FALSE"`。

## 开发

```bash
python3 -m unittest discover -s tests -t .     # 120 个用例，标准库 unittest
```

测试不会启动 `vmrun` 或虚拟机，对 VMware 的探测始终被 mock。

## 许可证

MIT，见 [LICENSE](LICENSE)。
