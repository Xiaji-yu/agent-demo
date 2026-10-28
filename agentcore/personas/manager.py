"""人格（Persona）系统：OpenClaw 兼容的 SOUL.md 布局，供 AgentEngine 注入 system prompt。

**布局**（与 OpenClaw / picoclaw 的 workspace 约定一致，靠**文件位置**标识人格，
不再用 frontmatter——OpenClaw 的 SOUL.md 是纯 markdown，加 frontmatter 会破坏互通）::

    data/workspace/                 ← PERSONAS_DIR，容器内 /app/data/workspace
    ├── SOUL.md                     ← 默认人格（根级，无 frontmatter）
    └── personas/
        ├── aemeath/SOUL.md         ← 可切换人格，**目录名 = 人格名**
        └── <name>/SOUL.md
        └── <name>/ABOUT.txt        ← 可选，web「人格」页的描述来源

默认目录从本模块所在目录（旧的 ``agentcore/personas/``）改为 **``data/workspace``**
（相对 CWD 解析）：那才是 compose 挂载出去的目录，用户改文件即生效，不必重建镜像。
``PERSONAS_DIR`` 仍可覆盖（容器外部署 / 测试）。
"""

from __future__ import annotations

import builtins
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 相对 CWD：容器内 WORKDIR 是 /app，即 /app/data/workspace（compose 已挂载）。
# 不用 Path(__file__).parent——那是镜像里的 agentcore/personas，用户改不到。
DEFAULT_PERSONAS_DIR = Path("data/workspace")

# 目录名会进 /persona use <name> 与 web URL，必须挡掉路径穿越与空白。
# 比旧的 _NAME_RE 宽松：允许中文等 unicode 字母数字（OpenClaw 的 workspace 名不限 ASCII），
# 但拒绝 "/"、".."、点开头（隐藏目录）与纯空白。
_NAME_RE = re.compile(r"^[^\s/\\]+$")
_RESERVED_NAMES = {".", ".."}

# 落盘上限：body 每轮进 system prompt（engine._load_persona_text 只取 body），
# 无上限会把 token 预算撑爆；description 只用于 /persona list 展示，宽松封顶。
# persona 属管理员显式配置（等同 .env/config.yaml 的受信内容，不过 safety 围栏），
# 这两个上限防的是误操作导入巨型文件，不是防攻击。
_BODY_MAX = 20000
_DESCRIPTION_MAX = 500

_SOUL_FILENAME = "SOUL.md"
_ABOUT_FILENAME = "ABOUT.txt"
_PERSONAS_SUBDIR = "personas"


def valid_persona_name(name: str) -> bool:
    """人格名（= personas/ 下的目录名）是否合法。

    与旧 ``_NAME_RE`` 的差别：允许中文等 unicode，但显式拒绝路径分隔符、
    ``.``/``..`` 与前导点——目录名要能安全拼路径。
    """
    text = (name or "").strip()
    if not text or text in _RESERVED_NAMES:
        return False
    if text.startswith("."):
        return False
    return bool(_NAME_RE.match(text))


def render_soul_md(body: str) -> str:
    """渲染 SOUL.md 全文（**纯 markdown，无 frontmatter**）。

    web 写面的两段确认用它生成待写入文本做 HMAC 绑定——确认码绑定的就是最终落盘
    内容，与 ``save`` 共用同一渲染函数，不会两头漂移。
    """
    body = body or ""
    if len(body) > _BODY_MAX:
        raise ValueError(f"persona body too long: {len(body)} > {_BODY_MAX}")
    return f"{body.strip()}\n"


def render_about(description: str) -> str:
    """渲染 ABOUT.txt（web「人格」页的描述落点，纯文本一行）。"""
    description = (description or "").strip()
    if len(description) > _DESCRIPTION_MAX:
        raise ValueError(
            f"persona description too long: {len(description)} > {_DESCRIPTION_MAX}"
        )
    return f"{description}\n" if description else ""


@dataclass
class Persona:
    name: str
    description: str = ""
    body: str = ""
    default: bool = False
    file: str = ""  # SOUL.md 的绝对路径，便于展示


def _first_heading(text: str) -> str:
    """取 md 的首个 `# 标题` 作为 description 兜底（web 未填 ABOUT.txt 时）。"""
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            title = s.lstrip("#").strip()
            # 去掉 OpenClaw 惯用的「- Who You Are」后缀，只留主干
            title = re.split(r"\s+[-–—]\s+", title)[0].strip()
            if title:
                return title[:_DESCRIPTION_MAX]
    return ""


class PersonaManager:
    def __init__(self, dir_path: str | Path | None = None):
        env_dir = (os.getenv("PERSONAS_DIR") or "").strip()
        if env_dir:
            self.dir = Path(env_dir)
        elif dir_path:
            self.dir = Path(dir_path)
        else:
            self.dir = DEFAULT_PERSONAS_DIR
        self.dir = self.dir.resolve()
        self._by_name: dict[str, Persona] | None = None
        self._scan_ts: float = 0.0
        # 重新扫描的 TTL：热新增人格后最多此秒数内可见，避免每轮对话都读盘
        self._ttl = 2.0
        # 已报过的故障 key（_warn_once 用）；refresh() 时清空
        self._warned: set[tuple[str, str]] = set()

    # ---------------------------------------------------------------- 扫描

    def _ensure_loaded(self) -> None:
        now = time.monotonic()
        if self._by_name is None or now - self._scan_ts > self._ttl:
            self._by_name = self._scan()
            self._scan_ts = now

    def refresh(self) -> None:
        # L18（REVIEW-2ed7c63..1b16fa7）：先清去重集再扫描——refresh 是显式的
        # 「重新读一遍」，应当把现存故障再报一次；否则一次静默就永远是静默。
        self._warned.clear()
        self._by_name = self._scan()
        self._scan_ts = time.monotonic()

    def _warn_once(self, key: tuple[str, str], msg: str, *args) -> None:
        """同一故障只报一次（L18，REVIEW-2ed7c63..1b16fa7）。

        ``_scan`` 被 ``_ensure_loaded`` 每 2 秒重跑一次（热新增人格的 TTL），
        一个坏文件会把它那条 WARNING 反复刷出来，把真正的告警埋掉。按 key 去重，
        新出现的故障仍会被报（key 不同）。
        """
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(msg, *args)

    def _read(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            self._warn_once(("read", path.name), "cannot read persona file %s", path)
            return None

    def _load_one(self, name: str, soul: Path, *, default: bool) -> Persona | None:
        text = self._read(soul)
        if text is None:
            return None
        # 上限判在 **strip 后的正文**上：落盘内容是 body.strip() + "\n"，
        # 直接比原始长度会把边界值（正好 20000）误判成超长而静默丢弃。
        body = text.strip()
        if len(body) > _BODY_MAX:
            self._warn_once(
                ("toolong", name),
                "persona %s too long: %d > %d, skipped",
                name,
                len(body),
                _BODY_MAX,
            )
            return None
        about = soul.parent / _ABOUT_FILENAME
        description = ""
        if about.is_file():
            raw = self._read(about)
            if raw:
                description = raw.strip().splitlines()[0][:_DESCRIPTION_MAX]
        if not description:
            description = _first_heading(body)
        return Persona(
            name=name,
            description=description,
            body=body,
            default=default,
            file=str(soul),
        )

    def _scan(self) -> dict[str, Persona]:
        """扫 根级 SOUL.md（默认人格）+ personas/<name>/SOUL.md（可切换人格）。

        目录不存在不是错误——全新部署还没放文件时人人都是这个状态，告警一次即可。
        """
        personas: dict[str, Persona] = {}
        if not self.dir.is_dir():
            self._warn_once(
                ("dir", str(self.dir)), "personas dir not found: %s", self.dir
            )
            return personas

        # 根级 SOUL.md = 默认人格。名字固定 "default"，供 PERSONAS_DEFAULT 兜底引用。
        root_soul = self.dir / _SOUL_FILENAME
        if root_soul.is_file():
            p = self._load_one("default", root_soul, default=True)
            if p is not None:
                personas["default"] = p

        sub = self.dir / _PERSONAS_SUBDIR
        if not sub.is_dir():
            return personas
        for child in sorted(sub.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if not valid_persona_name(name):
                self._warn_once(
                    ("badname", name), "persona dir name %r invalid, skipped", name
                )
                continue
            soul = child / _SOUL_FILENAME
            if not soul.is_file():
                # 没有 SOUL.md 的目录不是人格（可能是用户放素材的地方），静默跳过
                continue
            p = self._load_one(name, soul, default=False)
            if p is not None:
                personas[name] = p
        return personas

    # ---------------------------------------------------------------- 查询

    def list(self) -> builtins.list[Persona]:
        self._ensure_loaded()
        personas = self._by_name
        if personas is None:  # _ensure_loaded 保证非 None；防御性兜底
            return []
        return sorted(personas.values(), key=lambda p: (not p.default, p.name))

    def get(self, name: str) -> Persona | None:
        if not valid_persona_name(name):
            return None
        self._ensure_loaded()
        personas = self._by_name
        return personas.get(name) if personas is not None else None

    def default(self) -> Persona | None:
        """取默认人格，优先级：``PERSONAS_DEFAULT`` env > ``.default_persona`` 标记
        （web「设为默认」落的）> 根级 ``SOUL.md``。

        标记文件是纯文本一行人格名；指向的人格被删/改名时告警一次并继续往下回落，
        不让整个会话失去人格。
        """
        env_name = (os.getenv("PERSONAS_DEFAULT") or "").strip()
        if env_name:
            p = self.get(env_name)
            if p is not None:
                return p
            # L18（REVIEW-2ed7c63..1b16fa7）：default() 每轮对话都可能被调，
            #  PERSONAS_DEFAULT 配错时这条会每轮刷一次。同一个错名只报一次。
            self._warn_once(
                ("default", env_name),
                "PERSONAS_DEFAULT %r not found; falling back",
                env_name,
            )
        marker = self._default_marker()
        if marker.is_file():
            try:
                marked = marker.read_text(encoding="utf-8").strip()
            except OSError:
                marked = ""
            if marked:
                p = self.get(marked)
                if p is not None:
                    return p
                self._warn_once(
                    ("marker", marked),
                    "default persona marker %r not found; falling back",
                    marked,
                )
        for p in self.list():
            if p.default:
                return p
        return None

    # ---------------------------------------------------------------- 写面

    def save(
        self, name: str, description: str = "", body: str = "", default: bool = False
    ) -> Persona:
        """把一个人格写入 ``personas/<name>/``（SOUL.md + 可选 ABOUT.txt）。

        同名覆盖；写完立即刷新缓存。校验失败抛 ``ValueError``（调用方转 4xx），
        IO 失败原样抛 ``OSError``。

        ``default=True`` 的语义：**把该人格提为默认**——实现方式是更新
        ``PERSONAS_DEFAULT`` 之外的一层，即把名字写进 ``self.dir/.default_persona``
        （纯文本一行）。不采用「移动根级 SOUL.md」是因为那会毁掉用户的默认文件。
        """
        name = (name or "").strip()
        if not valid_persona_name(name):
            raise ValueError(f"invalid persona name: {name!r}")
        if name == "default":
            # "default" 保留给根级 SOUL.md，避免两个来源打架
            raise ValueError("persona name 'default' is reserved for root SOUL.md")
        body_text = render_soul_md(body)
        about_text = render_about(description)
        target = self.dir / _PERSONAS_SUBDIR / name
        target.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target / _SOUL_FILENAME, body_text)
        if about_text:
            self._atomic_write(target / _ABOUT_FILENAME, about_text)
        elif (target / _ABOUT_FILENAME).is_file():
            # 描述被清空：删掉旧的 ABOUT.txt，让它回落首个标题
            (target / _ABOUT_FILENAME).unlink(missing_ok=True)
        if default:
            self._set_default_marker(name)
        self.refresh()
        persona = self.get(name)
        if persona is None:  # refresh 后仍读不到 = 落盘没成功，别让调用方以为写好了
            raise OSError(f"persona {name!r} not readable after save")
        return persona

    def _atomic_write(self, path: Path, text: str) -> None:
        """原子替换 + 0600（umask 只能去位，os.open 显式 mode 一步到位）。

        ``O_NOFOLLOW`` 防 tmp 名被预置 symlink 截断目标——与仓库其它落盘点
        （providers/totp/config_write）同一口径。**Windows 无此常量**，用
        ``getattr`` 取 0 兜底：POSIX 上语义不变，Windows 上退化为普通创建
        （该平台的 symlink 语义本就需要特权，不构成同等风险）。
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _default_marker(self) -> Path:
        return self.dir / ".default_persona"

    def _set_default_marker(self, name: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._default_marker(), f"{name}\n")

    def delete(self, name: str) -> bool:
        """删除一个人格目录；不存在返回 False。校验失败抛 ``ValueError``。"""
        name = (name or "").strip()
        if not valid_persona_name(name):
            raise ValueError(f"invalid persona name: {name!r}")
        target = self.dir / _PERSONAS_SUBDIR / name
        if not target.is_dir():
            return False
        # 只删这两个本机制生成的文件，不 rmtree——用户可能往目录里放了别的东西
        for fname in (_SOUL_FILENAME, _ABOUT_FILENAME):
            (target / fname).unlink(missing_ok=True)
        try:
            target.rmdir()
        except OSError:
            # 目录里还有用户文件：留着目录，只当「人格已删」
            pass
        marker = self._default_marker()
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == name:
            marker.unlink(missing_ok=True)
        self.refresh()
        return True


def load_manager() -> PersonaManager:
    return PersonaManager()
