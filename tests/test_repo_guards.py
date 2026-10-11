"""仓库级不变量守卫：把 AGENTS.md §4 的高复发铁律变成 CI 可执行断言。

来源：2026-10-11「通用规范评审」的落地项。三条铁律此前只靠文档约定 + 事后评审发现，
同型问题跨轮复发（ssh_run 输出不围栏 → 下一轮 run_shell/log_tail 同类 H；
`.env.example` 缺 env 以 M8/M9 多次复发），本文件把它们前置到测试层。

组织方式（防恒真断言）：每条守卫 = 探测器单测（``tmp_path`` 投毒，**必须检出**）
+ 真实仓库断言（当前应为零命中）。只写后者的话，把探测器改坏也不会红。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTCORE = REPO_ROOT / "agentcore"
PLUGINS = REPO_ROOT / "plugins"

# AGENTS.md §1 唯一既存豁免：agentcore/skills/file_sender.py 对 nonebot 的
# 受保护可选导入（try/except + 降级 None，REVIEW-679c9b3..c472e56 L11）。
LAYERING_EXEMPTIONS = {"agentcore/skills/file_sender.py"}

# BACKLOG §6 已入档的已知死代码：LLMClient.embeddings 无生产调用方，
# LLM_EMBEDDING_MODEL 有意不补文档（记录结论，不补文档）。
ENV_DOC_EXEMPTIONS = {"LLM_EMBEDDING_MODEL"}

# AGENTS.md §4 路由不变量：越界拒绝统一走 acl.deny，不允许各命令自行 finish 掉
_DENY_RE = re.compile(r"无权限|没有权限|权限不足|无权|permission denied", re.I)
# 端口性评审 P3：给用户看的时间戳统一走 agentcore.tz（含显示层）——宿主机 TZ 与
# AGENT_SCHEDULER_TZ 不一致时 localtime 显示与日键对不上账。tz.py 自身豁免。
_LOCALTIME_RE = re.compile(r"time\.localtime\(")
_ENV_READ_RE = re.compile(
    r"os\.(?:environ\.get|getenv)\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
    r"|os\.environ\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
)


def _iter_guarded_imports(tree: ast.AST) -> set[int]:
    """收集位于 try 块内的 import 节点 id（豁免判定用）。"""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import | ast.ImportFrom):
                    guarded.add(id(sub))
    return guarded


def find_nonebot_imports(path: Path) -> list[tuple[int, bool]]:
    """返回该文件里全部 nonebot import 的 (行号, 是否在 try 保护内)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    guarded = _iter_guarded_imports(tree)
    hits: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        if any(m == "nonebot" or m.startswith("nonebot.") for m in modules):
            hits.append((node.lineno, id(node) in guarded))
    return hits


def _literal_str(node: ast.AST) -> str:
    """取字符串字面量/f-string 的静态文本部分（f-string 只取常量段）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return ""


def find_permission_finish(path: Path) -> list[tuple[int, str]]:
    """返回直接 finish(权限拒绝文案) 的 (行号, 文本)——应走 acl.deny。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_finish = (isinstance(func, ast.Attribute) and func.attr == "finish") or (
            isinstance(func, ast.Name) and func.id == "finish"
        )
        if not is_finish:
            continue
        for arg in node.args:
            text = _literal_str(arg)
            if text and _DENY_RE.search(text):
                hits.append((node.lineno, text))
    return hits


def find_localtime_uses(path: Path) -> list[int]:
    """返回文件里 ``time.localtime(`` 出现的行号（显示层应走 agentcore.tz）。"""
    return [
        lineno
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if _LOCALTIME_RE.search(line)
    ]


def find_env_names(root: Path) -> set[str]:
    """生产代码（agentcore/plugins/scripts/bot.py）里读取的字面量 env 名。"""
    names: set[str] = set()
    targets = [root / "agentcore", root / "plugins", root / "scripts"]
    bot = root / "bot.py"
    if bot.is_file():
        targets.append(bot)
    for target in targets:
        if target.is_file():
            paths = [target]
        elif target.is_dir():
            paths = sorted(target.rglob("*.py"))
        else:
            continue
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for m in _ENV_READ_RE.finditer(text):
                names.add(m.group(1) or m.group(2))
    return names


def undocumented_envs(root: Path, doc_text: str) -> set[str]:
    """代码读取但 .env.example 未出现的 env（扣除已入档豁免）。"""
    return {n for n in find_env_names(root) if n not in doc_text} - ENV_DOC_EXEMPTIONS


class TestLayering:
    """agentcore 不得 import nonebot（AGENTS.md §1 分层铁律）。"""

    def test_detector_flags_unguarded_import(self, tmp_path: Path):
        bad = tmp_path / "agentcore" / "x.py"
        bad.parent.mkdir(parents=True)
        bad.write_text("import nonebot\n", encoding="utf-8")
        assert find_nonebot_imports(bad) == [(1, False)]

    def test_detector_allows_try_guarded_import(self, tmp_path: Path):
        good = tmp_path / "agentcore" / "x.py"
        good.parent.mkdir(parents=True)
        good.write_text(
            "try:\n"
            "    from nonebot import get_driver\n"
            "except ImportError:\n"
            "    get_driver = None\n",
            encoding="utf-8",
        )
        assert find_nonebot_imports(good) == [(2, True)]

    def test_real_tree_only_documented_exemption(self):
        violations: list[str] = []
        for path in sorted(AGENTCORE.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            for lineno, guarded in find_nonebot_imports(path):
                if rel not in LAYERING_EXEMPTIONS or not guarded:
                    violations.append(f"{rel}:{lineno}（guarded={guarded}）")
        assert not violations, (
            "agentcore 出现 nonebot 依赖（分层铁律）："
            + "; ".join(violations)
            + "。平台能力请放 plugins/；确需可选导入须走 try/except 降级并登记豁免表"
        )


class TestAclDeny:
    """权限拒绝必须走 acl.deny，不允许插件自行 finish 掉（AGENTS.md §4）。"""

    def test_detector_flags_direct_deny(self, tmp_path: Path):
        bad = tmp_path / "plugins" / "x.py"
        bad.parent.mkdir(parents=True)
        bad.write_text(
            "async def f():\n    await cmd.finish('无权限')\n",
            encoding="utf-8",
        )
        assert find_permission_finish(bad) == [(2, "无权限")]

    def test_detector_flags_fstring_deny(self, tmp_path: Path):
        bad = tmp_path / "plugins" / "x.py"
        bad.parent.mkdir(parents=True)
        bad.write_text(
            'async def f():\n    await cmd.finish(f"没有权限：{who}")\n',
            encoding="utf-8",
        )
        assert [text for _lineno, text in find_permission_finish(bad)] == ["没有权限："]

    def test_real_tree_routes_deny_through_acl(self):
        hits: list[str] = []
        for path in sorted(PLUGINS.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            hits += [
                f"{rel}:{lineno} {text!r}"
                for lineno, text in find_permission_finish(path)
            ]
        assert not hits, (
            "plugins/ 存在直接 finish 的权限拒绝（应走 acl.deny，保证黑名单/日志口径统一）："
            + "; ".join(hits)
        )


class TestUnifiedClock:
    """显示层时间戳必须走 agentcore.tz（端口性评审 P3，防 localtime 复发）。"""

    def test_detector_flags_localtime(self, tmp_path: Path):
        bad = tmp_path / "agentcore" / "x.py"
        bad.parent.mkdir(parents=True)
        bad.write_text(
            "import time\n\n\ndef f(ts):\n"
            "    return time.strftime('%m-%d %H:%M', time.localtime(ts))\n",
            encoding="utf-8",
        )
        assert find_localtime_uses(bad) == [5]

    def test_real_tree_has_no_localtime(self):
        hits: list[str] = []
        for base in (AGENTCORE, PLUGINS, REPO_ROOT / "scripts"):
            for path in sorted(base.rglob("*.py")):
                if path.relative_to(REPO_ROOT).as_posix() == "agentcore/tz.py":
                    continue  # tz.py 自身是实现，豁免
                rel = path.relative_to(REPO_ROOT).as_posix()
                hits += [f"{rel}:{lineno}" for lineno in find_localtime_uses(path)]
        assert not hits, (
            "生产代码仍在用 time.localtime 显示时间（应走 agentcore.tz.fmt_ts，"
            "与日键同时区）：" + "; ".join(hits)
        )


class TestEnvDocs:
    """代码读取的 env 必须出现在 .env.example（M8/M9 复发的文档漂移类）。"""

    def test_detector_flags_undocumented(self, tmp_path: Path):
        pkg = tmp_path / "agentcore"
        pkg.mkdir()
        (pkg / "m.py").write_text(
            'import os\nV = os.getenv("AGENT_TOTALLY_NEW")\n', encoding="utf-8"
        )
        doc = tmp_path / ".env.example"
        doc.write_text("LLM_MODEL=x\n", encoding="utf-8")
        assert undocumented_envs(tmp_path, doc.read_text(encoding="utf-8")) == {
            "AGENT_TOTALLY_NEW"
        }

    def test_detector_passes_when_documented(self, tmp_path: Path):
        pkg = tmp_path / "agentcore"
        pkg.mkdir()
        (pkg / "m.py").write_text(
            'import os\nV = os.getenv("AGENT_TOTALLY_NEW")\n', encoding="utf-8"
        )
        doc = tmp_path / ".env.example"
        doc.write_text("# 注释\nAGENT_TOTALLY_NEW=1\n", encoding="utf-8")
        assert undocumented_envs(tmp_path, doc.read_text(encoding="utf-8")) == set()

    def test_real_tree_env_vars_documented(self):
        doc = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        missing = undocumented_envs(REPO_ROOT, doc)
        assert not missing, (
            f"代码读取的 env 未出现在 .env.example：{sorted(missing)}"
            "——新 env 必须同步补文档（.env.example 是该文件的唯一事实源）；"
            "确为死代码/有意不文档化的请加进 ENV_DOC_EXEMPTIONS 并注明依据"
        )
