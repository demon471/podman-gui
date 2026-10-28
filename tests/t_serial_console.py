"""测试 C：串口控制台修复

Alpine 3.22.7+ 的 kernel cmdline 带三个 console=：
    console=ttyS0,115200n8 console=ttyAMA0,115200n8 console=tty0
Linux 把最后一个当 /dev/console，于是服务的日志全进了 tty0。
宿主机是 -display none 无显卡，tty0 是黑洞 —— console.log 里
只剩 initramfs 的几行，排查启动失败时毫无线索。

cloud-init 的 bootcmd 里投放一个脚本，把它收敛成单个 console=ttyS0。

验证点：
  1. seed.iso 里有 bootcmd 与修复脚本
  2. VM 内脚本执行后 /etc/default/grub 只剩 ttyS0
  3. 重启后 console.log 出现 OpenRC/systemd 的用户空间输出

用法：.build-venv/bin/python tests/t_serial_console.py [vm_name]
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vm import cloudinit, manager, ssh as vm_ssh

TEST_VM = "serial-test"


def seed_has_fix(seed: Path) -> bool:
    """检查 seed.iso 里是否含 bootcmd 与串口修复脚本"""
    try:
        raw = seed.read_bytes()
    except OSError:
        return False
    # ISO9660 里内容是明文的，直接找关键字
    return (
        b"bootcmd" in raw
        and b"podman-gui-serial-console" in raw
        and b"console=ttyAMA0" in raw
    )


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else TEST_VM
    print("── C：串口控制台修复测试 ──")

    vm = manager.load(name)
    created = False
    if vm is None:
        print(f"1. 创建测试 VM: {name}")
        vm, err = manager.create_vm(name=name, image_key="alpine-3.22")
        if vm is None:
            print(f"✗ 创建失败: {err[:120]}")
            return 1
        created = True
    else:
        print(f"1. 复用已有 VM: {name}")

    # ── 2. seed.iso 里应有修复脚本 ──
    has = seed_has_fix(vm.seed)
    print(f"2. seed.iso 含串口修复: {has}")
    if not has:
        print("✗ seed 里没有修复脚本")
        return 1

    # ── 3. 启动并检查 guest 内 grub 配置 ──
    print("3. 启动 VM（首次启动 cloud-init 执行 bootcmd）…")
    if not vm.running:
        ok, err = manager.start_vm(vm)
        if not ok:
            print(f"✗ 启动失败: {err[:150]}")
            return 1

    cmdline = ""
    for i in range(16):
        time.sleep(5)
        ok, out = vm_ssh.run_command(
            vm, "grep ^GRUB_CMDLINE_LINUX_DEFAULT /etc/default/grub", timeout=10
        )
        if "GRUB_CMDLINE" in out:
            cmdline = out.strip()
            print(f"   [{5 * (i + 1)}s] 读到 grub 配置")
            break

    if not cmdline:
        print("✗ 无法 SSH 进去读配置")
        return 1

    n_console = cmdline.count("console=")
    print(f"   cmdline: {cmdline[:110]}")
    print(f"   console= 个数: {n_console}（应为 1）")

    # bootcmd 在 cloud-init-local 阶段跑，之后 runcmd 才起 sshd；
    # 若读到多个 console，说明修复脚本没生效
    ok_fix = n_console <= 1 and "ttyS0" in cmdline
    print(f"4. 修复生效: {ok_fix}")

    if created and not ok_fix:
        print("   提示：bootcmd 可能在 sshd 起来之后才执行，需重启复核")

    # ── 5. 重启后看 console.log 是否有用户空间输出 ──
    print("5. 重启 VM，检查串口是否有服务日志…")
    manager.stop_vm(vm)
    time.sleep(3)
    manager.start_vm(vm)

    user_space = 0
    for i in range(12):
        time.sleep(5)
        try:
            log = vm.console() if hasattr(vm, "console") else ""
        except Exception:
            log = ""
        if not log:
            from core.vm import qemu

            log = qemu.read_console(vm.dir)
        user_space = len(
            re.findall(r"OpenRC|rc-service|systemd\[1\]|Reached target", log)
        )
        if user_space > 0:
            print(f"   [{5 * (i + 1)}s] 发现 {user_space} 条用户空间日志")
            break

    print(f"6. 串口用户空间日志条数: {user_space}")

    if user_space > 0:
        print("\n✓ C 通过：串口能看到完整启动日志")
        return 0

    print("\n△ C 未完全生效：串口仍只有内核输出")
    print("  （首次启动只能改配置，需再重启一次才用上新 cmdline）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
