#!/usr/bin/env bash
# 全量回归测试 —— 编译前必跑
#
# 用法：
#   ./run_tests.sh              # 快速测试（默认，跳过需要真实 VM 的）
#   ./run_tests.sh --all        # 全部（含需要真实 VM 的，慢）
#   ./run_tests.sh -k activity  # 只跑匹配的用例
#
# 设计原则：
#   · 默认几秒内跑完，适合每次改动后执行
#   · 任何一项失败就非零退出，能直接卡住打包流程
#   · 需要真实 VM 的测试用 @pytest.mark.needs_vm 隔离

set -uo pipefail

cd "$(dirname "$0")"

PY=".build-venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "✗ 未找到 $PY，请先创建构建虚拟环境" >&2
    exit 1
fi

MODE="${1:-fast}"
shift 2>/dev/null || true

echo "═══════════════════════════════════════════════════"
echo "  podman-gui 回归测试"
echo "═══════════════════════════════════════════════════"
echo ""

# ── 1. 语法检查（最快，先挡住明显的错）──
echo "▶ 1/4  语法检查"
FAIL=0
while IFS= read -r f; do
    if ! "$PY" -m py_compile "$f" 2>/tmp/pycompile.err; then
        echo "  ✗ $f"
        cat /tmp/pycompile.err | head -5
        FAIL=1
    fi
done < <(find core ui resources -name '*.py' -not -path '*/.build-venv/*'; echo main.py)

if [ "$FAIL" = "0" ]; then
    echo "  ✓ 全部通过"
else
    echo ""
    echo "✗ 语法检查失败，终止" >&2
    exit 1
fi
echo ""

# ── 2. 单元测试 ──
echo "▶ 2/4  单元测试"
if [ "$MODE" = "--all" ]; then
    echo "  （含 needs_vm，可能较慢）"
    "$PY" -m pytest tests/ -q --tb=short -p no:cacheprovider "$@" 2>&1 | tail -30
else
    "$PY" -m pytest tests/ -q --tb=short -p no:cacheprovider \
        -m "not needs_vm" "$@" 2>&1 | tail -30
fi

PYTEST_RC=${PIPESTATUS[0]}
echo ""

# ── 3. 静态检查：未定义名字（这类错误只会在运行时炸）──
echo "▶ 3/4  静态检查"
if "$PY" -m pyflakes --version >/dev/null 2>&1; then
    UNDEF=$("$PY" -m pyflakes ui/ core/ resources/ main.py 2>&1 \
            | grep "undefined name" || true)
    if [ -n "$UNDEF" ]; then
        echo "  ✗ 发现未定义名字（运行时会 NameError → 进程 abort）："
        echo "$UNDEF" | sed 's/^/    /'
        UNDEF_RC=1
    else
        echo "  ✓ 无未定义名字"
        UNDEF_RC=0
    fi
else
    echo "  ⚠ 未安装 pyflakes，跳过（pip install pyflakes）"
    UNDEF_RC=0
fi
echo ""

# ── 4. 关键模块导入检查（PyInstaller 会漏的）──
echo "▶ 4/4  关键模块导入"
"$PY" - <<'PYEOF'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

mods = [
    "core.async_task",
    "core.state",
    "core.vm.activity",
    "core.vm.daemon",
    "core.vm.files",
    "core.vm.images",
    "core.vm.keepalive",
    "core.vm.manager",
    "core.vm.qemu",
    "core.vm.shared_disk",
    "core.vm.sudo",
    "core.vm.wakeproxy",
]
bad = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        bad.append((m, str(e)[:80]))

if bad:
    for m, e in bad:
        print(f"  ✗ {m}: {e}")
    sys.exit(1)
print(f"  ✓ {len(mods)} 个模块全部可导入")
PYEOF
IMPORT_RC=$?
echo ""

# ── 汇总 ──
echo "═══════════════════════════════════════════════════"
if [ "$PYTEST_RC" = "0" ] && [ "$IMPORT_RC" = "0" ] && [ "${UNDEF_RC:-0}" = "0" ]; then
    echo "  ✓ 全部通过"
    echo "═══════════════════════════════════════════════════"
    exit 0
else
    echo "  ✗ 有失败项（pytest=$PYTEST_RC import=$IMPORT_RC undefined=${UNDEF_RC:-0}）"
    echo "═══════════════════════════════════════════════════"
    exit 1
fi
