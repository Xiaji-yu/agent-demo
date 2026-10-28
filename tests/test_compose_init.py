"""scripts/compose_init.py 的单测：compose 项目目录一键引导。

覆盖纯函数与带副作用的命令（三种目标态：全新 / 已填好 / Docker bind mount 误建的
目录）。按仓库纪律：每条断言都要能通过「改坏实现 → 必失败」的变异复核（见文件末注释）。

Windows 兼容：全部用 tmp_path + 文本读写，不断言权限位（本机无 POSIX 权限位语义）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_compose_init():
    spec = importlib.util.spec_from_file_location(
        "compose_init_script", ROOT / "scripts" / "compose_init.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ci = _load_compose_init()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """本文件会经 config_json.load() 写真实 os.environ，必须逐个清理。

    漏清的后果是跨模块污染：2026-10-28 实测漏掉 7 个状态重定向键后，
    test_nudge.py / test_scheduler.py 共 20 条在**全量**跑时红（单文件跑全绿），
    而它们在隔离运行时完全正常——这类污染只在全套件里现形。
    """
    from agentcore import config_json

    keys = (
        set(config_json.REQUIRED_KEYS)
        | set(ci.STATE_REDIRECTS)
        | {"HOST", "PORT", "AGENT_TEST_JSON_KEY", "AGENT_CONFIG_JSON"}
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in keys:
        os.environ.pop(key, None)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestRenderJson:
    def test_is_valid_json_with_expected_keys(self):
        data = _read_json.__wrapped__ if False else json.loads(ci.render_json())
        for key in (
            "HOST",
            "PORT",
            "SUPERUSERS",
            "ALLOWED_GROUPS",
            "LLM_API_KEY",
            "LLM_MODEL",
            "ONEBOT_ONEV11_ACCESS_TOKEN",
            "NAPCAT_HTTP_URL",
        ):
            assert key in data, key

    def test_host_is_zero_zero_zero_zero(self):
        """容器里必须 0.0.0.0——填 127.0.0.1 协议端 ECONNREFUSED（2026-10-28 实测）。"""
        assert json.loads(ci.render_json())["HOST"] == "0.0.0.0"

    def test_underscore_keys_are_notes(self):
        data = json.loads(ci.render_json())
        notes = [k for k in data if k.startswith("_")]
        assert notes, "没有任何备注键——JSON 没有注释语法，靠 _ 前缀自解释"
        # 真正的契约在 config_json 侧：TestGeneratedJsonIsActuallyLoadable 端到端验
        assert all(isinstance(v, str) for v in (data[k] for k in notes))

    def test_render_json_has_no_leftover_junk(self):
        """只允许字符串值——数字/布尔交给 config_json.to_env_value 转，这里别混用。"""
        data = json.loads(ci.render_json())
        assert all(isinstance(v, str) for v in data.values()), {
            k: v for k, v in data.items() if not isinstance(v, str)
        }

    def test_state_redirects_all_present(self):
        data = json.loads(ci.render_json())
        for key, value in ci.STATE_REDIRECTS.items():
            assert data.get(key) == value, key

    def test_napcat_url_example_is_http_and_port_3000(self):
        """两个易错点：漏 http:// 前缀、把 WebUI 的 6099 当成 HTTP API 端口。"""
        url = ci.NAPCAT_HTTP_URL_EXAMPLE
        assert url.startswith("http://")
        assert url.endswith(":3000")
        # 占位本身要能触发「未填」判定（以「换成」开头）
        placeholder = json.loads(ci.render_json())["NAPCAT_HTTP_URL"]
        assert placeholder.startswith(ci.PLACEHOLDER_PREFIX)
        assert url in placeholder


class TestMissingKeys:
    def test_placeholder_and_empty_count_as_missing(self):
        data = json.loads(ci.render_json())
        assert set(ci.missing_keys(data)) == set(ci.REQUIRED_KEYS)

    def test_filled_keys_drop_out(self):
        data = json.loads(ci.render_json())
        data.update(
            {
                "LLM_API_KEY": "sk-real",
                "SUPERUSERS": "12345",
                "ALLOWED_GROUPS": "111",
                "ONEBOT_ONEV11_ACCESS_TOKEN": "tok",
                "NAPCAT_HTTP_URL": "http://192.168.1.2:3000",
            }
        )
        assert ci.missing_keys(data) == []

    def test_empty_string_counts_as_missing(self):
        # 其余必填键也不在 dict 里，同样算没填
        filled = {k: "x" for k in ci.REQUIRED_KEYS}
        filled["LLM_API_KEY"] = ""
        assert ci.missing_keys(filled) == ["LLM_API_KEY"]

    def test_absent_key_counts_as_missing(self):
        assert ci.missing_keys({}) == list(ci.REQUIRED_KEYS)


class TestInitProject:
    def test_fresh_dir_creates_everything(self, tmp_path):
        src = tmp_path / "src-config.yaml"
        src.write_text("agent:\n  max_iterations: 8\n", encoding="utf-8")
        out = tmp_path / "out"

        report = ci.init_project(out, source_config=src)

        data = _read_json(out / "config" / "agent.json")
        assert data["HOST"] == "0.0.0.0"
        assert data["AGENT_PUSH_STATE_FILE"] == "data/state/push-state.json"
        assert (out / "config.yaml").read_text(encoding="utf-8") == src.read_text(
            encoding="utf-8"
        )
        assert sorted(p.name for p in (out / "data").iterdir()) == sorted(ci.DATA_DIRS)
        assert report == {
            "config": "created",
            "data": "created",
            "json": "created",
            "web_env": "created",
        }

    def test_existing_json_is_never_overwritten(self, tmp_path):
        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"
        (out / "config").mkdir(parents=True)
        secret = out / "config" / "agent.json"
        secret.write_text('{"LLM_API_KEY": "sk-real-do-not-touch"}\n', encoding="utf-8")
        before = secret.read_bytes()

        report = ci.init_project(out, source_config=src)

        assert secret.read_bytes() == before
        assert report["json"] == "kept"
        assert "sk-real-do-not-touch" in secret.read_text(encoding="utf-8")

    def test_docker_created_json_directory_is_replaced(self, tmp_path):
        """目录挂载场景下 Docker 建目录是对的；但 agent.json 是目录时必须换回文件。"""
        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"
        (out / "config" / "agent.json").mkdir(parents=True)

        report = ci.init_project(out, source_config=src)

        assert (out / "config" / "agent.json").is_file()
        assert report["json"] == "created"

    def test_nonempty_directory_trap_aborts_loudly(self, tmp_path):
        """非空目录不替用户决定删什么——必须喊出来而不是静默覆盖。"""
        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"
        trap = out / "config" / "agent.json"
        trap.mkdir(parents=True)
        (trap / "别删我.txt").write_text("x", encoding="utf-8")

        try:
            ci.init_project(out, source_config=src)
        except SystemExit as exc:
            assert "非空目录" in str(exc)
        else:
            raise AssertionError("非空目录应当报错退出，而不是覆盖")

    def test_docker_created_config_directory_is_replaced(self, tmp_path):
        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"
        (out / "config.yaml").mkdir(parents=True)

        report = ci.init_project(out, source_config=src)

        assert (out / "config.yaml").is_file()
        assert report["config"] == "created"

    def test_existing_config_is_kept(self, tmp_path):
        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"
        out.mkdir()
        mine = out / "config.yaml"
        mine.write_text("agent:\n  max_iterations: 3\n", encoding="utf-8")

        report = ci.init_project(out, source_config=src)

        assert "max_iterations: 3" in mine.read_text(encoding="utf-8")
        assert report["config"] == "kept"

    def test_idempotent_second_run_changes_nothing(self, tmp_path):
        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"

        ci.init_project(out, source_config=src)
        first = (out / "config" / "agent.json").read_bytes()
        second = ci.init_project(out, source_config=src)

        assert (out / "config" / "agent.json").read_bytes() == first
        assert second["json"] == "kept"
        assert second["config"] == "kept"

    def test_web_env_file_is_created_and_loadable(self, tmp_path):
        """agent.env 必须先存在：config_write 对缺文件是 fail-closed 的。

        没人建它 → web 写面整体 500（`env_file_missing`），而原因只是缺个空文件
        （P1-2 在 .env 上踩过同款）。生成的内容必须不含任何键——dotenv 注释除外，
        否则等于替部署者预填了一轮配置。
        """
        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"

        report = ci.init_project(out, source_config=src)

        env = out / "config" / "agent.env"
        assert env.is_file(), "init 必须建出 web 写面落点"
        assert report["web_env"] == "created"
        text = env.read_text(encoding="utf-8")
        assert text.lstrip().startswith("#"), text
        # 只有注释、没有键：加载它不该往 environ 里写任何东西
        from agentcore import config_json

        assert config_json.apply_env_file(env) == ()

    def test_existing_web_env_file_is_kept(self, tmp_path):
        """已存在的 agent.env 里是管理员改过的键，覆盖等于毁配置。"""
        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"
        (out / "config").mkdir(parents=True)
        mine = out / "config" / "agent.env"
        mine.write_text("LLM_MODEL=step-3\n", encoding="utf-8")

        report = ci.init_project(out, source_config=src)

        assert mine.read_text(encoding="utf-8") == "LLM_MODEL=step-3\n"
        assert report["web_env"] == "kept"


class TestBootstrap:
    """容器首次启动自举：空项目目录 + 一条 ``compose up`` 就能起来。

    与 :class:`TestInitProject` 的分工：``init`` 是部署者手动跑一次、生成在项目
    目录；``bootstrap`` 由 entrypoint 每次启动时以 root 跑、生成在容器内的
    ``/app``（宿主侧即 compose 挂进来的 ``./config`` 与 ``./data``）。
    """

    @staticmethod
    def _app_root(tmp_path: Path) -> Path:
        """造一个容器内的 /app：带一份镜像里烤好的 config.yaml 模板。"""
        app = tmp_path / "app"
        app.mkdir()
        shutil.copyfile(ROOT / "config.yaml", app / "config.yaml")
        return app

    def test_fresh_app_root_creates_everything(self, tmp_path):
        app = self._app_root(tmp_path)

        report = ci.bootstrap(app)

        assert report == {
            "config_yaml": "created",
            "json": "created",
            "web_env": "created",
            "data": "created",
        }
        # config.yaml 必须原样复制镜像内那份——改内容等于偷偷改引擎配置
        assert (app / "config" / "config.yaml").read_bytes() == (
            ROOT / "config.yaml"
        ).read_bytes()
        data = json.loads((app / "config" / "agent.json").read_text(encoding="utf-8"))
        assert data["HOST"] == "0.0.0.0"
        assert data["AGENT_PUSH_STATE_FILE"] == "data/state/push-state.json"
        assert (app / "config" / "agent.env").is_file()
        assert sorted(p.name for p in (app / "data").iterdir()) == sorted(ci.DATA_DIRS)

    def test_config_yaml_lands_in_config_dir(self):
        """落点必须与 compose 的 AGENT_CONFIG 一致，否则引擎读不到自举出的配置。"""
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "AGENT_CONFIG: /app/config/config.yaml" in compose
        assert (ROOT / "config.yaml").is_file()

    def test_existing_files_are_never_overwritten(self, tmp_path):
        """已存在的是部署者填好的密钥/改过的配置，覆盖等于毁配置。"""
        app = self._app_root(tmp_path)
        (app / "config").mkdir()
        secret = app / "config" / "agent.json"
        secret.write_text('{"LLM_API_KEY": "sk-real-do-not-touch"}\n', encoding="utf-8")
        mine = app / "config" / "config.yaml"
        mine.write_text("agent:\n  max_iterations: 3\n", encoding="utf-8")
        before = (secret.read_bytes(), mine.read_bytes())

        report = ci.bootstrap(app)

        assert (secret.read_bytes(), mine.read_bytes()) == before
        assert report["json"] == "kept"
        assert report["config_yaml"] == "kept"
        assert "sk-real-do-not-touch" in secret.read_text(encoding="utf-8")
        assert "max_iterations: 3" in mine.read_text(encoding="utf-8")

    def test_idempotent_second_run_changes_nothing(self, tmp_path):
        app = self._app_root(tmp_path)

        ci.bootstrap(app)
        first = (app / "config" / "agent.json").read_bytes()
        second = ci.bootstrap(app)

        assert (app / "config" / "agent.json").read_bytes() == first
        assert set(second.values()) == {"kept"}

    def test_docker_created_directory_is_replaced(self, tmp_path):
        """docker 对不存在的 bind 源补建同名目录：自举要把目录态换回文件。"""
        app = self._app_root(tmp_path)
        (app / "config" / "agent.json").mkdir(parents=True)

        report = ci.bootstrap(app)

        assert (app / "config" / "agent.json").is_file()
        assert report["json"] == "created"

    def test_missing_template_falls_back_instead_of_failing(self, tmp_path):
        """自举跑在容器启动路径上：为一份配置让整个容器起不来更糟。

        镜像里 Dockerfile 有 COPY config.yaml，正常不会缺；缺了就写最小兜底并告警，
        真配置由部署者随后编辑。
        """
        app = tmp_path / "app"
        app.mkdir()

        report = ci.bootstrap(app)

        assert report["config_yaml"] == "created"
        text = (app / "config" / "config.yaml").read_text(encoding="utf-8")
        assert "max_iterations" in text

    def test_generated_json_is_loadable_by_config_json(self, tmp_path):
        """端到端：自举出的 agent.json 必须真能被 config_json.load() 读进 environ。"""
        from agentcore import config_json

        app = self._app_root(tmp_path)
        ci.bootstrap(app)

        report = config_json.load(app / "config" / "agent.json")

        assert report.error is None
        assert "HOST" in report.loaded
        assert set(ci.STATE_REDIRECTS) <= set(report.loaded)
        assert not [k for k in report.loaded if k.startswith("_")]


class TestGeneratedJsonIsActuallyLoadable:
    """端到端：init 生成的 JSON 必须能被 config_json 真正加载进 environ。"""

    def test_generated_json_populates_environ(self, tmp_path, monkeypatch):
        from agentcore import config_json

        src = tmp_path / "src-config.yaml"
        src.write_text("agent: {}\n", encoding="utf-8")
        out = tmp_path / "out"
        ci.init_project(out, source_config=src)

        report = config_json.load(out / "config" / "agent.json")

        assert report.error is None
        assert "HOST" in report.loaded
        assert config_json.to_env_value(8080) == "8080"
        # 备注键不能漏进 environ
        assert not [k for k in report.loaded if k.startswith("_")]
        # 7 个状态重定向也要真的加载进去（漏一个就 recreate 丢数据）
        assert set(ci.STATE_REDIRECTS) <= set(report.loaded)
        assert set(report.missing_required) == set(config_json.REQUIRED_KEYS)
        # 清理交给 autouse fixture，这里不手工 pop（漏清会污染其它模块）


class TestRealTemplate:
    """看守真实文件：本模块依赖的行必须在仓库里真实存在。"""

    def test_config_yaml_exists_in_repo(self):
        assert (ROOT / "config.yaml").is_file()

    def test_data_dirs_match_compose_mounts(self):
        """DATA_DIRS 与 compose 的挂载表必须一致，漏一个该目录就不落盘。"""
        compose = (ROOT / "docs" / "deploy-nas" / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        for name in ci.DATA_DIRS:
            assert f"/app/data/{name}" in compose, f"compose 里没有挂 /app/data/{name}"


# ---- 变异复核（2026-10-28）----
# 实现过程中真被抓到的一个：初版生成的是 .env，用户设备上 dotfile 被文件管理器反复
# 建成目录、Docker 的单文件 bind mount 又自动补目录，连踩两次后改为 JSON + 目录挂载。
# 其余逐条对应：
# 1. HOST 生成 127.0.0.1 → test_host_is_zero_zero_zero_zero 红
# 2. NAPCAT_HTTP_URL 漏 http:// 或端口写 6099 → test_napcat_url_is_http_scheme_and_port_3000 红
# 3. init_project 覆盖已存在 agent.json → test_existing_json_is_never_overwritten 红
# 4. 不处理目录态 → test_docker_created_json_directory_is_replaced 红
# 5. 非空目录也静默覆盖 → test_nonempty_directory_trap_aborts_loudly 红
# 6. 少生成某个状态重定向键 → test_state_redirects_all_present 红
# 7. DATA_DIRS 与 compose 挂载表漂移 → test_data_dirs_match_compose_mounts 红
# 8. missing_keys 不识别「换成」占位 → test_placeholder_and_empty_count_as_missing 红
