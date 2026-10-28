"""scripts/compose_init.py 的单测：compose 项目目录一键引导。

覆盖密码生成、模板改写、以及三种目标态（全新 / 已填好 / Docker bind mount
误建的目录）。按仓库纪律：每条断言都要能通过「改坏实现 → 必失败」的变异复核
（见文件末注释）。

Windows 兼容：全部用 tmp_path + 文本读写，不断言权限位（本机无 POSIX 权限位语义）。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_compose_init():
    spec = importlib.util.spec_from_file_location(
        "compose_init_script", ROOT / "scripts" / "compose_init.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ci = _load_compose_init()

# 与真实 .env.example 同构的最小模板：只留本模块会改写的行 + 几条无关行。
# **刻意建模真实模板的重复键形态**——.env.example 里这 7 个状态键每一个都至少出现
# 两次（容器部署块的注释行 + 功能文档里的活跃行），早期版本只「取消注释」不去重，
# 被靠后的空值行按 dotenv 的后者覆盖语义静默抵消，真机上一次都没报错。
TEMPLATE = """\
# 身份
SUPERUSERS=
ALLOWED_GROUPS=
LLM_BASE_URL=https://api.stepfun.com/v1
LLM_API_KEY=...
LLM_MODEL=step-3.7-flash
POSTGRES_PASSWORD=qqagent
AGENT_BACKUP_MIRROR_DIR=
# AGENT_LLM_PROVIDERS_FILE=data/state/llm-providers.json
# AGENT_SEARCH_PROVIDERS_FILE=data/state/search-providers.json
# AGENT_2FA_SECRET_FILE=data/state/2fa-secret.json
# AGENT_NUDGE_CONFIG=data/state/nudge.json
# AGENT_NUDGE_HOTSPOT_FILE=data/state/nudge-hotspot.json
# AGENT_REBOOT_NOTICE_FILE=data/state/reboot-notice.json
# AGENT_PUSH_STATE_FILE=data/state/push-state.json
# 无关行不应被动
SOME_OTHER_KEY=keep-me

# ---- 功能文档段的同名活跃行（真实模板就有，必须被去重）----
AGENT_LLM_PROVIDERS_FILE=
AGENT_2FA_SECRET_FILE=
AGENT_NUDGE_CONFIG=data/nudge.json
AGENT_NUDGE_HOTSPOT_FILE=data/nudge-hotspot.json
# AGENT_PUSH_STATE_FILE=data/push-state.json
"""


def _write_template(tmp_path: Path) -> Path:
    p = tmp_path / "env.template"
    p.write_text(TEMPLATE, encoding="utf-8")
    return p


class TestGeneratePassword:
    def test_length_and_charset(self):
        pw = ci.generate_password()
        assert len(pw) == 24
        # compose 插值会吃掉单个 $ 后名字；DATABASE_URL 是裸拼接不编码 @
        assert re.fullmatch(r"[A-Za-z0-9]{24}", pw), pw
        assert "$" not in pw and "@" not in pw and "/" not in pw

    def test_not_constant(self):
        assert ci.generate_password() != ci.generate_password()


class TestRenderEnv:
    def test_replaces_db_password(self, tmp_path):
        out = ci.render_env(TEMPLATE, "Ab3xyzEXAMPLEpassword9")
        assert "POSTGRES_PASSWORD=Ab3xyzEXAMPLEpassword9\n" in out
        assert "POSTGRES_PASSWORD=qqagent" not in out

    def test_uncomments_exactly_the_seven_state_lines(self):
        out = ci.render_env(TEMPLATE, "pw12345678901234567890ab")
        for key in ci.STATE_REDIRECT_KEYS:
            assert re.search(rf"^{key}=data/state/", out, re.M), key
        # 不能再剩下被注释的同名行（否则两行并存，dotenv 取后者行为不定）
        assert not re.search(r"^#\s*AGENT_LLM_PROVIDERS_FILE=", out, re.M)
        assert out.count("AGENT_LLM_PROVIDERS_FILE=") == 1

    def test_state_keys_appear_exactly_once_each(self):
        """每个状态键只能有一行——dotenv 后者覆盖前者，重复即静默抵消。"""
        out = ci.render_env(TEMPLATE, "pw12345678901234567890ab")
        for key, value in ci.STATE_REDIRECTS.items():
            assert out.count(f"{key}=") == 1, key
            assert re.search(rf"^{key}={re.escape(value)}$", out, re.M), key

    def test_drops_active_duplicate_rows(self):
        """靠后的活跃行（真实模板 70/139/518/523 行那种）必须被去掉。"""
        out = ci.render_env(TEMPLATE, "pw12345678901234567890ab")
        assert "AGENT_LLM_PROVIDERS_FILE=\n" not in out
        assert "AGENT_2FA_SECRET_FILE=\n" not in out
        assert "AGENT_NUDGE_CONFIG=data/nudge.json\n" not in out
        assert "AGENT_NUDGE_HOTSPOT_FILE=data/nudge-hotspot.json\n" not in out

    def test_sets_backup_mirror_default(self):
        out = ci.render_env(TEMPLATE, "pw12345678901234567890ab")
        assert "AGENT_BACKUP_MIRROR_DIR=data/mirror\n" in out

    def test_leaves_everything_else_untouched(self, tmp_path):
        out = ci.render_env(TEMPLATE, "pw12345678901234567890ab")
        assert "SUPERUSERS=\n" in out
        assert "LLM_API_KEY=...\n" in out
        assert "SOME_OTHER_KEY=keep-me\n" in out
        assert "LLM_BASE_URL=https://api.stepfun.com/v1\n" in out

    def test_idempotent(self, tmp_path):
        once = ci.render_env(TEMPLATE, "pw12345678901234567890ab")
        twice = ci.render_env(once, "pw12345678901234567890ab")
        assert once == twice


class TestMissingKeys:
    def test_lists_placeholder_and_empty(self, tmp_path):
        missing = ci.missing_keys(TEMPLATE)
        assert set(missing) == {
            "SUPERUSERS",
            "ALLOWED_GROUPS",
            "LLM_API_KEY",
            "ONEBOT_ONEV11_ACCESS_TOKEN",
            "NAPCAT_HTTP_URL",
        }

    def test_filled_keys_drop_out(self, tmp_path):
        text = TEMPLATE.replace("SUPERUSERS=", "SUPERUSERS=12345")
        assert "SUPERUSERS" not in ci.missing_keys(text)


class TestInitProject:
    def test_fresh_dir_creates_everything(self, tmp_path):
        tpl = _write_template(tmp_path)
        src = tmp_path / "src-config.yaml"
        src.write_text("agent:\n  max_iterations: 8\n", encoding="utf-8")
        out = tmp_path / "out"

        report = ci.init_project(out, template=tpl, source_config=src)

        env = (out / ".env").read_text(encoding="utf-8")
        assert "POSTGRES_PASSWORD=" in env and "qqagent" not in env
        assert re.search(r"^AGENT_PUSH_STATE_FILE=data/state/", env, re.M)
        assert (out / "config.yaml").read_text(encoding="utf-8") == src.read_text(
            encoding="utf-8"
        )
        assert sorted(p.name for p in (out / "data").iterdir()) == sorted(ci.DATA_DIRS)
        assert report["env"] == "created"

    def test_existing_env_is_never_overwritten(self, tmp_path):
        tpl = _write_template(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        secret = out / ".env"
        secret.write_text("LLM_API_KEY=sk-real-do-not-touch\n", encoding="utf-8")
        before = secret.read_bytes()

        report = ci.init_project(out, template=tpl, source_config=tpl)

        assert secret.read_bytes() == before
        assert report["env"] == "kept"
        assert "LLM_API_KEY=sk-real-do-not-touch" in secret.read_text(encoding="utf-8")

    def test_docker_created_env_directory_is_replaced(self, tmp_path):
        """bind mount 指向不存在的文件时 docker 建的是**目录**，必须换回文件。"""
        tpl = _write_template(tmp_path)
        out = tmp_path / "out"
        (out / ".env").mkdir(parents=True)

        report = ci.init_project(out, template=tpl, source_config=tpl)

        assert (out / ".env").is_file()
        assert report["env"] == "created"

    def test_docker_created_config_directory_is_replaced(self, tmp_path):
        tpl = _write_template(tmp_path)
        out = tmp_path / "out"
        (out / "config.yaml").mkdir(parents=True)

        report = ci.init_project(out, template=tpl, source_config=tpl)

        assert (out / "config.yaml").is_file()
        assert report["config"] == "created"

    def test_existing_config_is_kept(self, tmp_path):
        tpl = _write_template(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        mine = out / "config.yaml"
        mine.write_text("agent:\n  max_iterations: 3\n", encoding="utf-8")

        report = ci.init_project(out, template=tpl, source_config=tpl)

        assert "max_iterations: 3" in mine.read_text(encoding="utf-8")
        assert report["config"] == "kept"

    def test_idempotent_second_run_changes_nothing(self, tmp_path):
        tpl = _write_template(tmp_path)
        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"

        ci.init_project(out, template=tpl, source_config=src)
        first = (out / ".env").read_bytes()
        second_report = ci.init_project(out, template=tpl, source_config=src)

        assert (out / ".env").read_bytes() == first
        assert second_report["env"] == "kept"
        assert second_report["config"] == "kept"


class TestRealTemplate:
    """看守真实 .env.example：本模块依赖的行必须在模板里真实存在。"""

    def test_template_has_all_rewritten_lines(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for key in ci.STATE_REDIRECT_KEYS:
            assert re.search(rf"^#\s*{key}=data/state/", text, re.M), (
                f".env.example 少了 {key} 的注释行，compose_init 无处可取消注释"
            )
        assert re.search(r"^POSTGRES_PASSWORD=", text, re.M)
        assert re.search(r"^AGENT_BACKUP_MIRROR_DIR=", text, re.M)

    def test_real_template_yields_one_row_per_state_key(self):
        """真模板跑一遍：7 个键各只有一行且值正确（重复键会被后者覆盖）。"""
        out = ci.render_env(
            (ROOT / ".env.example").read_text(encoding="utf-8"),
            "pw12345678901234567890ab",
        )
        for key, value in ci.STATE_REDIRECTS.items():
            assert out.count(f"{key}=") == 1, f"{key} 在产物里不是一行"
            assert re.search(rf"^{key}={re.escape(value)}$", out, re.M), key
        # 密码与镜像目录各一行
        assert out.count("POSTGRES_PASSWORD=") == 1
        assert "POSTGRES_PASSWORD=pw12345678901234567890ab\n" in out
        assert out.count("AGENT_BACKUP_MIRROR_DIR=") == 1

    def test_real_template_keeps_every_other_key(self):
        """除目标行外一行不丢：改写是点对点的，不是重建文件。"""
        src = (ROOT / ".env.example").read_text(encoding="utf-8")
        out = ci.render_env(src, "pw12345678901234567890ab")
        rewritten = set(ci.STATE_REDIRECTS) | {
            "POSTGRES_PASSWORD",
            "AGENT_BACKUP_MIRROR_DIR",
        }
        dropped = [
            ln
            for ln in src.splitlines()
            if ln.strip()
            and ln not in out.splitlines()
            and ci._key_of(ln) not in rewritten
        ]
        assert dropped == [], f"误删了非目标行：{dropped[:5]}"

    def test_config_yaml_exists_in_repo(self):
        assert (ROOT / "config.yaml").is_file()


# ---- 变异复核（2026-10-28）----
# 实现过程中真被抓到的一个：初版 render_env 只「取消注释」那 7 行、不去重，而
# .env.example 里这 7 个键每一个都至少出现两次（容器部署块的注释行 + 功能文档里的
# 活跃行）。python-dotenv 是后者覆盖前者，靠后的空值/旧值行把重定向**静默抵消**——
# 真机不报错，只是 recreate 后 provider/key、2FA secret、nudge 记录照丢。补了
# test_state_keys_appear_exactly_once_each / test_drops_active_duplicate_rows /
# test_real_template_yields_one_row_per_state_key 三条才守住。
# 其余逐条对应：
# 1. render_env 不替换 POSTGRES_PASSWORD → test_replaces_db_password 红
# 2. 密码生成器加入 @ 或 $ → test_length_and_charset 红
# 3. init_project 覆盖已存在 .env → test_existing_env_is_never_overwritten 红
# 4. init_project 不处理目录态 → test_docker_created_env_directory_is_replaced 红
# 5. 改写顺手动无关行 → test_leaves_everything_else_untouched / 真模板版 红
# 6. missing_keys 不识别 ... 占位 → test_lists_placeholder_and_empty 红
# 7. 真实模板删掉某条 state 行 → TestRealTemplate 红（防模板漂移）
