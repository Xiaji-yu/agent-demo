"""评审/修复产物的归档完整性与文档计数自检。

用法::

    python scripts/check_review_docs.py                  # 结构自检（秒级）
    python scripts/check_review_docs.py --check-stats    # 追加实跑 pytest 核对 BACKLOG 计数
    python scripts/check_review_docs.py --write-stats    # 用当前实测重写 BACKLOG 计数块

机械执行 ``review/REVIEW-WORKFLOW.md`` 的硬性约定，抓四类历史上以 M 级复发的问题：

* **索引断裂（M14 类）**：``review/`` 下的 REVIEW/FIX 产物未登记进 WORKFLOW §9/§9.1
  索引——增量评审链入口断裂，已在 4 轮评审里重复出现（M4/M14/L3 补录）；
* **产物散落（§7 类）**：REVIEW/FIX/AC 文档出现在 ``review/`` 之外（早期四份
  FIX 曾在仓库根目录，靠人工 ``git ls-files`` 自检，没有闸门）；
* **索引断链**：WORKFLOW 正文/索引引用的产物文件名在磁盘上不存在；
* **计数漂移（M13 类）**：BACKLOG 头部的测试计数与 ``pytest -q`` 实测不符
  （手写估算；已多次以「记账失真」立案）；
* **发布漏记（M16 类）**：最新一个版本的 CHANGELOG 段没有覆盖该版本引入的
  **用户可指名新面**——``.env.example`` 新增且已进 ``settings_spec.ENV_SPECS``
  的 env 键、``plugins/`` 新增的 ``on_command`` 命令名。1.0.5 段曾一次漏 6 组
  （同型第三次复发），判据刻意收窄到这两类，避免把部署脚本类 env 算成误报。

``--check-stats`` 自行跑一遍**默认套件**（环境里剥离 ``TEST_DATABASE_URL``，
与 BACKLOG 记录的口径对齐）；PG / 错位时区数字仍为人工维护并在 BACKLOG 该段
注明实测日期。退出码：0 = 通过，1 = 检出问题。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
REVIEW_DIR = REPO_ROOT / "review"
WORKFLOW = REVIEW_DIR / "REVIEW-WORKFLOW.md"
BACKLOG = REPO_ROOT / "BACKLOG.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"

# BACKLOG 头部的机器生成计数块（``--write-stats`` 只重写两标记之间的内容）
STATS_BEGIN_RE = re.compile(
    r"<!-- DOCS-STATS:BEGIN.*?-->.*?<!-- DOCS-STATS:END -->", re.S
)
STATS_PARSE_RE = re.compile(
    r"测试 (\d+) 个文件（\*\*(\d+) 收集：(\d+) 通过 \+ (\d+) 跳过\*\*——`pytest -q` 实测 (\S+?)）"
)
# os.walk 剪枝：这些目录里的同名文件不是评审产物
_PRUNE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "data",
    "node_modules",
}
_ARTIFACT_RE = re.compile(r"^(REVIEW-|FIX-|AC-).*\.md$")


def check_index() -> list[str]:
    """REVIEW/FIX 产物必须登记进 WORKFLOW 索引；索引引用不得断链。"""
    problems: list[str] = []
    if not WORKFLOW.is_file():
        return [f"缺少 {WORKFLOW.relative_to(REPO_ROOT)}（评审规范本体）"]
    text = WORKFLOW.read_text(encoding="utf-8")
    for path in sorted(REVIEW_DIR.glob("*.md")):
        if path.name == "REVIEW-WORKFLOW.md" or not _ARTIFACT_RE.match(path.name):
            continue
        if path.name not in text:
            problems.append(
                f"索引断裂（M14 类）：{path.name} 未登记进 REVIEW-WORKFLOW.md 的 §9/§9.1 索引"
            )
    for ref in sorted(set(re.findall(r"\]\(([^)]+\.md)\)", text))):
        if (
            ref.startswith(("http://", "https://"))
            or "*" in ref
            or "<" in ref
            or "x..y" in ref
        ):
            continue  # 外链、命名模板（REVIEW-<start8>..<end8>.md）与占位示例（REVIEW-x..y.md）
        if not (REVIEW_DIR / ref).is_file():
            problems.append(f"索引断链：REVIEW-WORKFLOW.md 引用 {ref} 但文件不存在")
    return problems


def check_placement() -> list[str]:
    """REVIEW/FIX/AC 产物只能位于 review/（WORKFLOW §7 硬性约束）。"""
    problems: list[str] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        if Path(dirpath).resolve() == REVIEW_DIR.resolve():
            continue
        for name in filenames:
            if _ARTIFACT_RE.match(name):
                rel = (Path(dirpath) / name).relative_to(REPO_ROOT)
                problems.append(
                    f"产物散落（§7 类）：{rel} 不在 review/ 内——评审/修复文档一律归档 review/"
                )
    return problems


def check_doc_map() -> list[str]:
    """AGENTS.md §6 文档地图引用的文件必须存在（防文档自身断链）。"""
    if not AGENTS_MD.is_file():
        return []
    text = AGENTS_MD.read_text(encoding="utf-8")
    if "## 6. 文档地图" not in text:
        return ["AGENTS.md 缺少 §6 文档地图一节"]
    section = text.split("## 6. 文档地图", 1)[1].split("\n## ", 1)[0]
    problems: list[str] = []
    for token in re.findall(r"`([^`]+\.md)`", section):
        if "*" in token or token.startswith(("http://", "https://")):
            continue
        if not (REPO_ROOT / token).is_file():
            problems.append(f"AGENTS.md §6 文档地图引用了不存在的文件：{token}")
    return problems


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    return proc.stdout if proc.returncode == 0 else ""


def _changelog_sections() -> dict[str, str]:
    """``{版本号: 该段正文}``；只认 ``## [x.y.z] - YYYY-MM-DD`` 形态的段。

    段正文裁到**下一个段标题**为止（M8，REVIEW-6867995..e079b8d）：此前取
    ``text[m.start():]`` 到文件尾，「该版本段没提到键名」实际是「从最新段起
    全文都没提到」——键名在任一历史段出现过（补记/迁移说明/移除后重引入）
    就静默放过，判据被历史段污染（漏报方向）。
    """
    if not CHANGELOG.is_file():
        return {}
    text = CHANGELOG.read_text(encoding="utf-8")
    matches = list(
        re.finditer(r"^## \[([\d.]+[^\]]*)\] - \d{4}-\d{2}-\d{2}", text, re.M)
    )
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[m.group(1)] = text[m.start() : end]
    return sections


def _settings_env_keys() -> set[str]:
    """从 ``settings_spec.py`` 源码里取 ENV_SPECS 的键名集合。

    **刻意不 import**：那会拖进 nonebot 与整个插件包（本脚本是秒级 lint，
    不该为了一条判据初始化 driver）；ENV_SPECS 的条目形态固定（``EnvSpec(``
    后第一个位置参数就是键名），正则足够稳。取不到就返回空集——调用方据此
    跳过并留痕，不静默通过。
    """
    path = REPO_ROOT / "plugins" / "qq_agent_adapter" / "settings_spec.py"
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8")
    start = text.find("ENV_SPECS")
    if start < 0:
        return set()
    return set(re.findall(r'EnvSpec\(\s*\n?\s*"([A-Z][A-Z0-9_]+)"', text[start:]))


def check_changelog() -> list[str]:
    """M16：最新发布段的 CHANGELOG 必须覆盖该版本引入的用户可指名新面。

    机械判据（刻意收窄，低误报）：

    * 范围只取**最新一个**有段 tag——发布时正在写的正是这一段；历史段不追溯
      （已发布的段落改写没有收益，还会把门禁变成考古）；
    * env 键只算 ``.env.example`` 里新增、且**当前仍在 ``settings_spec.ENV_SPECS``**
      里的——那是 web 设置面的单一 schema，也就是「用户能指名的配置」；
      compose/deploy 脚本类的 env（NAPCAT_HTTP_URL、FILES、MOUNTS…）不算，
      它们不是 bot 的功能面；
    * 命令只算 ``plugins/`` 下新增的 ``on_command("<名字>"`` 注册。

    取不到 tag（浅克隆 / 无 tag）或取不到 ENV_SPECS 时**跳过**并返回空——
    门禁不能因为环境不全而误伤 CI。
    """
    tags = [
        t for t in _git("tag", "--sort=-creatordate").split() if re.match(r"^v\d", t)
    ]
    if len(tags) < 2:
        return []  # 至少要有个「上一个 tag」才能算 diff
    sections = _changelog_sections()
    tag = tags[0]
    version = tag[1:]
    if version not in sections:
        return []  # 这个 tag 还没写 CHANGELOG 段（发布流程的第二步），不归本检查管
    spec_keys = _settings_env_keys()
    if not spec_keys:
        print("[skip] 发布漏记检查：取不到 settings_spec 的 env 键清单，本次跳过")
        return []

    body = sections[version]
    # env 键看全仓 diff（.env.example 在仓库根）；命令只扫 plugins/——on_command
    # 是插件面 API，且这样能把 scripts/ 里文档字符串提到的示例挡在外面
    diff = _git("diff", f"{tags[1]}..{tag}")
    if not diff:
        return []
    env_added = sorted(set(re.findall(r"^\+\s*([A-Z][A-Z0-9_]{3,})=", diff, re.M)))
    user_env = [k for k in env_added if k in spec_keys and k not in body]
    plugin_diff = _git("diff", f"{tags[1]}..{tag}", "--", "plugins/")
    # 命令名必须是「小写字母开头的标识符」：注释/文档字符串里的 on_command("<名字>")
    # 这类占位示例天然被滤掉（实测踩过：本函数自己的 docstring 就误报过一次）
    cmds_added = sorted(
        {
            name
            for name in re.findall(r'on_command\(\s*"([^"]+)"', plugin_diff)
            if re.match(r"^[a-z][a-z0-9_]{0,20}$", name)
        }
    )
    missing_cmds = [c for c in cmds_added if c not in body]
    problems = []
    if user_env:
        problems.append(
            f"发布漏记（M16 类）：{tag} 的 CHANGELOG 段没提到新增的用户面 env 键 "
            + ", ".join(user_env)
            + "——升级者看不到新开关"
        )
    if missing_cmds:
        problems.append(
            f"发布漏记（M16 类）：{tag} 的 CHANGELOG 段没提到新增命令 "
            + ", ".join(f"/{c}" for c in missing_cmds)
        )
    return problems


def run_pytest_summary() -> tuple[int, int, int, int]:
    """跑默认套件，返回 (测试文件数, 收集数, 通过数, 跳过数)。"""
    env = {k: v for k, v in os.environ.items() if k != "TEST_DATABASE_URL"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    passed = re.search(r"(\d+) passed", tail)
    if proc.returncode != 0 or not passed:
        raise RuntimeError(
            f"pytest 未通过或输出无法解析：{tail!r}\n{proc.stderr[-800:]}"
        )
    skipped = re.search(r"(\d+) skipped", tail)
    files = len(list((REPO_ROOT / "tests").glob("test_*.py")))
    n_passed = int(passed.group(1))
    n_skipped = int(skipped.group(1)) if skipped else 0
    return files, n_passed + n_skipped, n_passed, n_skipped


def _render_stats(files: int, collected: int, passed: int, skipped: int) -> str:
    date = time.strftime("%Y-%m-%d")
    return (
        f"测试 {files} 个文件（**{collected} 收集：{passed} 通过 + {skipped} 跳过**"
        f"——`pytest -q` 实测 {date}）"
    )


def check_stats() -> tuple[list[str], str]:
    """核对 BACKLOG 计数块与实测；返回 (问题列表, 当前块内容或缺失提示)。"""
    if not BACKLOG.is_file():
        return [f"缺少 {BACKLOG.name}"], ""
    text = BACKLOG.read_text(encoding="utf-8")
    match = STATS_BEGIN_RE.search(text)
    if not match:
        return [
            "BACKLOG 头部缺少 DOCS-STATS 计数块（标记 <!-- DOCS-STATS:BEGIN -->；"
            "先跑一次 --write-stats 生成）",
        ], ""
    block = match.group(0)
    parsed = STATS_PARSE_RE.search(block)
    if not parsed:
        return [f"BACKLOG 计数块格式无法解析：{block!r}"], block
    files, collected, passed, skipped = run_pytest_summary()
    doc = tuple(int(g) for g in parsed.groups()[:4])
    if doc != (files, collected, passed, skipped):
        return [
            "计数漂移（M13 类）：BACKLOG 记录 "
            f"{doc[0]} 文件 / {doc[1]} 收集：{doc[2]} 通过 + {doc[3]} 跳过，"
            f"实测 {files} / {collected}：{passed} 通过 + {skipped} 跳过"
            "——跑 scripts/check_review_docs.py --write-stats 重新生成，勿手改",
        ], block
    return [], block


def write_stats() -> tuple[int, str]:
    """重写 BACKLOG 计数块，返回 (退出码, 新统计句)。"""
    text = BACKLOG.read_text(encoding="utf-8")
    match = STATS_BEGIN_RE.search(text)
    if not match:
        return 1, "BACKLOG 头部缺少 DOCS-STATS 计数块标记，无法定位重写位置"
    files, collected, passed, skipped = run_pytest_summary()
    rendered = _render_stats(files, collected, passed, skipped)
    new_block, replaced = STATS_PARSE_RE.subn(rendered, match.group(0), count=1)
    if replaced != 1:
        return 1, f"计数块内未找到可替换的统计句：{match.group(0)!r}"
    BACKLOG.write_text(
        text[: match.start()] + new_block + text[match.end() :], encoding="utf-8"
    )
    return 0, rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评审产物归档完整性与文档计数自检")
    parser.add_argument(
        "--check-stats",
        action="store_true",
        help="追加：实跑默认套件核对 BACKLOG 计数块",
    )
    parser.add_argument(
        "--write-stats", action="store_true", help="用当前实测重写 BACKLOG 计数块"
    )
    args = parser.parse_args(argv)

    problems = check_index() + check_placement() + check_doc_map() + check_changelog()
    for line in problems:
        print(f"[FAIL] {line}")
    if not problems:
        print("[ok] 索引完整 / 产物均在 review/ / 文档地图无断链")

    stats_problems: list[str] = []
    if args.write_stats:
        try:
            code, rendered = write_stats()
        except RuntimeError as exc:
            print(f"[FAIL] {exc}")
            return 1
        if code != 0:
            print(f"[FAIL] {rendered}")
            return 1
        print(f"[ok] 已重写 BACKLOG 计数块：{rendered}")
    elif args.check_stats:
        try:
            stats_problems, _block = check_stats()
        except RuntimeError as exc:
            print(f"[FAIL] {exc}")
            return 1
        for line in stats_problems:
            print(f"[FAIL] {line}")
        if not stats_problems:
            print("[ok] BACKLOG 计数与 pytest 实测一致")

    return 1 if problems or stats_problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
