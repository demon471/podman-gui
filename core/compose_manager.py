"""Compose 项目管理

目录约定（与 /data 现有结构一致）：
    <root>/<project>/compose/docker-compose.yml
    <root>/<project>/data/          ← 数据卷
    <root>/<project>/config/        ← 配置文件

也兼容：
    <root>/<project>/docker-compose.yml
    <root>/<project>/compose.yml
    <root>/docker-compose.yml       ← root 本身是一个项目

通过 podman-compose CLI 执行操作（up/down/restart/logs/ps）。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_ROOT = Path("/data")

# compose 文件名候选（按优先级）
COMPOSE_FILENAMES = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
]


@dataclass
class ComposeService:
    """compose 文件中的一个服务"""

    name: str
    image: str = ""
    container_name: str = ""
    ports: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    command: str = ""
    restart: str = ""
    depends_on: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)

    @property
    def port_summary(self) -> str:
        return ", ".join(self.ports) if self.ports else "—"

    @property
    def volume_summary(self) -> str:
        return ", ".join(self.volumes) if self.volumes else "—"


@dataclass
class ComposeProject:
    """一个 compose 项目"""

    name: str
    path: Path                      # compose 文件路径
    root: Path                      # 项目根目录（含 compose/ 的目录）
    services: list[ComposeService] = field(default_factory=list)
    error: str = ""                 # 解析错误

    @property
    def workdir(self) -> Path:
        """执行 podman-compose 的工作目录"""
        return self.path.parent

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def service_count(self) -> int:
        return len(self.services)

    @property
    def image_summary(self) -> str:
        imgs = [s.image for s in self.services if s.image]
        if not imgs:
            return "—"
        if len(imgs) == 1:
            return imgs[0]
        return f"{imgs[0]} 等 {len(imgs)} 个"

    def __str__(self) -> str:
        return self.name


# ─────────────────────────────────────────
# 扫描
# ─────────────────────────────────────────
def _find_compose_file(directory: Path) -> Path | None:
    for fn in COMPOSE_FILENAMES:
        p = directory / fn
        if p.is_file():
            return p
    return None


def scan_project(dirpath: Path, root: Path) -> ComposeProject | None:
    """扫描单个目录，识别 compose 项目"""
    # 约定1：<project>/compose/docker-compose.yml
    cf = _find_compose_file(dirpath / "compose")
    if cf:
        proj = ComposeProject(name=dirpath.name, path=cf, root=dirpath)
        _parse(proj)
        return proj

    # 约定2：<project>/docker-compose.yml
    cf = _find_compose_file(dirpath)
    if cf:
        proj = ComposeProject(name=dirpath.name, path=cf, root=dirpath)
        _parse(proj)
        return proj

    return None


def scan_root(root: Path | str = DEFAULT_ROOT) -> list[ComposeProject]:
    """扫描根目录下所有 compose 项目

    遍历 root 的直接子目录，识别符合约定的项目。
    """
    root = Path(root)
    if not root.is_dir():
        return []

    projects: list[ComposeProject] = []

    # root 自身是否是一个项目
    cf = _find_compose_file(root)
    if cf:
        proj = ComposeProject(name=root.name, path=cf, root=root)
        _parse(proj)
        projects.append(proj)

    # 子目录
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return projects

    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        proj = scan_project(entry, root)
        if proj:
            projects.append(proj)

    return projects


# ─────────────────────────────────────────
# 解析
# ─────────────────────────────────────────
def _parse(proj: ComposeProject) -> None:
    """解析 compose 文件，填充 services"""
    try:
        raw = proj.path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        proj.error = f"YAML 解析失败: {e}"
        return
    except OSError as e:
        proj.error = f"读取失败: {e}"
        return

    if not isinstance(data, dict):
        proj.error = "文件格式不正确（根节点应为映射）"
        return

    services = data.get("services") or {}
    if not isinstance(services, dict):
        proj.error = "缺少 services 段"
        return

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        s = ComposeService(name=str(svc_name))
        s.image = str(svc.get("image", "") or "")
        s.container_name = str(svc.get("container_name", "") or "")
        s.restart = str(svc.get("restart", "") or "")

        # ports: ["8080:80"] 或 [{target: 80, published: 8080}]
        for p in svc.get("ports") or []:
            if isinstance(p, dict):
                pub = p.get("published", "")
                tgt = p.get("target", "")
                s.ports.append(f"{pub}:{tgt}" if pub else str(tgt))
            else:
                s.ports.append(str(p))

        # volumes: ["../data:/data:Z"] 或 [{type: bind, source, target}]
        for v in svc.get("volumes") or []:
            if isinstance(v, dict):
                src = v.get("source", "")
                tgt = v.get("target", "")
                s.volumes.append(f"{src}:{tgt}" if src else str(tgt))
            else:
                s.volumes.append(str(v))

        # environment: {K: V} 或 ["K=V"]
        env = svc.get("environment") or {}
        if isinstance(env, dict):
            s.environment = {str(k): str(v) for k, v in env.items()}
        elif isinstance(env, list):
            for item in env:
                item = str(item)
                if "=" in item:
                    k, v = item.split("=", 1)
                    s.environment[k] = v
                else:
                    s.environment[item] = ""

        cmd = svc.get("command", "")
        if isinstance(cmd, list):
            s.command = " ".join(str(x) for x in cmd)
        else:
            s.command = str(cmd or "")

        dep = svc.get("depends_on") or []
        if isinstance(dep, dict):
            s.depends_on = list(dep.keys())
        elif isinstance(dep, list):
            s.depends_on = [str(x) for x in dep]

        prof = svc.get("profiles") or []
        s.profiles = [str(x) for x in prof]

        proj.services.append(s)


# ─────────────────────────────────────────
# podman-compose 执行
# ─────────────────────────────────────────
_ANSI_RE = None


def strip_ansi(text: str) -> str:
    """剥离 ANSI 转义序列（颜色、光标控制等）

    podman-compose logs 默认带彩色前缀，如：
        \x1b[1;32m[postgres] |\x1b[0m \x1b[37m...\x1b[0m
    """
    global _ANSI_RE
    if _ANSI_RE is None:
        import re

        _ANSI_RE = re.compile(
            r"\x1b\[[0-9;?]*[a-zA-Z]"      # CSI 序列
            r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC 序列
            r"|\x1b[@-Z\\-_]"              # 其他双字节序列
        )
    return _ANSI_RE.sub("", text)


def _which_compose() -> tuple[str, list[str]] | None:
    """找到可用的 compose 命令，返回 (命令, 前缀参数)"""
    import shutil

    # 优先 podman-compose
    if shutil.which("podman-compose"):
        return "podman-compose", []
    # 其次 podman compose（内置 provider）
    if shutil.which("podman"):
        return "podman", ["compose"]
    if shutil.which("docker-compose"):
        return "docker-compose", []
    return None


def compose_available() -> tuple[bool, str]:
    """检查 compose 是否可用"""
    found = _which_compose()
    if not found:
        return False, "未找到 podman-compose 或 podman compose"
    cmd, prefix = found
    try:
        r = subprocess.run(
            [cmd] + prefix + ["--version"],
            capture_output=True, text=True, timeout=10,
        )
        ver = (r.stdout or r.stderr).strip().splitlines()
        return r.returncode == 0, ver[0] if ver else cmd
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def run_compose(
    proj: ComposeProject,
    action: str,
    *,
    detach: bool = True,
    timeout: int = 300,
    extra: list[str] | None = None,
) -> tuple[bool, str]:
    """执行 compose 操作

    action: up / down / restart / stop / start / ps / pull / logs
    返回 (成功, 输出)
    """
    found = _which_compose()
    if not found:
        return False, "未找到 podman-compose，请先安装：sudo pacman -S podman-compose"

    cmd, prefix = found

    args: list[str] = [cmd] + prefix + ["-f", str(proj.path)]

    if action == "up":
        args += ["up"]
        if detach:
            args.append("-d")
    elif action == "down":
        args += ["down"]
    elif action in ("restart", "stop", "start", "ps", "pull"):
        args.append(action)
    elif action == "logs":
        args += ["logs", "--tail", "200"]
    else:
        return False, f"不支持的操作: {action}"

    if extra:
        args += extra

    env = os.environ.copy()
    env.setdefault("COMPOSE_PROJECT_NAME", proj.name)

    try:
        r = subprocess.run(
            args,
            cwd=str(proj.workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"操作超时（{timeout}s）"
    except OSError as e:
        return False, str(e)

    output = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, strip_ansi(output).strip()


def get_status(proj: ComposeProject, containers: list[dict] | None = None) -> dict[str, str]:
    """查询项目内各服务的运行状态

    直接比对 Podman 容器列表（比 podman-compose ps 更可靠，
    因为 compose 的项目名前缀规则与实际容器名可能不一致）。

    containers: 可传入已获取的容器列表以复用（避免重复请求）

    返回 {服务名: 状态}，状态值：running / exited / created / missing
    """
    if containers is None:
        try:
            from core.podman_client import get_client

            containers = get_client().list_containers()
        except Exception:
            return {}

    # 建立名字索引：容器名 → 状态
    by_name: dict[str, str] = {}
    for c in containers:
        names = c.get("Names") or []
        if isinstance(names, str):
            names = [names]
        state = str(c.get("State", "")).lower()
        for n in names:
            n = str(n).lstrip("/")
            by_name[n] = state
            # 去掉 compose 前缀形式 <proj>_<svc>_1 / <proj>-<svc>-1
            m = re.match(rf"^{re.escape(proj.name)}[_-](.+?)[_-]\d+$", n)
            if m:
                by_name.setdefault(m.group(1), state)

    result: dict[str, str] = {}
    for svc in proj.services:
        candidates = [
            svc.container_name,
            svc.name,
            f"{proj.name}_{svc.name}_1",
            f"{proj.name}-{svc.name}-1",
        ]
        state = "missing"
        for cand in candidates:
            if cand and cand in by_name:
                state = by_name[cand]
                break
        result[svc.name] = state

    return result


def validate_yaml(text: str) -> tuple[bool, str, dict]:
    """校验 YAML 文本

    返回 (有效, 错误信息, 解析结果)
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return False, f"YAML 语法错误:\n{e}", {}

    if not isinstance(data, dict):
        return False, "根节点必须是映射（key: value 结构）", {}

    if "services" not in data:
        return False, "缺少 services 段", data

    if not isinstance(data["services"], dict):
        return False, "services 必须是映射", data

    if not data["services"]:
        return False, "services 为空", data

    return True, "", data


def create_project(
    root: Path | str,
    name: str,
    template: str = "basic",
) -> tuple[bool, str, Path | None]:
    """创建新 compose 项目

    目录结构：
        <root>/<name>/compose/docker-compose.yml
        <root>/<name>/data/
        <root>/<name>/config/

    返回 (成功, 消息, compose 文件路径)
    """
    root = Path(root)
    proj_dir = root / name
    compose_dir = proj_dir / "compose"

    if proj_dir.exists():
        return False, f"目录已存在: {proj_dir}", None

    templates = {
        "basic": """# {name}
# 用法：
#   cd {compose_dir}
#   podman-compose up -d
#   podman-compose down

services:
  {name}:
    image: docker.io/library/alpine:latest
    container_name: {name}
    restart: always
    command: ["sh", "-c", "while true; do sleep 3600; done"]
    environment:
      TZ: Asia/Shanghai
    volumes:
      - ../data:/data:Z
      - ../config:/config:Z
""",
        "empty": """# {name}

services:
  {name}:
    image: docker.io/library/alpine:latest
    container_name: {name}
    restart: always
""",
    }

    content = templates.get(template, templates["basic"]).format(
        name=name, compose_dir=compose_dir
    )

    try:
        compose_dir.mkdir(parents=True, exist_ok=False)
        (proj_dir / "data").mkdir()
        (proj_dir / "config").mkdir()
        cf = compose_dir / "docker-compose.yml"
        cf.write_text(content, encoding="utf-8")
    except OSError as e:
        return False, f"创建失败: {e}", None

    return True, f"已创建 {proj_dir}", cf
