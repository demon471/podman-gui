# 轻机器：纯系统容器架构（方案 v2）

> **产品决定：只有轻机器（LXC 系统容器），不再有 QEMU 虚拟机档。**
> 向导直接创建容器，不提供类型选择。QEMU 相关代码彻底删除。
>
> **硬约束：UI 与功能逐一对齐原 VM 版**——向导、列表、详情页
> （文件 / 共享目录 / 账户）、悬停卡片、托盘、设置页的布局与交互
> 全部保持不变，只把底层从 QEMU 换成 LXC。
>
> 分支策略：基于 `feature/system-containers`（继承全部 UI 精修），
> 功能完成后反向合并 master。

## 1. 为什么容器够用

| | QEMU VM（删除） | 系统容器（唯一形态） |
|---|---|---|
| 启动 | 10-30s | 1-2s |
| 内存 | 100-200MB | ~1-5MB（共享宿主内核） |
| 磁盘 | qcow2 层 | overlayfs/rootfs 目录 |
| 端口透传 | hostfwd / 网桥直连 | 网桥直连（天然） |
| 真休眠 | migrate-to-file | stop 即保留状态，start 毫秒级恢复 |

系统容器跑完整 init（systemd/openrc）、有独立 rootfs（发行版随便换）、
真实 sshd。唯一限制：内核与宿主共享（不能换内核版本、不能加载宿主
没有的内核模块）——开发环境用不到。

## 2. 后端

**LXC 二进制直管**（`lxc-start`/`lxc-stop`/`lxc-info`/`lxc-attach`），
不装 incus/lxd。创建、配置、状态、网络沿用本应用既有体系。
前提已核实：CachyOS 仓库有 lxc 7.0.0；宿主用户已有 subuid 条目
（非特权容器开箱可用）。

## 3. 镜像与首次配置

- rootfs tarball：`images.linuxcontainers.org`（清华 tuna 有同步），
  解包到 `machines/<name>/rootfs/`
- 首批：alpine-3.22-lxc / rocky-10-lxc / debian-13-lxc
- 启动前播种（纯文件操作，无 chroot）：
  1. `/etc/hostname`、`/etc/hosts`
  2. sshd 公钥注入 root 与默认用户
  3. 默认用户（名字 = 宿主用户名）写 passwd/shadow（`openssl passwd -6`）
  4. sshd_config：密钥 + 密码登录
- 家族差异沿用 FAMILY_SHELL / 包管理器认知（apk/apt/dnf）

`lxc.conf` 关键行：

```
lxc.include = /usr/share/lxc/config/common.conf
lxc.idmap = u 0 100000 65536
lxc.rootfs.path = dir:<rootfs>
lxc.net.0.type = veth
lxc.net.0.link = pgbr0
lxc.mount.entry = <宿主目录> <挂载点> none bind,rw,create=dir 0 0
```

## 4. 网络（复用，不改）

pgbr0 + dnsmasq DHCP + lease_ip 原样：
每台唯一 IP（10.99.x.x）、`ssh <name>.vm.local`、宿主机直连内部 IP、
容器互通——全部由现有 bridge/dns/ssh/sshconfig/network 模块承担，
**一行不改**。

## 5. UI 对齐表（硬约束的落地）

| 界面 | VM 版行为 | 容器版行为（必须一致） |
|---|---|---|
| 向导 | 选发行版 → 内存/磁盘 → 创建 | 选发行版（-lxc 列表）→ 创建；**无类型选择、无内存/磁盘页**（容器不可调） |
| 列表 | 名称/状态点/悬停卡片 | 同；悬停卡片显示镜像/用户/密码/IP/SSH 命令 |
| 详情 | 文件 / 共享目录 / 账户 三 tab | 同三 tab；文件页走 SFTP（同一份代码） |
| 共享目录 | 选目录→virtiofs→应用并重启 | 选目录→bind mount→应用并重启（秒级完成） |
| 账户 | 改密码/看连接信息两列 | 同 |
| 右键菜单 | 重启/休眠/快照/删除 | 重启/删除；无休眠与快照（stop 已等价休眠） |
| 托盘 | 列出机器/显示主窗口/退出 | 同（机器=容器） |
| 设置页 | 服务/资源/镜像/存储/关于 | 服务页加 lxc 依赖检测；资源页删除（容器无可调资源） |

## 6. 删除清单（QEMU 链路全删）

**整文件删除**：
core/vm/{qemu,cloudinit,virtiofs,shared_disk,wakeproxy,keepalive,
memory,budget,autoreclaim,activity,autoconfig}.py
（deps.py 保留——供 lxc 检测复用）

**大幅简化**：
- manager.py：去掉 qemu/快照/端口分配/balloon；生命周期路由到 container.py
- daemon.py：只保留基础 tick + 状态同步；paused 救援/唤醒代理/内存回收全删
- ui/settings_page.py：资源页删除、VM 内存控件删除、加 lxc 检测
- ui/vm_detail.py：保留三 tab 结构，去掉休眠/快照入口
- ui/vm_page.py / vm_infocard.py：状态机简化（running/stopped）

**保留不动**（两类机器共用，已验证）：
core/vm/{bridge,dns,ssh,sshconfig,files,network,sudo,hostinfo}.py

## 7. 守护进程（daemon）

- 保留：启动自检、状态上报、托盘/通知联动
- 删除：resume_paused_vms、唤醒代理、balloon/内存预算、活动探测降级
- 容器档「自动回收」若保留 = lxc-stop + 事件唤醒 lxc-start（可选，二期）

## 8. 存储布局

```
~/.local/share/podbox/            # 改名随 rename.md 一起
  machines/<name>/
    machine.json                  # spec
    lxc.conf
    rootfs/
    ssh_key / ssh_key.pub
  images/                         # rootfs tarball 缓存
```

老 VM 机器目录（qcow2 等）不再兼容：首次启动检测到旧目录时提示一次性清理。

## 9. 实施步骤（每步带测试）

1. **core/vm/container.py**：conf 生成/播种/start/stop/status/delete
   （假 rootfs + 配置生成单测，lxc dry-run）
2. **images.py 换目录**：rootfs tarball 三条目 + 下载解包管线
3. **manager.py 重写底层**：路由 container.py；删 qemu 引用
4. **删除清单执行**：整删 VM-only 模块 + 测试同步删除
5. **daemon 简化**
6. **UI 对齐**：向导 → 列表/卡片 → 详情三 tab → 设置页
7. **端到端**：建 alpine 容器 → SSH → 共享目录 → 文件页 → 密码 → 删除
8. **PodBox 改名收尾**（见 rename.md 阶段 2）

## 10. 测试要求

- 每步新增行为都有对应测试；删除模块时其测试同删
- `run_tests.sh` 全绿才允许构建（build.sh 已强制）
- 端到端至少覆盖：创建/SSH/共享目录/文件页/改密码/删除
