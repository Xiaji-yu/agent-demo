"""人格（Persona）系统：从 md 文件加载人格，供 AgentEngine 注入 system prompt。

每个人格文件是一个 markdown，形如：

    ---
    name: assistant
    description: 通用 AI 助手
    default: true
    ---

    这里是人格行为指南，会作为 system prompt 的一部分注入给模型。
    留空表示不注入额外行为（保持通用助手）。
"""

from __future__ import annotations

import builtins
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_PERSONAS_DIR = Path(__file__).resolve().parent

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 落盘上限：body 每轮进 system prompt（engine._load_persona_text 只取 body），
# 无上限会把 token 预算撑爆；description 只用于 /persona list 展示，宽松封顶。
# persona 属管理员显式配置（等同 .env/config.yaml 的受信内容，不过 safety 围栏），
# 这两个上限防的是误操作导入巨型文件，不是防攻击。
_BODY_MAX = 20000
_DESCRIPTION_MAX = 500


def render_persona_md(
    name: str, description: str = "", body: str = "", default: bool = False
) -> str:
    """渲染 persona md 全文（frontmatter + 正文）；校验失败抛 ``ValueError``。

    web 写面的两段确认用它生成待写入文本做 HMAC 绑定——确认码绑定的就是
    最终落盘内容，与 ``save`` 共用同一渲染函数，不会两头漂移。
    """
    name = (name or "").strip()
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid persona name: {name!r}")
    body = body or ""
    description = description or ""
    if len(body) > _BODY_MAX:
        raise ValueError(f"persona body too long: {len(body)} > {_BODY_MAX}")
    if len(description) > _DESCRIPTION_MAX:
        raise ValueError(
            f"persona description too long: {len(description)} > {_DESCRIPTION_MAX}"
        )
    meta = {"name": name, "description": description, "default": bool(default)}
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{head}\n---\n\n{body.strip()}\n"


@dataclass
class Persona:
    name: str
    description: str = ""
    body: str = ""
    default: bool = False
    file: str = ""  # 绝对路径，便于展示


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """解析 md 的 YAML frontmatter（--- 开头）。无 frontmatter 返回 (None, 全文)。"""
    if not text.startswith("---"):
        return None, text
    lines = text.splitlines()
    if len(lines) < 2:
        return None, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except Exception:
        logger.warning("persona frontmatter parse failed in %s", text[:60])
        return None, text
    body = "\n".join(lines[end + 1 :]).strip()
    return (meta if isinstance(meta, dict) else {}), body


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
        # 重新扫描的 TTL：热新增 md 后最多此秒数内可见，避免每轮对话都读盘
        self._ttl = 2.0
        # 已报过的故障 key（_warn_once 用）；refresh() 时清空
        self._warned: set[tuple[str, str]] = set()

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

        ``_scan`` 被 ``_ensure_loaded`` 每 2 秒重跑一次（热新增 md 的 TTL），
        一个坏文件会把它那条 WARNING 反复刷出来，把真正的告警埋掉。按 key 去重，
        新出现的故障仍会被报（key 不同）。
        """
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(msg, *args)

    def _scan(self) -> dict[str, Persona]:
        personas: dict[str, Persona] = {}
        if not self.dir.is_dir():
            self._warn_once(
                ("dir", str(self.dir)), "personas dir not found: %s", self.dir
            )
            return personas
        for md in sorted(self.dir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                self._warn_once(("read", md.name), "cannot read persona file %s", md)
                continue
            meta, body = _split_frontmatter(text)
            if not meta or not meta.get("name"):
                self._warn_once(
                    ("noname", md.name),
                    "persona file %s missing name in frontmatter, skipped",
                    md.name,
                )
                continue
            name = str(meta["name"])
            if not _NAME_RE.match(name):
                self._warn_once(
                    ("badname", md.name), "persona name %r invalid, skipped", name
                )
                continue
            personas[name] = Persona(
                name=name,
                description=str(meta.get("description") or ""),
                body=body,
                default=bool(meta.get("default", False)),
                file=str(md),
            )
        return personas

    def list(self) -> builtins.list[Persona]:
        self._ensure_loaded()
        personas = self._by_name
        if personas is None:  # _ensure_loaded 保证非 None；防御性兜底
            return []
        return sorted(personas.values(), key=lambda p: (not p.default, p.name))

    def get(self, name: str) -> Persona | None:
        if not _NAME_RE.match(name or ""):
            return None
        self._ensure_loaded()
        personas = self._by_name
        return personas.get(name) if personas is not None else None

    def default(self) -> Persona | None:
        # 本地覆盖优先：PERSONAS_DEFAULT 指定的人格（写在 .env，不入库）——
        # 让「本机用某个人格当默认」与「仓库内置 md 的 default」解耦，
        # 私有 personas（.gitignore）不必改仓库里的 assistant.md
        env_name = (os.getenv("PERSONAS_DEFAULT") or "").strip()
        if env_name:
            p = self.get(env_name)
            if p is not None:
                return p
            # L18（REVIEW-2ed7c63..1b16fa7）：default() 每轮对话都可能被调，
            #  PERSONAS_DEFAULT 配错时这条会每轮刷一次。同一个错名只报一次。
            self._warn_once(
                ("default", env_name),
                "PERSONAS_DEFAULT %r not found; falling back to md default",
                env_name,
            )
        for p in self.list():
            if p.default:
                return p
        return None

    def save(
        self, name: str, description: str = "", body: str = "", default: bool = False
    ) -> Persona:
        """把一个人格写入目录（原子替换 + 0600），同名覆盖；写完立即刷新缓存。

        校验失败抛 ``ValueError``（调用方转 4xx），IO 失败原样抛 ``OSError``。
        ``default`` 只写本文件的 frontmatter，不清理其它文件的 default——
        多 default 时 ``default()`` 取名称排序第一个（保持既有语义）。
        """
        name = (name or "").strip()
        text = render_persona_md(name, description, body, default)
        self.dir.mkdir(parents=True, exist_ok=True)
        # 原子替换：半截文件不会留在目录里被 _scan 读成坏人格（name 已过
        # _NAME_RE，tmp 名不含路径字符）
        tmp = self.dir / f".{name}.{os.getpid()}.tmp"
        try:
            # 上轮遗留 M2：open(,"w") 按 umask 落 0644、写完才 chmod 0600——
            # 窗口期内人格正文同机其他本地用户可读。改 os.open 显式 0600
            # 一步到位（umask 只能去位），O_NOFOLLOW 防符号链接替换。
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o600)  # tmp 已存在时保留历史 mode 的兜底
            os.replace(tmp, self.dir / f"{name}.md")
        finally:
            tmp.unlink(missing_ok=True)
        self.refresh()
        persona = self.get(name)
        if persona is None:  # refresh 后仍读不到 = 落盘没成功，别让调用方以为写好了
            raise OSError(f"persona {name!r} not readable after save")
        return persona

    def delete(self, name: str) -> bool:
        """删除一个人格文件；不存在返回 False。校验失败抛 ``ValueError``。"""
        name = (name or "").strip()
        if not _NAME_RE.match(name or ""):
            raise ValueError(f"invalid persona name: {name!r}")
        path = self.dir / f"{name}.md"
        if not path.is_file():
            return False
        path.unlink()
        self.refresh()
        return True


def load_manager() -> PersonaManager:
    return PersonaManager()
