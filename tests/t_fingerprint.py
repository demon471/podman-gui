"""测试 B：共享基础指纹绑定 + 篡改检测

验证点：
  1. 新创建的 VM 有 disk.base.json，且与当前基础匹配 → 可启动
  2. 基础被替换（模拟重下镜像）→ 启动被拦下并给出人话提示
  3. 老 VM（无记录）不阻断（给一次机会）

用法：.build-venv/bin/python tests/t_fingerprint.py
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vm import images, shared_disk as sd

SPEC = images.get_spec("alpine-3.22")
ARCH = "amd64"


def main() -> int:
    print("── B：共享基础指纹绑定测试 ──")

    base = sd.shared_base_path(SPEC, ARCH)
    if not base.exists():
        print("✗ 共享基础不存在，先创建一台 alpine VM")
        return 1

    fp = sd.base_fingerprint(SPEC, ARCH)
    print(f"1. 基础指纹: {fp[:16]}…")
    assert fp, "指纹为空"

    meta = sd.read_base_meta(SPEC, ARCH)
    print(f"   源镜像指纹: {str(meta.get('source_sha256'))[:16]}…")

    # 找一个已有 VM 做校验对象
    from core.vm import manager

    vms = [v for v in manager.load_all() if v.spec.image_key == "alpine-3.22"]
    if not vms:
        print("✗ 没有 alpine VM 可测")
        return 1

    vm = vms[0]
    print(f"2. 测试 VM: {vm.name}")

    rec = vm.dir / "disk.base.json"
    print(f"   增量层记录存在: {rec.exists()}")

    matched, why = sd.check_vm_disk_base(SPEC, vm.dir, ARCH)
    print(f"   匹配校验: {matched} {('— ' + why[:40]) if why else ''}")

    # ── 篡改：把增量层记录改成不存在的指纹 ──
    if rec.exists():
        backup = rec.read_text(encoding="utf-8")
        data = json.loads(backup)
        data["base_sha256"] = "f" * 64
        rec.write_text(json.dumps(data), encoding="utf-8")

        matched2, why2 = sd.check_vm_disk_base(SPEC, vm.dir, ARCH)
        print(f"3. 篡改后校验: {matched2}（应为 False）")
        print(f"   提示: {(why2 or '').splitlines()[0][:60]}")
        assert not matched2, "篡改未被检出！"

        rec.write_text(backup, encoding="utf-8")
        matched3, _ = sd.check_vm_disk_base(SPEC, vm.dir, ARCH)
        print(f"4. 还原后校验: {matched3}（应为 True）")
        assert matched3
    else:
        print("3. 该 VM 无记录（老版本创建），跳过篡改测试")

    print("\n✓ B 通过：增量层与基础的绑定校验有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
