# 改名 PodBox —— 清单与节奏

> 决定：新名字 **PodBox**，范围「全部」（含 GitHub 仓库名）。
> 节奏（用户定的）：**不现在大爆炸改名**——写系统容器新代码时顺手改，
> 收尾时统一检查一遍、确认无遗漏后再改仓库名。

## 命名映射

| 旧 | 新 |
|---|---|
| podman-gui | podbox |
| podman_gui | podbox（Python 模块内字符串） |
| Podman GUI（显示名） | PodBox |
| podman-gui-vmd.service | podbox-vmd.service |
| podman-gui-dnsmasq.service | podbox-dnsmasq.service |
| podman-gui-net.service | podbox-net.service（若 bridge.py 内仍叫这个） |
| podman-gui-vm.conf / 标记 "podman-gui-vm" | podbox-vm.conf / "podbox-vm" |
| ~/.local/share/podman-gui | ~/.local/share/podbox |
| ~/.config/podman-gui | ~/.config/podbox |
| GitHub: demon471/podman-gui | demon471/podbox（最后一步） |
| pgbr0 网桥 | **不改**（改了要 teardown/重建网桥，收益为零；"pg"可解释为 pod-gui/PG 的历史名，或后续单独决定） |

## 代码内命中位置（27 个文件）

core/：config.py, notify.py, state.py, podman_config.py(仅显示名),
vm/{cloudinit,dns,images,manager,network,sshconfig,files,daemon,sudo,bridge}.py
ui/：main_window.py, icons.py
根：main.py, podman-gui.spec, build.sh, run_tests.sh, install.sh, README.md,
resources/podman-gui.desktop, .github/workflows/build.yml
tests/：t_conflict.py, t_loading2.py, t_serial_console.py
docs/：vm-research.md, light-machines.md

## 系统/数据侧（收尾时一次性迁移）

1. `~/.local/share/podman-gui`（state.db + vm/machines）→ `~/.local/share/podbox`
   - **迁移逻辑要进代码**：启动时检测旧目录存在且新目录不存在 → mv，
     老用户升级无痛
2. `~/.config/systemd/user/podman-gui-vmd.service` → 新 unit 名
   （daemon 侧 disable 旧 enable 新）
3. `/etc/systemd/system/podman-gui-dnsmasq.service` +
   `/etc/dnsmasq.d/podman-gui-vm.conf`（bridge/dns 模块的 pkexec 脚本改）
   - dns.py 的 `_conf_matches` 按标记匹配，改名字段需兼容旧文件
     （检测旧标记也算已配置，或提供迁移）
4. `resources/podman-gui.desktop` → podbox.desktop + Exec/Icon 改
5. PyInstaller spec 与 build.sh 产物名 → podbox
6. 安装的 `~/.local/bin/podman-gui` → podbox

## 时机安排

- **阶段 1（写容器档代码时）**：新建文件一律用 podbox 命名；触碰到的
  旧文件顺手改其中的名字字符串
- **阶段 2（功能完成后收尾）**：全局 grep 零命中检查 → 数据目录迁移
  逻辑 → unit/桌面文件/构建产物 → README → 本地安装替换
- **阶段 3（用户确认后）**：GitHub 仓库 Settings 里 rename
  （GitHub 会自动重定向旧 URL），更新 remote
