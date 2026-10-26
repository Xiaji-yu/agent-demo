"""帮助菜单的单一事实源 + 全命令面覆盖。

历史教训（2026-10-26）：文本菜单（admin._HELP_TEXT）与图片菜单
（help_render._SECTIONS）各写一份，图片菜单漏过 /词云、/发言榜、/memory、
/2fa 等一整批命令——用户问「词云怎么调用」而帮助里根本没有。现在两边都从
`help_menu.SECTIONS` 生成，并且这里用 AST 扫全部 `on_command` 注册面：
**新命令不进帮助菜单，这条用例就红**。
"""

import ast
import importlib
from pathlib import Path

import pytest

pytest.importorskip("nonebot")

_ADAPTER_DIR = Path(__file__).resolve().parent.parent / "plugins" / "qq_agent_adapter"


@pytest.fixture(scope="module")
def nb_driver():
    import nonebot

    nonebot.init(_env_file=None, superusers={"10000"})
    from nonebot import get_driver

    return get_driver()


def _registered_commands() -> dict[str, set[str]]:
    """AST 扫适配层全部 on_command：{主名: {主名, *aliases}}。"""
    out: dict[str, set[str]] = {}
    for path in sorted(_ADAPTER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "on_command"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                continue
            primary = str(node.args[0].value)
            names = {primary}
            for kw in node.keywords:
                if kw.arg == "aliases" and isinstance(kw.value, ast.Set):
                    for elt in kw.value.elts:
                        if isinstance(elt, ast.Constant):
                            names.add(str(elt.value))
            out[primary] = names
    return out


class TestHelpMenuSingleSource:
    @pytest.mark.usefixtures("nb_driver")
    def test_text_and_image_share_one_source(self):
        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        help_render = importlib.import_module("plugins.qq_agent_adapter.help_render")
        help_menu = importlib.import_module("plugins.qq_agent_adapter.help_menu")
        assert help_render._SECTIONS is help_menu.SECTIONS
        assert help_render._FOOTER is help_menu.FOOTER
        assert admin._HELP_TEXT == help_menu.render_text()

    def test_text_covers_every_menu_row(self):
        from plugins.qq_agent_adapter import help_menu

        text = help_menu.render_text()
        for head, rows in help_menu.SECTIONS:
            assert head in text
            for cmd, desc in rows:
                assert cmd in text, f"{cmd} 不在文本帮助里"
                assert desc in text, f"{desc} 不在文本帮助里"

    def test_group_stats_commands_listed(self):
        """用户入口：群统计三个入口必须在帮助里（否则只能靠口口相传）。"""
        from plugins.qq_agent_adapter import help_menu

        text = help_menu.render_text()
        # 命令面统一带 / 前缀（2026-10-26 撤掉免前缀入口后）
        for cmd in ("/词云", "/发言榜", "/help"):
            assert cmd in text, f"{cmd} 没进帮助菜单"
        assert "点评我一下" in text

    def test_help_states_prefix_requirement(self):
        """NoneBot 命令必须带 / 前缀（前缀 Trie），帮助不能宣称"带不带都行"。"""
        from plugins.qq_agent_adapter import help_menu

        text = help_menu.render_text()
        assert "带不带 / 都能触发" not in text
        assert "所有命令都带 / 前缀" in text


class TestEveryCommandInHelp:
    @pytest.mark.usefixtures("nb_driver")
    def test_no_command_missing_from_help(self):
        from plugins.qq_agent_adapter import help_menu

        text = help_menu.render_text()
        # 菜单允许紧凑写法（如「skill catalog / info / install …」而不是逐个
        # 「skill catalog / skill info / …」），故按去空白匹配；紧凑族按
        # 「前缀 + 子命令词都出现」判定（help_menu.COMPACT_FAMILIES）。
        squashed = text.replace(" ", "")

        def covered(primary: str, names: set[str]) -> bool:
            if any(name.replace(" ", "") in squashed for name in names):
                return True
            for prefix, subs in help_menu.COMPACT_FAMILIES.items():
                if primary.startswith(prefix) and primary[len(prefix) :] in subs:
                    sub = primary[len(prefix) :]
                    return prefix in squashed and sub in squashed
            return False

        missing = sorted(
            primary
            for primary, names in _registered_commands().items()
            if not covered(primary, names)
        )
        assert not missing, f"这些已注册命令没进帮助菜单：{missing}"

    def test_help_registered_as_help(self):
        """主命令必须是 help（2026-10-26 用户要求）；aihelp 只能退为别名。

        否则会出现"菜单写 /help、实际注册的是 aihelp"的漂移——纯文本包含性
        断言抓不到（别名「帮助」仍在菜单里），必须按 AST 主名判定。
        """
        cmds = _registered_commands()
        assert "help" in cmds, "帮助命令必须注册为 help"
        assert "aihelp" not in cmds, "aihelp 只能是别名，不能是主命令"
        assert {"aihelp", "帮助"} <= cmds["help"]

    def test_command_surface_not_empty(self):
        """防 AST 扫空（ glob 路径错/签名变）导致上面的用例恒真。"""
        cmds = _registered_commands()
        assert len(cmds) >= 15, f"只扫到 {len(cmds)} 个命令，AST 扫描大概率失效"
        assert {"词云", "发言榜", "memory", "2fa", "reboot"} <= set(cmds)
