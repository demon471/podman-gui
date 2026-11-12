#!/usr/bin/env bash
# 构建 + 安装 —— 强制先跑回归测试
#
# 用法：
#   ./build.sh              # 跑测试 → 打包 → 安装
#   ./build.sh --skip-tests # 跳过测试（仅紧急调试，慎用）
#
# 为什么把测试放在这里：
# 之前多次出现「改 A 坏 B」——修下载闪退时改坏了启动流程，
# 修启动又影响了保活。每次都靠用户复现才发现。
# 现在任何一次构建都必须先过全部测试。

set -euo pipefail

cd "$(dirname "$0")"

SKIP_TESTS=0
if [ "${1:-}" = "--skip-tests" ]; then
    SKIP_TESTS=1
    echo "⚠️  跳过测试（不推荐）"
    echo ""
fi

PY=".build-venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "✗ 未找到 $PY" >&2
    exit 1
fi

# ── 1. 回归测试 ──
if [ "$SKIP_TESTS" = "0" ]; then
    if ! ./run_tests.sh; then
        echo ""
        echo "✗ 测试未通过，构建中止。" >&2
        echo "  修复后重试；确需跳过请用 ./build.sh --skip-tests" >&2
        exit 1
    fi
    echo ""
fi

# ── 2. 打包 ──
echo "▶ 打包（PyInstaller，约 35 秒）…"
if ! "$PY" -m PyInstaller --noconfirm --clean podbox.spec > /tmp/podbox-build.log 2>&1; then
    echo "✗ 打包失败，日志尾部：" >&2
    tail -30 /tmp/podbox-build.log >&2
    exit 1
fi
echo "  ✓ 打包完成：$(du -h dist/podbox | cut -f1)"

# ── 3. 安装 ──
echo "▶ 安装到 ~/.local/bin/podbox"
install -Dm755 dist/podbox "$HOME/.local/bin/podbox"
echo "  ✓ 安装完成"

# ── 4. 冒烟测试：打包版能否启动 ──
echo "▶ 冒烟测试（打包版冷启动 15 秒）"

pkill -x podbox 2>/dev/null || true
sleep 1

# 不用 coredumpctl 计数判断：coredump 会被轮转/清理，
# 行数变少就会误报失败。直接看进程是否存活 + 日志有无崩溃痕迹。
QT_QPA_PLATFORM=offscreen QT_QPA_PLATFORMTHEME="" \
    timeout 20 "$HOME/.local/bin/podbox" > /tmp/podbox-smoke.log 2>&1 &
SMOKE_PID=$!
sleep 15

ALIVE=0
if kill -0 "$SMOKE_PID" 2>/dev/null; then
    ALIVE=1
fi

CRASH=0
if grep -qE "Fatal Python error|Traceback \(most recent|Aborted|core dumped" \
        /tmp/podbox-smoke.log 2>/dev/null; then
    CRASH=1
fi

if [ "$ALIVE" = "1" ] && [ "$CRASH" = "0" ]; then
    echo "  ✓ 启动正常，无崩溃"
    kill "$SMOKE_PID" 2>/dev/null || true
    wait "$SMOKE_PID" 2>/dev/null || true
else
    echo "  ✗ 冒烟测试失败" >&2
    echo "    存活=$ALIVE  崩溃迹象=$CRASH" >&2
    echo "    日志：" >&2
    head -30 /tmp/podbox-smoke.log >&2
    exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✓ 构建完成，可以启动测试"
echo "═══════════════════════════════════════════════════"
