# 轻机器：系统容器档（方案）

> 目标：把 OrbStack 的「轻快」带到 Linux —— 日常开发机默认用系统容器，
> **1-2 秒启动、~几 MB 内存开销**；需要独立内核时才用现有 QEMU VM 档。
> 本文只做设计，不动代码。

## 1. 为什么现在的 VM 重

| 开销 | 来源 | 系统容器 |
|---|---|---|
| 启动 10-30s | OVMF 固件 + GRUB + 内核 + systemd + cloud-init | 1-2s（直接起 init） |
| 内存 ≥100-200MB | 每台独立内核 + QEMU 进程 | ~1-5MB（共享宿主内核） |
| 磁盘 | qcow2 增量层（1TB 稀疏） | overlayfs 目录 |
| 端口透传 | 需要 hostfwd（SLIRP）或网桥直连 | 网桥直连（天然） |

系统容器**不是 Docker 容器**：它跑完整 init（systemd），有自己的 rootfs
（alpine/rocky/debian 随便换）、自己的网络栈视图、真实的 SSH 服务端。
区别只有一条：**内核与宿主共享**（不能换内核版本、不能加载宿主没有的
内核模块）——日常开发环境用不到这两样。

## 2. 后端选型

| 候选 | 结论 |
|---|---|
| **LXC 直接管理**（推荐） | 仓库有 lxc 7.0.0；`john` 已有 subuid 条目，非特权容器开箱可用。我们自己写管理层（复用现有 spec/SSH/网桥体系），不引入 lxd/incus 的守护与 API |
| Incus（user 模式） | 自带 REST API 与镜像源很省事，但要装 incus 全家桶 + 它自己的 user daemon；与现有 daemon/保活体系两套并存，违反极简原则 |
| systemd-nspawn | 轻但网络/共享目录灵活性差，发行版 rootfs 生态不如 LXC |

决定：**LXC 二进制只做运行时**（`lxc-start`/`lxc-stop`/`lxc-attach`），
创建、配置、状态、网络全部沿用本应用的既有体系。这样 GUI 现有的
账户页、文件页、共享目录、SSH 域名逻辑几乎原样复用。

## 3. 机器模型

`VMSpec` 增加一个字段（json/sqlite 双写自动带上）：

```
kind: "vm" | "container"      # 默认 "vm"，老机器不受影响
```

所有现有代码按「机器」抽象继续工作，只有底层实现分派：

| 能力 | VM 档（现状不动） | 容器档（新增） |
|---|---|---|
| 启动 | qemu.py `build_command` | `lxc-start -n <name>` |
| 状态 | qemu pidfile | `lxc-info -n <name>`（RUNNING/STOPPED） |
| 停止 | ACPI 关机 / soft_stop | `lxc-stop -k`（相当于拔电源）或优雅 stop |
| 真休眠 | migrate-to-file | **不需要**：stop 即保留状态，start 即恢复 |
| 删除 | 删 qcow2 层 | 删 rootfs 目录 |
| SSH | guest 内 sshd（22） | 容器内 sshd（22），同样密钥登录 |
| 文件页 | SFTP（paramiko） | **同一个 SFTP**，零改动 |
| 共享目录 | virtiofs | lxc 配置 `lxc.mount.entry`（bind mount，启动即挂） |
| 内存回收 | virtio-balloon | 不需要：没有独立内核可回收 |
| 保活/唤醒代理 | daemon + wakeproxy | 简化：start 是毫秒级，无需代理端口 |

## 4. 网络：完全复用 pgbr0

现成的 `core/vm/bridge.py`（网桥 + dnsmasq DHCP + lease_ip）原样复用：

```
容器 veth ──> pgbr0 ──> dnsmasq DHCP（10.99.x.x）
                │
宿主机 ─────────┘ 直接访问容器 IP（无任何转发）
```

- 容器网段仍走 `bridge.lease_ip(name)` 查租约，**每台唯一 IP 自动成立**
- 现有 `ssh.py::_ssh_target()` / `sshconfig.py` / `dns.py` **一行不改**
- SLIRP 的每台子网、端口转发逻辑容器档完全不需要
- VM↔容器、容器↔容器互通天然成立（同一二层）

lxc 网络配置：`lxc.net.0.type = veth` + `lxc.net.0.link = pgbr0` +
`lxc.net.0.name = eth0`，DHCP 由现有 dnsmasq 发放。

## 5. 镜像与首次配置

### rootfs 来源

不用 `lxc-create -t download`（模板脚本交互式、依赖外部脚本）。
直接从发行版 rootfs tarball 拉：

- 上游：`https://images.linuxcontainers.org/images/<distro>/<ver>/amd64/default/`
- 国内镜像：清华 tuna 同步了同一目录结构
- 格式选 `rootfs.tar.xz`，解包到 `machines/<name>/rootfs/`

首批目录（对齐现有 ImageSpec 家族）：

| key | 家族 | 说明 |
|---|---|---|
| alpine-3.22-lxc | alpine | 最轻，~60MB 解包 |
| rocky-10-lxc | fedora/rhel 系 | 与现有 rocky VM 对应 |
| debian-13-lxc | debian | |

### 首次配置（替代 cloud-init）

解包 rootfs 后、启动前做一次性「播种」（普通文件操作，不需要 chroot
跑包管理器）：

1. 写 `/etc/hostname`、`/etc/hosts`
2. 注入 sshd 公钥到 `/root/.ssh/authorized_keys` 与用户目录
3. 写默认用户（名字 = 宿主用户名，与 VM 档规则一致）：
   直接写 `/etc/passwd`、`/etc/shadow`（密码哈希用 `openssl passwd -6`）
4. 预置 sshd_config（允许密钥 + 密码）
5. 家族差异沿用 `FAMILY_SHELL` / 服务名（apk/apt/dnf 留给用户进容器后自己用）

lxc 配置文件 `machines/<name>/lxc.conf`（关键行）：

```
lxc.include = /usr/share/lxc/config/common.conf
lxc.idmap = u 0 100000 65536        # 非特权映射（subuid 已存在）
lxc.rootfs.path = dir:<rootfs>
lxc.net.0.type = veth
lxc.net.0.link = pgbr0
lxc.mount.entry = /home/john/项目 mnt/projects none bind,rw,create=dir 0 0
lxc.start.autostart = 0
```

## 6. 账户 / SSH / 域名：零新概念

- `manager.get_password/set_password`：原样（写的是 spec，不关心 kind）
- `ssh.py`：原样（容器里也是 sshd:22，`_ssh_target` 返回容器 IP）
- `sshconfig.py`：原样（`<name>.vm.local` 照写）
- `dns.py`：原样
- 悬停卡片 / 账户页：原样

## 7. UI 变更

| 位置 | 变更 |
|---|---|
| 向导 | 第一步加「机器类型」：**轻机器（推荐）** / 完整虚拟机；镜像列表随类型过滤（`-lxc` 后缀） |
| 列表 | 名称旁加类型徽标（容器「轻」/ VM「VM」）；状态点同现有 |
| 详情-共享目录 | 文案不变；VM 档「应用并重启」走重启，容器档直接 `lxc-stop`+`lxc-start`（秒级，无 loading 长等待） |
| 详情-账户 | 不变 |
| 右键菜单 | 容器档无「休眠」「保存快照」（stop 已等价）；其余同 |
| 设置页-服务 | 增加一项依赖检查：lxc 是否安装（与 podman 未安装同一套引导 UI） |

**不要**给容器档做内存/磁盘页——共享内核没有可调的，保持极简。

## 8. 守护进程（daemon）变更

- `_tick` 的 `resume_paused_vms`、唤醒代理、活动探测降级：容器档
  状态只有 running/stopped，**不需要** paused 救援逻辑
- keepalive 的 balloon 相关对容器档跳过（`spec.kind` 判断）
- 自动回收（autoreclaim）：容器档回收 = `lxc-stop`，唤醒 = `lxc-start`
  （毫秒级，用户无感），**逻辑反而变简单**

## 9. 存储布局

```
~/.local/share/podman-gui/vm/
  machines/
    <name>/
      machine.json        # spec（kind=container）
      lxc.conf
      rootfs/             # 解包的发行版根文件系统
      ssh_key / ssh_key.pub
```

与 VM 档同一目录规范，`manager.load_all()` 不需要两套扫描逻辑。

## 10. 依赖与一次性初始化

| 项 | 方式 |
|---|---|
| lxc 包 | 设置页「服务」tab 检测 `lxc-start --version`，未装给安装引导（与 podman 同款） |
| 网桥 | 已有 bridge.py，无新增 |
| subuid | CachyOS 上 `john` 已有条目；缺失时并入 bridge 的一次性 pkexec 脚本 |
| lxcfs | 可选（增强 free/uptime 显示），不装不影响功能 |

## 11. 实施步骤（每步带测试）

1. **spec 扩展**：`kind` 字段 + `is_container()` + 载入兼容测试（老 json 无 kind → vm）
2. **后端模块** `core/vm/container.py`：create/rootfs 播种/start/stop/status/delete/conf 生成；用 `/tmp` 假 rootfs + lxc dry-run 测配置生成（不打真容器）
3. **镜像目录**：`-lxc` 三个条目 + 下载解包（现有 images.py 的下载管线复用）
4. **manager 分派**：start_vm/stop_vm/delete 按 kind 路由；SSH 测试连真容器
5. **共享目录**：lxc.mount.entry 生成 + 重启生效
6. **daemon 简化**：容器档跳过 paused/wakeproxy/balloon
7. **UI**：向导类型选择 → 列表徽标 → 详情差异 → 设置页依赖检查
8. **端到端**：建 alpine 容器 → SSH → 共享目录 → 文件页 → 删除

## 12. 现有代码处置表（供清理时参考）

| 模块 | 容器档是否用 |
|---|---|
| qemu.py（build_command/snapshot/slirp） | 只 VM 档 |
| cloudinit.py | 只 VM 档 |
| virtiofs.py / shared_disk.py | 只 VM 档 |
| keepalive.py / memory.py / budget.py / autoreclaim.py | VM 档；容器档分支跳过 |
| wakeproxy.py / activity.py | VM 档；容器档不需要 |
| ssh.py / files.py / sshconfig.py / dns.py / bridge.py / network.py | **两者共用，不要动** |
| ui/vm_wizard.py / vm_page.py / vm_detail.py | 加 kind 分支 |
| ui/charts.py / settings_page.py | 设置页加 lxc 检测 |
