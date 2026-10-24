#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  Podman 管理器 — 安装脚本
#
#  用法：
#    ./install.sh                     # 安装到 ~/.local（推荐，无需 root）
#    ./install.sh --system            # 安装到 /usr/local（需 sudo）
#    ./install.sh --uninstall         # 卸载
#
#  需要同目录下存在：
#    podman-gui-v*-linux-x86_64      可执行文件
#    podman-gui.desktop              桌面入口
#    appicon/                        图标目录
# ══════════════════════════════════════════════════════════════

set -euo pipefail

APP_NAME="podman-gui"
APP_LABEL="Podman 管理器"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 解析参数 ──
MODE="user"
ACTION="install"
for arg in "$@"; do
    case "$arg" in
        --system)    MODE="system" ;;
        --user)      MODE="user" ;;
        --uninstall) ACTION="uninstall" ;;
        -h|--help)
            sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^#  \?//'
            exit 0
            ;;
        *)
            echo "未知参数: $arg（用 --help 查看用法）" >&2
            exit 1
            ;;
    esac
done

# ── 目标路径 ──
if [ "$MODE" = "system" ]; then
    BIN_DIR="/usr/local/bin"
    DESKTOP_DIR="/usr/share/applications"
    ICON_BASE="/usr/share/icons"
    NEED_SUDO=1
else
    BIN_DIR="$HOME/.local/bin"
    DESKTOP_DIR="$HOME/.local/share/applications"
    ICON_BASE="$HOME/.local/share/icons"
    NEED_SUDO=0
fi

run_root() {
    if [ "$NEED_SUDO" = "1" ]; then
        sudo "$@"
    else
        "$@"
    fi
}

# ══════════════════════════════════════════════════════════════
#  卸载
# ══════════════════════════════════════════════════════════════
if [ "$ACTION" = "uninstall" ]; then
    echo "==> 卸载 $APP_LABEL（$MODE）"

    run_root rm -f "$BIN_DIR/$APP_NAME"
    run_root rm -f "$DESKTOP_DIR/$APP_NAME.desktop"

    # 删除所有主题下的图标
    for theme_dir in "$ICON_BASE"/*; do
        [ -d "$theme_dir/apps" ] || continue
        find "$theme_dir/apps" -name "$APP_NAME.png" -delete 2>/dev/null || true
    done

    # 刷新缓存
    command -v gtk-update-icon-cache >/dev/null && \
        gtk-update-icon-cache -f -t "$ICON_BASE/hicolor" 2>/dev/null || true
    command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 --noincremental 2>/dev/null || true
    command -v update-desktop-database >/dev/null && \
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

    echo "==> 已卸载"
    echo "    提示：配置和容器数据未删除"
    echo "    如需清理配置：rm -rf ~/.config/podman-gui ~/.cache/podman-gui"
    exit 0
fi

# ══════════════════════════════════════════════════════════════
#  安装
# ══════════════════════════════════════════════════════════════
echo "==> 安装 $APP_LABEL（$MODE 模式）"
echo ""

# ── 1. 定位可执行文件 ──
BINARY=""
for f in "$SCRIPT_DIR"/podman-gui-v*-linux-x86_64 "$SCRIPT_DIR"/podman-gui; do
    if [ -f "$f" ] && [ -x "$f" ]; then
        BINARY="$f"
        break
    fi
done

if [ -z "$BINARY" ]; then
    # 可能是脚本内嵌的自解压包，稍后由 tail 解出
    if [ -f "$SCRIPT_DIR/podman-gui.desktop" ]; then
        echo "!! 未找到可执行文件" >&2
        exit 1
    fi
fi

# ── 2. 检查依赖 ──
echo "[1/5] 检查系统依赖"
# 先取一次完整列表（避免 pipefail 下 grep -q 提前退出导致 SIGPIPE）
LDCACHE="$(ldconfig -p 2>/dev/null || true)"
MISSING=""
for lib in libQt6Core.so.6 libQt6Gui.so.6 libQt6Widgets.so.6; do
    case "$LDCACHE" in
        *"$lib"*) ;;                      # 已找到
        *) MISSING="$MISSING $lib" ;;     # 缺失
    esac
done
if [ -n "$MISSING" ]; then
    echo "  !! 缺少 Qt6 运行库:$MISSING"
    echo "     请先安装："
    echo "       Arch/CachyOS : sudo pacman -S qt6-base qt6-svg"
    echo "       Ubuntu/Debian: sudo apt install libqt6svg6"
    echo "       Fedora       : sudo dnf install qt6-qtsvg"
    echo ""
    echo "     仍要继续安装吗？程序可能无法启动。"
    read -rp "     继续？[y/N] " ans
    [ "${ans,,}" = "y" ] || exit 1
else
    echo "  ✓ Qt6 运行库已就绪"
fi

# ── 3. 安装可执行文件 ──
echo "[2/5] 安装可执行文件 → $BIN_DIR/$APP_NAME"

# 升级场景：程序可能正在运行，占用目标文件
# 用 /proc/*/exe 判断（比命令行匹配可靠，单文件程序会 fork 子进程）
find_running() {
    local target
    target=$(readlink -f "$BIN_DIR/$APP_NAME" 2>/dev/null || echo "$BIN_DIR/$APP_NAME")
    for pid_dir in /proc/[0-9]*; do
        local pid="${pid_dir#/proc/}"
        local exe
        exe=$(readlink "$pid_dir/exe" 2>/dev/null || true)
        if [ "$exe" = "$target" ]; then
            echo "$pid"
        fi
    done
}

if [ -f "$BIN_DIR/$APP_NAME" ]; then
    RUNNING=$(find_running)
    if [ -n "$RUNNING" ]; then
        PIDS=$(echo "$RUNNING" | tr '\n' ' ')
        echo "  !! 检测到程序正在运行（PID: $PIDS）"
        echo "     升级需要先关闭程序。"
        read -rp "     现在自动关闭？[Y/n] " ans
        if [ "${ans,,}" != "n" ]; then
            for pid in $RUNNING; do
                kill "$pid" 2>/dev/null || true
            done
            sleep 2
            # 仍在运行则强制
            REMAIN=$(find_running)
            if [ -n "$REMAIN" ]; then
                for pid in $REMAIN; do
                    kill -9 "$pid" 2>/dev/null || true
                done
                sleep 1
            fi
            echo "  ✓ 已关闭"
        else
            echo "  !! 请手动关闭后重新运行安装脚本"
            exit 1
        fi
    fi
fi

run_root mkdir -p "$BIN_DIR"
# 先写临时文件再原子替换，避免「文本文件忙」
TMP_TARGET="$BIN_DIR/.${APP_NAME}.new.$$"
run_root cp "$BINARY" "$TMP_TARGET"
run_root chmod 755 "$TMP_TARGET"
run_root mv -f "$TMP_TARGET" "$BIN_DIR/$APP_NAME"
echo "  ✓ 已安装"

# ── 4. 安装桌面入口 ──
echo "[3/5] 安装桌面入口 → $DESKTOP_DIR"
run_root mkdir -p "$DESKTOP_DIR"
if [ -f "$SCRIPT_DIR/podman-gui.desktop" ]; then
    # 系统级安装时，Exec 要用绝对路径
    if [ "$MODE" = "system" ]; then
        sed 's|^Exec=.*|Exec=/usr/local/bin/podman-gui|' \
            "$SCRIPT_DIR/podman-gui.desktop" > /tmp/podman-gui.desktop.tmp
        run_root cp /tmp/podman-gui.desktop.tmp "$DESKTOP_DIR/$APP_NAME.desktop"
        rm -f /tmp/podman-gui.desktop.tmp
    else
        run_root cp "$SCRIPT_DIR/podman-gui.desktop" "$DESKTOP_DIR/$APP_NAME.desktop"
    fi
    run_root chmod 644 "$DESKTOP_DIR/$APP_NAME.desktop"
fi

# ── 5. 安装图标 ──
echo "[4/5] 安装图标"
# 检测当前 KDE 图标主题（KDE 不会去 hicolor 找应用图标）
CURRENT_THEME=""
if command -v kreadconfig6 >/dev/null 2>&1; then
    CURRENT_THEME=$(kreadconfig6 --file kdeglobals --group Icons --key Theme 2>/dev/null || true)
fi

THEMES="hicolor"
[ -n "$CURRENT_THEME" ] && THEMES="$THEMES $CURRENT_THEME ${CURRENT_THEME}-dark"

ICON_SRC="$SCRIPT_DIR/appicon"
if [ -d "$ICON_SRC" ]; then
    for theme in $THEMES; do
        for size in 16 22 24 32 48 64 128 256 512; do
            src="$ICON_SRC/podman-gui-${size}.png"
            [ -f "$src" ] || continue

            if [ "$theme" = "hicolor" ]; then
                dest="$ICON_BASE/hicolor/${size}x${size}/apps"
            else
                dest="$ICON_BASE/$theme/apps/$size"
            fi
            run_root mkdir -p "$dest"
            run_root cp "$src" "$dest/$APP_NAME.png"
        done
    done
    echo "  ✓ 已安装到主题: $THEMES"
else
    echo "  !! 未找到 appicon/ 目录，跳过图标"
fi

# ── 6. 刷新缓存 ──
echo "[5/5] 刷新系统缓存"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    run_root gtk-update-icon-cache -f -t "$ICON_BASE/hicolor" 2>/dev/null || true
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 --noincremental 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    run_root update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

# ══════════════════════════════════════════════════════════════
#  完成
# ══════════════════════════════════════════════════════════════
echo ""
echo "════════════════════════════════════════"
echo "  ✓ 安装完成"
echo "════════════════════════════════════════"
echo ""
echo "  启动方式："
echo "    · 应用菜单搜索「Podman」"
echo "    · 终端执行：podman-gui"
echo ""

# PATH 检查
case ":$PATH:" in
    *":$BIN_DIR:"*)
        echo "  ✓ $BIN_DIR 已在 PATH 中"
        ;;
    *)
        echo "  !! $BIN_DIR 不在 PATH 中"
        echo "     请把下面这行加到 ~/.bashrc 或 ~/.zshrc："
        echo "       export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo ""
echo "  前置条件：Podman socket 需运行"
echo "    systemctl --user enable --now podman.socket"
echo ""
echo "  卸载："
echo "    $SCRIPT_DIR/install.sh --uninstall"
echo ""
