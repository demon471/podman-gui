# 虚拟机功能调研与设计

> 参考 OrbStack 的 Linux machines，在 Linux 宿主机上实现等价能力。

## 1. OrbStack 架构拆解

### 它做了什么

```
macOS (无 Linux 内核)
  └─ 自研 VMM（基于 Hypervisor.framework）
      └─ 轻量 Linux VM（共享内核）
          ├─ Docker engine
          └─ Linux machines（Ubuntu/Alpine/...）
```

### 关键能力与实现方式

| 能力 | OrbStack 实现 | 我们的等价方案 |
|---|---|---|
| **快速启动（2s）** | 共享内核 + 快照恢复 | KVM + cloud image + 预置 cloud-init |
| **端口透传** | 自研用户态网络栈 | QEMU `-netdev user,hostfwd=` |
| **域名 `x.orb.local`** | 自研 DNS 服务器 | `/etc/hosts` 写入 + 端口约定 |
| **文件双向访问** | 自研 virtio-fs 实现 | `virtiofsd` 或 9p 共享 |
| **SSH 进入** | 32222 端口转发 + key 注入 | `hostfwd=tcp::2222-:22` + cloud-init ssh key |
| **剪贴板/复制** | 自研 mDNS + 文件通道 | SSH + `cat`/`scp` 或共享目录 |
| **host networking** | 自定义网络命名空间 | QEMU `-netdev user` 已含 localhost 互通 |

### 我们做不到的（本质差异）

1. **共享内核**：OrbStack 用 WSL2 式的共享内核，我们只能用完整内核启动
   - 后果：内存占用更高（最小 ~128MB vs OrbStack 的 ~20MB）
   - 启动更慢（~5-10s vs 2s）

2. **自研网络栈**：OrbStack 的网络是用户态实现的，可以任意玩花样
   - 我们只能用 QEMU 的 SLIRP（用户态 NAT）或 TAP 桥接
   - **你的机器只有 wlan0 无线网卡 → 桥接不可用**，必须用 SLIRP

3. **`.orb.local` 通配 DNS**：需要自建 DNS 服务器
   - 我们方案：`/etc/hosts` 写入具体域名（需 sudo）
   - 限制：新增 VM 要重新写 hosts

## 2. 我们的架构设计

```
┌─────────────────────────────────────────────────────┐
│  PyQt6 GUI                                           │
│  ┌───────────────────────────────────────────────┐  │
│  │  虚拟机页面（VM 列表 / 创建向导 / 详情）        │  │
│  └───────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────┤
│  core/vm/                                            │
│  ├── manager.py       VM 生命周期（创建/启动/停止）  │
│  ├── qemu.py          QEMU 命令行构建、进程管理       │
│  ├── images.py        发行版 catalog、镜像下载/校验   │
│  ├── cloudinit.py     cloud-init seed ISO 生成        │
│  ├── network.py       端口转发、hosts 域名、DNS       │
│  ├── ssh.py           SSH 连接、密钥注入、命令执行    │
│  └── storage.py       qcow2 磁盘、快照、共享目录      │
├─────────────────────────────────────────────────────┤
│  外部依赖                                             │
│  ├── qemu-system-x86_64 + qemu-img                   │
│  ├── cloud-init（VM 内）                              │
│  └── ssh / scp（宿主机，用于文件传输）                │
└─────────────────────────────────────────────────────┘
```

## 3. 数据布局

```
~/.local/share/podman-gui/vms/
├── catalog.json                # 已创建的 VM 清单
├── ubuntu-dev/
│   ├── vm.json                 # VM 配置
│   ├── disk.qcow2              # 系统盘（qcow2，支持快照）
│   ├── seed.iso                # cloud-init 数据盘
│   ├── ssh_key                 # 专属私钥（Ed25519）
│   ├── ssh_key.pub
│   ├── console.log             # 串口输出（排错用）
│   └── snapshots/              # 快照目录
└── images/                     # 下载的 base image 缓存
    ├── ubuntu-24.04-cloudimg-amd64.img
    └── alpine-3.20-cloudimg-amd64.img
```

## 4. 关键实现细节

### 4.1 发行版 catalog

```python
CATALOG = {
    "ubuntu-24.04": {
        "name": "Ubuntu 24.04 LTS",
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "size": "~600MB",
        "user": "ubuntu",
        "default_mem": 2048,
        "default_disk": 20,
        "init": "systemd",
    },
    "debian-12": {...},
    "fedora-41": {...},
    "alpine-3.20": {...},
    "arch": {...},
}
```

### 4.2 cloud-init 配置（自动注入 SSH key + 端口）

```yaml
#user-data
#cloud-config
users:
  - name: dev
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - ssh-ed25519 AAAA... podman-gui-vm
ssh_pwauth: false
package_update: false
runcmd:
  - systemctl enable --now sshd
```

### 4.3 QEMU 命令行

```bash
qemu-system-x86_64 \
  -enable-kvm -cpu host -smp 2 -m 2048 \
  -drive file=disk.qcow2,if=virtio,format=qcow2 \
  -drive file=seed.iso,if=virtio,format=raw,readonly=on \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22,hostfwd=tcp:127.0.0.1:8080-:80 \
  -device virtio-net-pci,netdev=net0 \
  -nographic -serial file:console.log \
  -pidfile qemu.pid -daemonize
```

**为什么用 `-nographic -serial file:`**：
- 无 GUI 依赖（CI/SSH 环境可用）
- 串口日志落盘，排错可查
- 图形化桌面需求可通过 VNC 追加（`-vnc :1`）

### 4.4 端口透传

QEMU SLIRP 的 `hostfwd` 天然实现双向：
- 宿主机 `127.0.0.1:8080` → VM `:80`
- VM 内访问宿主机用 `10.0.2.2`

**端口冲突处理**：创建 VM 时扫描可用端口，或让用户指定。

### 4.5 域名方案

OrbStack 的 `machine.orb.local` 我们这样近似：

```
/etc/hosts（需 sudo 一次）:
  127.0.0.1  ubuntu-dev.local
  127.0.0.1  alpine.local
```

配合端口约定（80 → 8080 等），访问 `http://ubuntu-dev.local:8080`。

**更好的方案**（后续可选）：内置 dnsmasq 监听 `127.0.0.1:5353`，但需要改 resolv.conf，侵入性强。

### 4.6 SSH 与文件传输

```python
# 生成专属密钥对（Ed25519）
ssh-keygen -t ed25519 -f ssh_key -N ""

# 连接（免密，因 key 已注入）
ssh -i ssh_key -p 2222 dev@127.0.0.1

# 文件传输
scp -i ssh_key -P 2222 ./local.txt dev@127.0.0.1:/tmp/
```

**剪贴板/复制**：复用现有 SSH 通道
- 从 VM 复制：`ssh ... "cat /path/to/file"` → 写入宿主机剪贴板
- 复制到 VM：`ssh ... "cat > /path/to/file"` ← 读剪贴板

### 4.7 共享目录

QEMU 有两条路：

| 方案 | 优点 | 缺点 |
|---|---|---|
| **9p (virtio-9p)** | 配置简单，QEMU 内置 | 性能较差 |
| **virtiofs** | 性能接近原生 | 需要 virtiofsd 守护进程 |

第一版用 **9p**（零额外依赖）：

```bash
-virtfs local,path=/home/user/share,mount_tag=hostshare,security_model=mapped-xattr
```

VM 内挂载：
```bash
mount -t 9p -o trans=virtio hostshare /mnt/host
```

## 5. 依赖清单

### 必须安装

```bash
# Arch / CachyOS
sudo pacman -S qemu-base qemu-img

# Ubuntu / Debian
sudo apt install qemu-system-x86 qemu-utils

# Fedora
sudo dnf install qemu-kvm qemu-img
```

### 可选

| 包 | 用途 |
|---|---|
| `cloud-utils`（`cloud-localds`） | 生成 cloud-init seed ISO（也可用 Python 手写 ISO9660） |
| `libvirt` | 若改用 libvirt 后端 |
| `edk2-ovmf` | UEFI 启动（默认用 BIOS 即可） |
| `virtiofsd` | 高性能共享目录 |

**我们的选择**：不依赖 `cloud-localds`，用 Python 的 `pycdlib` 或直接调用 `genisoimage`/`xorriso`。第一版用 `xorriso`（多数发行版都有）。

## 6. 实施步骤

- [ ] 1. 依赖检测与安装引导（GUI 内检测 QEMU，缺则引导安装）
- [ ] 2. `core/vm/images.py`：catalog + 下载（带进度）
- [ ] 3. `core/vm/cloudinit.py`：seed ISO 生成
- [ ] 4. `core/vm/qemu.py`：命令行构建 + 进程管理
- [ ] 5. `core/vm/manager.py`：VM 生命周期 + 状态持久化
- [ ] 6. `core/vm/ssh.py`：SSH 连接 + 文件传输
- [ ] 7. `core/vm/network.py`：端口管理 + hosts 写入
- [ ] 8. `ui/vm_page.py`：VM 列表页
- [ ] 9. `ui/vm_wizard.py`：创建向导
- [ ] 10. `ui/vm_detail.py`：详情（端口/SSH/共享目录）
- [ ] 11. 集成到主窗口侧边栏
- [ ] 12. 端到端测试

## 7. 已知限制（诚实说明）

| 限制 | 原因 | 影响 |
|---|---|---|
| **无法桥接网络** | 你只有 wlan0，无线网卡不支持桥接 | VM 只能用 NAT，局域网内其他设备访问不到 VM |
| **启动比 OrbStack 慢** | 完整内核启动 vs 共享内核 | 约 5-10 秒 |
| **内存占用更高** | 每 VM 独立内核 | 最小 128MB，推荐 2GB |
| **域名需改 /etc/hosts** | 无自研 DNS | 每次新增 VM 要 sudo |
| **9p 性能一般** | 未用 virtiofs | 大文件传输较慢 |

## 8. 与 Podman 的关系

**两者独立**：
- Podman 容器跑在宿主机内核
- VM 跑独立内核
- 可以在 VM 内再装 Podman/Docker（嵌套容器）

**互通的点**：
- VM 的端口转发和容器的端口映射都走宿主机 127.0.0.1
- 共享目录可以让 VM 访问宿主机的 `/data`

## 参考

- [OrbStack Architecture](https://docs.orbstack.dev/architecture)
- [OrbStack Linux machines](https://docs.orbstack.dev/machines/)
- [OrbStack Linux networking](https://docs.orbstack.dev/machines/network)
- [cloud-init QEMU tutorial](https://docs.cloud-init.io/en/latest/tutorial/qemu.html)
- [Ubuntu: Launch QCOW images using QEMU](https://ubuntu.com/docs/public-images/public-images-how-to/launch-qcow-with-qemu/)
