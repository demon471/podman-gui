"""VM 内 sudo 执行

目标：用户授权一次，之后所有需要 root 的操作都自动走 sudo，
不再反复询问。

三层递进
────────
1. **免密 sudo**（首选）
   cloud-init 已经给用户配了 `NOPASSWD:ALL`，正常情况下
   `sudo -n <cmd>` 直接可用，不需要任何密码。

2. **已知密码 sudo**（老 VM / 镜像未生效）
   部分云镜像的 cloud-init 版本较旧，`sudo: ALL=(ALL) NOPASSWD:ALL`
   没写进 sudoers。此时用默认密码或用户输入的密码，
   通过 `sudo -S` 从 stdin 读。

3. **一次性提升**（首次遇到无权限时）
   用一个已知可用的密码把 `NOPASSWD` 规则写进
   `/etc/sudoers.d/`，从此这台 VM 永久免密 —— 用户只被打扰一次。
   规则文件随 VM 磁盘一起存在，除非删除虚拟机，否则一直有效。

设计要点
────────
· 密码只在内存里，不落盘（`_password` 模块级变量）。
· 提升成功后立刻把密码丢弃，后续全部走免密路径。
· 所有函数都返回 `(ok, out)`，调用方不需要处理异常。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# sudoers 规则文件名（放在 /etc/sudoers.d/ 下，需 0440 权限）
SUDOERS_FILE = "/etc/sudoers.d/90-podman-gui"
SUDOERS_RULE = "{user} ALL=(ALL) NOPASSWD: ALL"

# 本次会话缓存的密码（内存，不落盘）
_password: str | None = None

# 已确认免密的 VM 名字（避免每次操作都探测）
_nopass_cache: set[str] = set()


# ─────────────────────────────────────────
# 密码管理
# ─────────────────────────────────────────
def set_password(pwd: str | None) -> None:
    """设置 / 清除缓存的 sudo 密码"""
    global _password
    _password = pwd or None


def has_password() -> bool:
    return bool(_password)


def forget(name: str) -> None:
    """VM 被删除时清掉缓存"""
    _nopass_cache.discard(name)


# ─────────────────────────────────────────
# 探测
# ─────────────────────────────────────────
def can_sudo(vm, *, use_cache: bool = True) -> bool:
    """当前是否已经能免密 sudo"""
    name = vm.spec.name
    if use_cache and name in _nopass_cache:
        return True

    from core.vm import ssh

    ok, out = ssh.run_command(
        vm, "sudo -n true 2>/dev/null && echo SUDO_OK", timeout=15
    )
    if ok and "SUDO_OK" in (out or ""):
        _nopass_cache.add(name)
        return True
    return False


# ─────────────────────────────────────────
# 执行
# ─────────────────────────────────────────
def run(vm, command: str, *, timeout: int = 60) -> tuple[bool, str]:
    """以 root 身份执行命令

    command 不需要自带 sudo，本函数负责加。
    返回 (成功, 输出)。
    """
    from core.vm import ssh

    name = vm.spec.name

    # ── 1. 免密路径（最常见）──
    if can_sudo(vm):
        return ssh.run_command(
            vm, f"sudo -n {command}", timeout=timeout
        )

    # ── 2. 有密码 → sudo -S ──
    if _password:
        ok, out = _run_with_password(vm, command, timeout=timeout)
        if ok:
            return True, out

    # ── 3. 尝试自动提升（用默认密码试一次）──
    if ensure_nopass(vm):
        return ssh.run_command(
            vm, f"sudo -n {command}", timeout=timeout
        )

    return False, (
        "该虚拟机需要 root 权限才能执行此操作，但当前无法获得。\n"
        "请在详情页的「修改密码」里确认密码，或手动在 VM 内执行一次：\n"
        f"  echo '{SUDOERS_RULE.format(user=vm.spec.username)}' | "
        f"sudo tee {SUDOERS_FILE}"
    )


def _run_with_password(vm, command: str, *, timeout: int = 60) -> tuple[bool, str]:
    """用已知密码通过 sudo -S 执行"""
    import subprocess

    from core.vm.ssh import SSH_OPTS

    port = vm.ssh_port
    if not port or not vm.ssh_key.exists():
        return False, "SSH 未就绪"

    # `sudo -k` 先清掉可能缓存的凭据，确保 -S 一定读 stdin。
    # 另外把 lecture 关掉（lecture=never）：首次执行时 sudo 会先
    # 打印一段「We trust you have received...」教育文本，那段输出
    # 会和 stdin 的密码行错位，导致密码被当成命令 → 认证失败。
    cmd = [
        "ssh",
        "-i", str(vm.ssh_key),
        "-p", str(port),
        *SSH_OPTS,
        f"{vm.spec.username}@127.0.0.1",
        f"sudo -k; sudo -S -p '' lecture=never {command}",
    ]

    try:
        r = subprocess.run(
            cmd,
            input=(_password or "") + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)

    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        return True, out.strip()
    return False, out.strip()


# ─────────────────────────────────────────
# 一次性提升
# ─────────────────────────────────────────
def ensure_nopass(vm) -> bool:
    """让该 VM 永久免密 sudo

    做法：用一个已知可用的密码把规则写进 /etc/sudoers.d/。
    成功后这台 VM 之后所有 sudo 都免密 —— 用户只被打扰一次，
    规则随磁盘持久化，除非删除虚拟机否则一直有效。

    返回是否成功。
    """
    name = vm.spec.name
    if name in _nopass_cache:
        return True

    # 已经是免密，无需处理
    if can_sudo(vm):
        return True

    from core.vm import ssh

    user = vm.spec.username
    rule = SUDOERS_RULE.format(user=user)

    # 密码候选：用户设过的，或镜像默认密码
    candidates: list[str] = []
    if _password:
        candidates.append(_password)

    # 该 VM 当前实际密码（用户可能已经改过）
    try:
        from core.vm import manager as vm_manager

        cur = vm_manager.get_password(vm.spec.name)
        if cur and cur not in candidates:
            candidates.append(cur)
    except Exception:
        pass

    try:
        from core.vm.cloudinit import DEFAULT_PASSWORD

        if DEFAULT_PASSWORD not in candidates:
            candidates.append(DEFAULT_PASSWORD)
    except Exception:
        pass

    for pwd in candidates:
        # 关键：只**新增**规则文件，绝不改动/移动已有文件。
        # 早期实现曾把 cloud-init 的规则改名做实验，结果该用户
        # 彻底失去 sudo 权限（连还原命令都执行不了），只能靠
        # GRUB 单用户模式救回来。
        #
        # 这里的写法：
        #   1. 先验证密码可用（sudo -S -v）
        #   2. 用同一个 sudo 会话写规则（不用 tee 管道，
        #      避免密码与管道输入混淆）
        #   3. 校验语法（visudo -c），语法错会锁死 sudo
        #   4. 确认 sudo -n 生效
        script = (
            f"sudo -k; "
            f"echo {_quote(pwd)} | sudo -S -p '' -v 2>/dev/null && "
            f"echo {_quote(rule)} | sudo -n tee {SUDOERS_FILE} >/dev/null && "
            f"sudo -n chmod 0440 {SUDOERS_FILE} && "
            f"sudo -n visudo -c -f {SUDOERS_FILE} >/dev/null 2>&1 && "
            f"sudo -n true && echo PROMOTED"
        )
        ok, out = _run_plain(vm, script, timeout=40)
        if ok and "PROMOTED" in (out or ""):
            _nopass_cache.add(name)
            set_password(None)      # 提升成功 → 丢掉密码
            log.info("VM %s 已获得免密 sudo 权限", name)
            return True
        log.debug("  密码候选失败: %s", (out or "")[:120])

    log.debug("VM %s 自动提升免密失败", name)
    return False


def _run_plain(vm, script: str, *, timeout: int = 60) -> tuple[bool, str]:
    """执行一段已经自带 sudo 的脚本（不再额外加 sudo）"""
    from core.vm import ssh

    return ssh.run_command(vm, script, timeout=timeout)


def _quote(s: str) -> str:
    """单引号转义，安全嵌入 shell"""
    return "'" + s.replace("'", "'\\''") + "'"


# ─────────────────────────────────────────
# 便捷封装（业务代码直接用这些）
# ─────────────────────────────────────────
def chown(vm, path: str, owner: str | None = None) -> tuple[bool, str]:
    """把路径的属主改为当前用户"""
    user = owner or vm.spec.username
    return run(vm, f"chown {user}:{user} {path}")


def chmod(vm, path: str, mode: str) -> tuple[bool, str]:
    return run(vm, f"chmod {mode} {path}")


def mkdir(vm, path: str, mode: str = "0755") -> tuple[bool, str]:
    return run(vm, f"mkdir -p -m {mode} {path}")


def write_file(vm, path: str, content: str) -> tuple[bool, str]:
    """以 root 写文件（避免密码出现在命令行里）"""
    import shlex

    q = shlex.quote(content)
    return run(
        vm,
        f"sh -c 'printf %s {q} > {path}'",
        timeout=60,
    )


def status(vm) -> dict:
    """当前 sudo 状态（UI 展示用）"""
    return {
        "nopass": can_sudo(vm),
        "has_password": has_password(),
        "sudoers_file": SUDOERS_FILE,
    }
