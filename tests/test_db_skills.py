"""db_query 只读数据库查询技能的专用测试。

对应实现 ``agentcore/skills/db_skills.py``。两层用例：
- **纯校验 / mock 层**（永远运行，不连任何库）：SQL 只读校验、statement_timeout 整数
  强转防注入、只读事务、行数/输出截断、结果围栏、异常不外泄连接串；
- **端到端层**（仅设了 TEST_DATABASE_URL 时运行）：连独立测试库跑 SELECT 1 与 INSERT 拒绝。

铁律：测试**绝不连生产库**——conftest 会把 .env 的 DATABASE_URL（生产）载入 environ，
故本文件 autouse fixture 一律删掉 DATABASE_URL；端到端只用 TEST_DATABASE_URL 经
``dsn=`` 显式注入，永不回落 DATABASE_URL。
"""

import os

import pytest

import agentcore.skills.db_skills as db_skills
from agentcore.skills.db_skills import db_query, register_db_skills


@pytest.fixture(autouse=True)
def _no_production_db(monkeypatch):
    """每个用例都删掉 DATABASE_URL，杜绝误连 .env 里的生产库。

    保留 TEST_DATABASE_URL 不动：端到端用例靠它决定是否运行、并作为 dsn 注入。
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)


# ---------- 纯校验层：攻击 SQL 一律拒绝（连库前就因非法返回，永不需要数据库） ----------

_REJECT_CASES = [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a=1",
    "DELETE FROM t",
    "DROP TABLE t",
    "CREATE TABLE t (a int)",
    "ALTER TABLE t ADD COLUMN b int",
    "GRANT ALL ON t TO public",
    "TRUNCATE t",
    "COPY t TO '/tmp/x'",
    "SELECT 1; DROP TABLE t",  # 多语句
    "SELECT /**/1;DELETE FROM t",  # 块注释藏多语句
    "SELECT pg_sleep(10)",  # 危险函数（纵深黑名单）
    "SELECT pg_read_file('/etc/passwd')",
]


@pytest.mark.parametrize("sql", _REJECT_CASES)
@pytest.mark.asyncio
async def test_rejects_write_multistatement_and_dangerous(sql):
    out = await db_query(sql)
    assert "拒绝" in out, sql


# ---------- 纯校验层：合法 SELECT/WITH（含注释、大小写混淆）必须放行 ----------

_ALLOW_CASES = [
    "SELECT 1 -- comment",  # 行注释
    "/* c */ SELECT 1",  # 块注释前缀
    "select 1",  # 全小写
    "SeLeCt 1",  # 大小写混淆
    "WITH x AS (SELECT 1 AS n) SELECT * FROM x",  # CTE
]


@pytest.mark.parametrize("sql", _ALLOW_CASES)
@pytest.mark.asyncio
async def test_allows_readonly_selects(sql):
    # DATABASE_URL 已被 autouse 删除：合法 SQL 应通过校验、停在 fail-closed 配置提示，
    # 而不是被拒。以此证明校验放行了它们（改坏校验 → 这些会变成"拒绝"或用例红）。
    out = await db_query(sql)
    assert "拒绝" not in out, sql
    assert "未配置 DATABASE_URL" in out, sql


@pytest.mark.asyncio
async def test_empty_sql_rejected():
    assert "拒绝" in await db_query("   ")


# ---------- mock 层：asyncpg 连接契约（不连真库，用假连接打桩） ----------


class _FakeRecord(dict):
    """最小 asyncpg Record 形态：keys() + 按键取值。"""

    def keys(self):
        return dict.keys(self)


class _FakeCursor:
    """asyncpg Cursor 的最小形态：``fetch(n)`` 是**行数上限**（最多 n 行）。"""

    def __init__(self, rows, captured):
        self._rows = rows
        self._captured = captured

    async def fetch(self, n, timeout=None):
        self._captured["fetch_n"] = n
        return self._rows[:n]


class _FakePrepared:
    """PreparedStatement 的**坑位**形状：真实 asyncpg 的 ``fetch(*args)`` 的坑是
    查询绑定参数而不是行数上限——0 参查询传任何位置参数都会被服务端拒绝。
    实现若从 cursor 退回 prepare().fetch(n) 形态，这里必须炸（回归护栏）。"""

    def __init__(self, rows, captured):
        self._rows = rows
        self._captured = captured

    async def fetch(self, *args, timeout=None):
        if args:
            raise db_skills.asyncpg.exceptions.InterfaceError(
                f"the server expects 0 arguments for this query, {len(args)} was passed"
            )
        return self._rows


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rows, readonly=True):
        self._rows = rows
        self._readonly = readonly
        self.captured: dict = {}
        self.closed = False

    def transaction(self, readonly=False, **kw):
        self.captured["readonly"] = readonly
        return _FakeTxn()

    async def execute(self, query):
        self.captured.setdefault("executed", []).append(query)

    async def cursor(self, sql):
        self.captured["cursor_sql"] = sql
        return _FakeCursor(self._rows, self.captured)

    async def prepare(self, sql):
        self.captured["prepared_sql"] = sql
        return _FakePrepared(self._rows, self.captured)

    async def close(self):
        self.closed = True


def _install_fake_connect(monkeypatch, rows, *, connect_raises=None):
    holder: dict = {}

    async def _connect(dsn=None, timeout=None, **kw):
        holder["dsn"] = dsn
        holder["timeout"] = timeout
        holder["kwargs"] = kw
        if connect_raises is not None:
            raise connect_raises
        holder["conn"] = _FakeConn(rows)
        return holder["conn"]

    monkeypatch.setattr(db_skills.asyncpg, "connect", _connect)
    return holder


@pytest.mark.asyncio
async def test_readonly_txn_and_fetch_limit_plus_one(monkeypatch):
    monkeypatch.setenv("AGENT_DB_MAX_ROWS", "200")
    holder = _install_fake_connect(monkeypatch, [_FakeRecord(n=1), _FakeRecord(n=2)])
    out = await db_query("SELECT n FROM t", dsn="postgresql://u@h/db")
    conn = holder["conn"]
    # 只读事务是写操作的最终闸（即使校验被绕过，PG 也会拒写）
    assert conn.captured["readonly"] is True
    assert holder["dsn"] == "postgresql://u@h/db"
    # fetch limit+1 探上界（走 cursor 的 fetch(n)，不是 prepare 的坑）
    assert conn.captured["fetch_n"] == 201
    assert conn.captured["cursor_sql"] == "SELECT n FROM t"
    # 结果必须过围栏（数据库内容 = 不可信数据）
    assert "数据库查询结果开始" in out
    assert "数据库查询结果结束" in out
    assert "不可信数据" in out
    assert conn.closed is True  # 用完即关，不复用连接池


@pytest.mark.asyncio
async def test_statement_timeout_is_pure_int(monkeypatch):
    monkeypatch.setenv("AGENT_DB_TIMEOUT_MS", "2500")
    holder = _install_fake_connect(monkeypatch, [_FakeRecord(n=1)])
    await db_query("SELECT 1", dsn="postgresql://u@h/db")
    assert holder["conn"].captured["executed"] == ["SET LOCAL statement_timeout = 2500"]


@pytest.mark.asyncio
async def test_statement_timeout_dirty_value_falls_back_no_injection(monkeypatch):
    # 注入尝试：非整数且带分号/DROP——必须被 int() 挡下，绝不进 SQL
    monkeypatch.setenv("AGENT_DB_TIMEOUT_MS", "10; DROP TABLE t")
    holder = _install_fake_connect(monkeypatch, [_FakeRecord(n=1)])
    await db_query("SELECT 1", dsn="postgresql://u@h/db")
    executed = holder["conn"].captured["executed"]
    assert executed == ["SET LOCAL statement_timeout = 10000"]  # 回退默认
    assert all(";" not in q and "DROP" not in q for q in executed)


@pytest.mark.asyncio
async def test_connect_timeout_env_passed(monkeypatch):
    monkeypatch.setenv("AGENT_DB_CONNECT_TIMEOUT", "7")
    holder = _install_fake_connect(monkeypatch, [_FakeRecord(n=1)])
    await db_query("SELECT 1", dsn="postgresql://u@h/db")
    assert holder["timeout"] == 7


@pytest.mark.asyncio
async def test_connect_failure_short_error_no_leak(monkeypatch):
    _install_fake_connect(
        monkeypatch,
        [],
        connect_raises=OSError("password auth failed for user secret@prod:5432/db"),
    )
    out = await db_query("SELECT 1", dsn="postgresql://secret:pw@prod:5432/db")
    assert "失败" in out
    # 连接串 / 密码绝不进返回值
    assert "prod" not in out
    assert "secret" not in out
    assert "pw" not in out


@pytest.mark.asyncio
async def test_query_error_short_message(monkeypatch):
    _install_fake_connect(monkeypatch, [_FakeRecord(n=1)])

    # 让 cursor 抛错，模拟 SQL 执行阶段失败
    async def _cursor(self, sql):
        raise RuntimeError('relation "t" does not exist')

    monkeypatch.setattr(_FakeConn, "cursor", _cursor)
    out = await db_query("SELECT * FROM missing", dsn="postgresql://u@h/db")
    assert "失败" in out
    assert "does not exist" not in out  # 不外泄底层错误细节


@pytest.mark.asyncio
async def test_prepare_fetch_row_limit_tripwire(monkeypatch):
    """回归护栏：实现若退回 ``prepare().fetch(n)``（把行数上限当绑定参数传），
    真实 asyncpg 会拒（expects 0 arguments）——这里的假连接按同款契约炸。"""
    _install_fake_connect(monkeypatch, [_FakeRecord(n=1)])
    conn = await db_skills.asyncpg.connect(dsn="postgresql://u@h/db")
    stmt = await conn.prepare("SELECT 1")
    with pytest.raises(db_skills.asyncpg.exceptions.InterfaceError):
        await stmt.fetch(201)


@pytest.mark.asyncio
async def test_no_rows_placeholder(monkeypatch):
    _install_fake_connect(monkeypatch, [])
    out = await db_query("SELECT 1 WHERE false", dsn="postgresql://u@h/db")
    assert "查询无结果" in out


@pytest.mark.asyncio
async def test_null_rendered_as_NULL(monkeypatch):
    _install_fake_connect(monkeypatch, [_FakeRecord(a=None, b=5)])
    out = await db_query("SELECT a, b", dsn="postgresql://u@h/db")
    assert "NULL" in out
    assert "5" in out


@pytest.mark.asyncio
async def test_cell_truncated_over_200(monkeypatch):
    _install_fake_connect(monkeypatch, [_FakeRecord(a="x" * 500)])
    out = await db_query("SELECT a", dsn="postgresql://u@h/db")
    assert "…" in out
    assert "x" * 201 not in out  # 单元格被截到 ~200


@pytest.mark.asyncio
async def test_row_truncation_note(monkeypatch):
    monkeypatch.setenv("AGENT_DB_MAX_ROWS", "3")
    rows = [_FakeRecord(n=i) for i in range(4)]  # fetch(4) 拿回 4 行 > 上限 3
    holder = _install_fake_connect(monkeypatch, rows)
    out = await db_query("SELECT n", dsn="postgresql://u@h/db")
    assert holder["conn"].captured["fetch_n"] == 4
    assert "共超过 3 行，已截断" in out


@pytest.mark.asyncio
async def test_char_cap_4000(monkeypatch):
    big = "y" * 190  # < 200，不触发单元格截断，靠总量触发
    rows = [_FakeRecord(a=big, b=big, c=big) for _ in range(30)]
    _install_fake_connect(monkeypatch, rows)
    monkeypatch.setenv("AGENT_DB_MAX_ROWS", "5000")
    out = await db_query("SELECT a, b, c", dsn="postgresql://u@h/db")
    assert "输出超过 4000 字符，已截断" in out


# ---------- 注册契约 ----------


def test_skill_registered_superuser_not_readonly():
    """注册契约：superuser 权限 + 只读标记**不在注册处打**（REVIEW C3：集中在
    builtin.py 尾部的 mark_read_only 审计点；新工具默认非只读，fail-closed）。"""
    from agentcore.skills.registry import SkillRegistry

    reg = SkillRegistry()
    register_db_skills(reg)
    skill = reg.skills["db_query"]
    assert skill.permission == "superuser"
    assert skill.read_only is False
    assert skill.params_schema["required"] == ["sql"]
    assert skill.params_schema["properties"]["sql"]["type"] == "string"


def test_builtin_does_not_mark_db_query_readonly(monkeypatch):
    """集中审计点口径：db_query 结果要进 prompt，按 fail-closed **不进**只读并行名单。"""
    import agentcore.skills.builtin as B
    from agentcore.skills.registry import SkillRegistry

    # 清掉 SEARCH_API_KEY：否则 register_builtin_skills 会真的注册搜索 skill 并
    # 填充 search 模块的全局 _state 缓存，污染后续 test_search 的用例（顺序依赖）
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    reg = SkillRegistry()
    B.register_builtin_skills(reg)
    assert reg.is_read_only("db_query") is False
    # 对照组：纯查询的 docker_ps/docker_logs 确认真被标了（审计点本身有效）
    assert reg.is_read_only("docker_ps") is True
    assert reg.is_read_only("docker_logs") is True


# ---------- 端到端层：仅设 TEST_DATABASE_URL 时运行（绝不用生产库） ----------


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="未设置 TEST_DATABASE_URL，跳过真实库端到端用例",
)
class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_select_literal(self):
        out = await db_query("SELECT 1 AS n", dsn=os.getenv("TEST_DATABASE_URL"))
        assert "1" in out
        assert "数据库查询结果" in out  # 过了围栏

    @pytest.mark.asyncio
    async def test_insert_rejected(self):
        out = await db_query(
            "INSERT INTO t VALUES (1)", dsn=os.getenv("TEST_DATABASE_URL")
        )
        assert "拒绝" in out


class TestHandlerAdminGate:
    """P0 回归：db_query handler 必须二次校验 is_superuser（纵深）。"""

    @pytest.mark.asyncio
    async def test_non_admin_gets_handler_denial(self, monkeypatch):
        from agentcore.skills.db_skills import register_db_skills
        from agentcore.skills.registry import SkillRegistry

        monkeypatch.setenv("SUPERUSERS", "10001")
        monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db")
        reg = SkillRegistry()
        register_db_skills(reg)

        class _AllowAll:
            def is_allowed(self, *a, **k):
                return True  # 模拟 registry 层被击穿：handler 必须兜住

        reg.permission_checker = _AllowAll()
        out = await reg.execute("db_query", user_id="99999", sql="SELECT 1")
        assert "无权限" in out

    @pytest.mark.asyncio
    async def test_superuser_reaches_dsn_error_path(self, monkeypatch):
        from agentcore.skills.db_skills import register_db_skills
        from agentcore.skills.permissions import PermissionChecker
        from agentcore.skills.registry import SkillRegistry

        monkeypatch.setenv("SUPERUSERS", "10001")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10001"}))
        register_db_skills(reg)
        out = await reg.execute("db_query", user_id="10001", sql="SELECT 1")
        # 过了管理员闸，走到「未配置 DATABASE_URL」的关闭提示（不是无权限）
        assert "无权限" not in out
        assert "DATABASE_URL" in out
