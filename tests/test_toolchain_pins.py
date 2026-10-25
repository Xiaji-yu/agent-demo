"""工具链 pin 三方一致性守卫（REVIEW-e55cab5..4d5c1b8 M-6）。

ruff 的版本出现在三处：pyproject（dev 依赖 pin）、.pre-commit-config.yaml
（rev）、uv.lock（锁定版本）。97a08af 手工对齐过一次，但没有任何机械守卫——
dependabot bump ruff 时会同步 pyproject 与 uv.lock、**漏掉 pre-commit**，
本地钩子与 CI 规则集漂移（「本地绿 CI 红」）会复发。三条秒级断言钉死。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ruff_pin() -> str:
    m = re.search(
        r'"ruff==([\d.]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert m, "pyproject.toml 必须显式 pin ruff（ruff==x.y.z）"
    return m.group(1)


def test_precommit_rev_matches_pyproject_pin():
    text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    revs = re.findall(r"^\s*rev:\s*v?([\d.]+)\s*$", text, re.MULTILINE)
    assert len(revs) == 1, f"pre-commit 只应有一个带版本 rev 的 hook：{revs}"
    assert revs[0] == _ruff_pin(), "pre-commit rev 必须与 pyproject 的 ruff pin 一致"


def test_uv_lock_matches_pyproject_pin():
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    blocks = [
        b
        for b in re.split(r"(?=\[\[package\]\])", lock)
        if re.search(r'^name = "ruff"$', b, re.MULTILINE)
    ]
    assert len(blocks) == 1, "uv.lock 里 ruff 应只有一个包块"
    m = re.search(r'^version = "([\d.]+)"$', blocks[0], re.MULTILINE)
    assert m, "uv.lock 的 ruff 块必须有 version"
    assert m.group(1) == _ruff_pin(), "uv.lock 的 ruff 版本必须与 pyproject pin 一致"
