# PodBox 依赖清单（中间件）

> 用途：测试 app 初始化安装流程、新机器部署、打包体积归因。
> 分两层：**内置**（PyInstaller 打进单文件，无需安装）与
> **外置**（宿主机系统组件，app 负责检测并引导安装）。

## A. 打包内置（无需安装，占 79M 单文件体积）

| 组件 | 用途 | 备注 |
|---|---|---|
| Python 3.14 运行时 | 解释器 | libpython 2.6MB |
| PyQt6（Core/Gui/Widgets/Svg） | KDE 原生界面 | ~11MB，含 libicudata 12MB 本地化数据 |
| paramiko + cryptography + bcrypt + nacl | SFTP 文件浏览/传输 | 走 SSH 通道浏览容器文件 |
| pexpect + ptyprocess + pyte + wcwidth | 内置终端 | |
| sqlite3 | 状态存储（state.db） | |
| Podman REST 客户端 | 自研（core/podman_client.py） | 走 podman.socket，无额外依赖 |

已剔除（体积优化，见 podman-gui.spec 的 `_BIN_EXCLUDE`）：
视频编解码器（x265/aom/SVT-AV1/x264/dav1d/rav1e/heif/jxl 16MB）、
GTK 链（9MB）、KF6 图标库（5.5MB）、QtQuick/Qml/Pdf（8.7MB）。

## B. 系统外置（app 检测 + 引导安装）

### 必需

| 组件 | 用途 | 何时检测 | arch | debian | fedora |
|---|---|---|---|---|---|
| podman (+podman.socket) | 容器引擎，应用核心 | 应用启动 | podman | podman | podman |
| lxc | 轻机器运行时 | 创建/启动前 | lxc | lxc | lxc |
| dnsmasq | ① `*.vm.local` 域名解析（127.0.0.1:5354）② 网桥 DHCP/DNS（pgbr0） | 初始化时 | dnsmasq | dnsmasq | dnsmasq |
| openssh（ssh/scp/ssh-keygen） | SSH 连接、密钥、文件传输 | 创建/连接前 | openssh | openssh-client | openssh-clients |
| polkit（pkexec） | 一次性系统初始化提权 | 初始化时 | polkit | polkit | polkit |
| acl（setfacl/getfacl） | 家目录 ACL（容器 root 穿越授权） | 初始化脚本 | acl | acl | acl |
| iptables | 网桥 NAT（容器出外网） | 初始化脚本 | iptables | iptables | iptables |
| systemd-resolved | `*.vm.local` 路由域转发 | 初始化时配置 | 系统自带 | 系统自带 | 系统自带 |

### 系统自带（不用装）

procps（sysctl）、iproute2（ip/bridge）、coreutils（getent）。
 Arch/CachyOS/Debian/Fedora 桌面版全部预装。

### 已不再需要（纯容器化清理，2026-11）

| 组件 | 原用途 | 清理点 |
|---|---|---|
| qemu-system-x86 / qemu-img | VM 档运行/磁盘 | 模块已删；bridge.py 白名单段已删 |
| xorriso / genisoimage | cloud-init seed ISO | cloudinit.py 已删（rootfs 播种替代） |
| /dev/kvm | 硬件加速 | 无 VM 即无需求 |
| qemu-bridge-helper | VM 挂网桥 | 容器走 lxc-user-nic（/etc/lxc/lxc-usernet） |

## C. 代码对应关系

| 检查点 | 位置 |
|---|---|
| Python 端依赖清单（包名/检测命令/用途） | `core/vm/deps.py::DEPENDENCIES` |
| 一次性系统初始化（网桥+DHCP+DNS+ACL+usernet） | `core/vm/bridge.py::_setup_script`（pkexec 幂等脚本） |
| 容器运行前置（lxc/subuid/ACL/usernet） | `core/vm/container.py::prerequisites` |
| 域名解析状态 | `core/vm/dns.py::status` |
| podman 服务状态与安装引导 | `core/podman_config.py` + 设置页「服务」tab |

## D. 一键初始化做了什么（pkexec 脚本，全部幂等）

1. 建 `pgbr0` 网桥（10.99.0.1/16）
2. IP 转发 + NAT（MASQUERADE）
3. dnsmasq 独立 unit（`podman-gui-net.service`）：网桥 DHCP + DNS 转发宿主 resolv
4. `/etc/lxc/lxc-usernet`：给发起用户 veth 配额
5. 家目录 ACL：`setfacl -m u:<subuid>:x /home/<user>`

域名解析另一套（`core/vm/dns.py`）：dnsmasq 5354 端口通配
`*.vm.local` → 127.0.0.1 + resolved 路由域，独立于上面的 unit。

## E. 测试环境的最小安装集

```bash
# Arch/CachyOS
sudo pacman -S --needed podman lxc dnsmasq openssh polkit acl iptables
# Debian/Ubuntu
sudo apt install -y podman lxc dnsmasq openssh-client polkitd acl iptables
# Fedora
sudo dnf install -y podman lxc dnsmasq openssh-clients polkit acl iptables
```

装完在 app 里点一次「设置 → 服务 → 启用共享网络」即可（pkexec 脚本
自动配齐 lxc-usernet / ACL / 网桥 / DHCP）。
