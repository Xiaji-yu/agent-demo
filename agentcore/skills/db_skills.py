"""只读数据库查询 skill（**仅管理员**）：单条 SELECT/WITH，绝不写库。

安全边界（为什么这样做、拒绝什么）：
- DSN 只从 ``os.getenv("DATABASE_URL")`` 读；未配置即整体关闭（fail-closed），
  **绝不猜默认库**——连错库等于把生产数据暴露给 LLM。测试经 ``dsn=`` 显式注入，
  永不回落 ``DATABASE_URL``。
- **只读事务** ``conn.transaction(readonly=True)``：数据库层再兜一层。即使前置字符串
  校验被绕过（例如 ``WITH x AS (DELETE ... RETURNING *) SELECT ...`` 这种数据修改型
  CTE），写操作也会被 PostgreSQL 拒绝——这是最后一道、也是最可靠的闸。
- **statement_timeout**：``SET`` 不支持参数化绑定，故先 ``int()`` 强转再用 f-string
  拼**纯数字**。强转后没有注入面；脏值（非整数/越界）回退默认并记 WARNING，绝不把
  原始字符串拼进 SQL。
- **单语句 + 前缀白名单**：执行前纯字符串校验——去注释后不得再含 ``;``（拒多语句），
  首关键字必须是小写化的 ``select``/``with``。白名单已拦住正常越权；危险函数名
  （pg_sleep/pg_read_file/...）的**子串黑名单是纵深防御**，兜住注释后、大小写变形、
  字符串拼接等前缀判定可能漏掉的形态。
- **行数/输出双上限**：``fetch(limit+1)`` 探上界，超限截断并标注；单元格与总输出
  分别封顶，避免一次查询把超大结果灌进 prompt。
- **结果过围栏**：数据库内容与搜索结果同级，是**不可信数据**（可能含指向模型的
  注入指令），必须经 ``agentcore.safety.fence_untrusted`` 包裹后才返回。
- **异常**：连接失败/查询错误只返回简短中文，**绝不外泄连接串或密码**（详情进服务端
  日志，不进返回值）。

已知取舍：单个单引号字符串字面量里含 ``;``（如 ``SELECT ';'``）会被多语句判定误杀
（fail-closed，管理员改用 ``chr(59)`` 即可）；注释剥离只识别单引号字符串，不解析
dollar-quote / 双引号标识符——这些形态同样 fail-closed 从严，不影响只读边界。
"""

from __future__ import annotations

import logging
import os
import re

import asyncpg

from agentcore.safety import fence_untrusted
from agentcore.skills.levels import at_least
from agentcore.workspace.utils import is_superuser

logger = logging.getLogger(__name__)

# 只读查询允许的首关键字（小写化后比对）：SELECT 与 WITH(CTE) 是仅有的两种只读入口。
_ALLOWED_PREFIX = frozenset({"select", "with"})

# 纵深防御黑名单：这些函数可读文件/列目录/建外连/耗时拖库，命中即拒。
# 前缀白名单（仅 select/with）本已挡住「以此类函数名开头」的正常情况；子串黑名单
# 兜住藏在注释后、大小写变形、字符串拼接里的漏网形态——这是纵深，不是唯一防线。
_BLOCKED_FUNCS = (
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "lo_import",
    "lo_export",
    "dblink",
    "set_config",
)

# 输出约束：单元格 / 总输出字符上限；行数上限的硬边界。
_MAX_CELL_CHARS = 200
_MAX_OUTPUT_CHARS = 4000
_MAX_ROWS_HI = 5000

_FIRST_KEYWORD_RE = re.compile(r"[A-Za-z_]+")


def _strip_comments(sql: str) -> str:
    """去掉 ``--`` 行注释与 ``/* */`` 块注释，保留单引号字符串字面量原样。

    只识别单引号字符串（最常见）；dollar-quote / 双引号标识符不解析——那些形态若含
    ``;`` 或注释符，会由后续多语句/关键字判定从严处理（fail-closed）。块注释用单个
    空格替换，避免 ``SELECT/**/1`` 之类的 token 被粘连成一个词。
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    in_str = False
    while i < n:
        c = sql[i]
        if in_str:
            out.append(c)
            if c == "'":
                if i + 1 < n and sql[i + 1] == "'":  # '' 转义
                    out.append(sql[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j  # 行注释吃到行尾（换行符本身保留）
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2  # 未闭合的块注释丢弃剩余
            out.append(" ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _validate_sql(sql: str) -> str | None:
    """执行前的纯字符串只读校验。返回 None 表示放行，否则返回中文拒绝原因。

    顺序：去注释 → 去首尾空白 → 去结尾分号 → 拒残留分号（多语句）→ 首关键字必须
    select/with → 危险函数名子串黑名单（纵深）。
    """
    if not sql or not sql.strip():
        return "拒绝：SQL 为空。"
    s = _strip_comments(sql).strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    if ";" in s:
        return "拒绝：只允许单条只读 SQL 语句（检测到分号，疑似多语句注入）。"
    m = _FIRST_KEYWORD_RE.match(s)
    if not m:
        return "拒绝：无法解析 SQL 首关键字（只允许 SELECT / WITH 开头的只读查询）。"
    kw = m.group(0).lower()
    if kw not in _ALLOWED_PREFIX:
        return (
            f"拒绝：只允许 SELECT / WITH 开头的只读查询（首关键字为 {kw!r}，"
            "禁止写入 / DDL / 管理等操作）。"
        )
    low = s.lower()
    for fn in _BLOCKED_FUNCS:
        if fn in low:
            return f"拒绝：检测到危险函数 {fn}（纵深防御黑名单，禁止读文件 / 外连 / 耗时）。"
    return None


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    """读一个整数型 env：空→默认；非整数→默认+WARNING；越界→收敛到 [lo,hi]+WARNING。

    返回值一定是 [lo,hi] 内的 int——调用方可安全地 f-string 进 SQL（statement_timeout）。
    """
    raw = (os.getenv(name) or "").strip()
    if raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning("env %s=%r 非整数，回退默认 %s", name, raw, default)
        return default
    clamped = max(lo, min(val, hi))
    if clamped != val:
        logger.warning("env %s=%s 越界[%s,%s]，收敛为 %s", name, val, lo, hi, clamped)
    return clamped


def _cell(value: object) -> str:
    """单元格渲染：NULL→``NULL``；超 200 字符截断；其余转 str。"""
    if value is None:
        return "NULL"
    text = str(value)
    if len(text) > _MAX_CELL_CHARS:
        text = text[:_MAX_CELL_CHARS] + "…"
    return text


def _render(rows: list, limit: int) -> str:
    """把查询结果渲染成对齐文本表格；无行→占位；超行数 / 超长分别截断标注。"""
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    if not rows:
        return "（查询无结果）"
    columns = list(rows[0].keys())
    matrix = [[_cell(row[c]) for c in columns] for row in rows]
    widths = [
        max([len(str(c))] + [len(matrix[r][i]) for r in range(len(matrix))])
        for i, c in enumerate(columns)
    ]

    def _line(values: list[str]) -> str:
        return " | ".join(v.ljust(widths[i]) for i, v in enumerate(values))

    header = _line([str(c) for c in columns])
    rule = "-+-".join("-" * w for w in widths)
    body = "\n".join(_line(r) for r in matrix)
    text = f"{header}\n{rule}\n{body}"
    if truncated:
        text += f"\n…（共超过 {limit} 行，已截断）"
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[:_MAX_OUTPUT_CHARS] + "\n…（输出超过 4000 字符，已截断）"
    return text


async def db_query(sql: str, *, dsn: str | None = None) -> str:
    """执行一条只读 SELECT/WITH 查询并返回**过围栏**的结果表格（仅管理员）。

    ``dsn`` 仅供测试注入；生产路径恒为 None → 读 ``DATABASE_URL``。
    """
    err = _validate_sql(sql)
    if err is not None:
        return err
    url = dsn if dsn is not None else (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        return (
            "未配置 DATABASE_URL：只读数据库查询已关闭（fail-closed）。"
            "请在 .env 设置 DATABASE_URL 后重启生效；不会连接任何默认数据库。"
        )
    connect_timeout = _int_env("AGENT_DB_CONNECT_TIMEOUT", 10, 1, 120)
    timeout_ms = _int_env("AGENT_DB_TIMEOUT_MS", 10000, 1, 600000)
    limit = _int_env("AGENT_DB_MAX_ROWS", 200, 1, _MAX_ROWS_HI)
    try:
        conn = await asyncpg.connect(dsn=url, timeout=connect_timeout)
    except Exception:
        logger.exception("db_query: 连接数据库失败")
        return "连接数据库失败：请检查 DATABASE_URL 与网络（详情见服务端日志）。"
    try:
        async with conn.transaction(readonly=True):
            # SET 不支持参数化；timeout_ms 已是强转 int，拼出来必为纯数字，无注入面
            await conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            # 行数上限必须走 Cursor.fetch(n)：PreparedStatement.fetch(*args) 的坑是
            # **查询绑定参数**（传了 N 会被服务端判 "expects 0 arguments" 而报错），
            # 不是行数限量——cursor 才是 "最多取 N 行" 的正确 API。
            cur = await conn.cursor(sql)
            rows = await cur.fetch(limit + 1)
    except Exception:
        logger.exception("db_query: 查询执行失败")
        return "查询失败：SQL 执行出错或被数据库拒绝（详情见服务端日志）。"
    finally:
        try:
            await conn.close()
        except Exception:
            logger.exception("db_query: 关闭连接失败")
    return fence_untrusted("数据库查询结果", _render(list(rows), limit), "外部数据")


def register_db_skills(registry) -> None:
    """注册只读数据库查询技能（仅管理员）。"""

    @registry.register(
        "db_query",
        "在 .env 配置的 PostgreSQL 上执行**一条只读** SELECT/WITH 查询，返回结果表格"
        "（仅管理员）。仅支持单条 SELECT/WITH、只读事务，禁止任何写入 / DDL / 危险函数；"
        "行数与输出长度有上限，超限自动截断。未配置 DATABASE_URL 时功能关闭。",
        {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "单条只读 SQL：只能以 SELECT 或 WITH 开头、一条语句，"
                    "不得含 INSERT/UPDATE/DELETE/DROP 等写操作或 pg_sleep 等危险函数。",
                }
            },
            "required": ["sql"],
        },
        permission="superuser",
        # 只读标记刻意不在这里打：按仓库约定（REVIEW C3）集中在 builtin.py 尾部的
        # mark_read_only 审计点统一登记，新工具默认非只读（fail-closed）。
    )
    async def db_query_skill(sql: str, user_id: str = "") -> str:
        # handler 内二次校验（P0 纵深）：db_query 能读全部业务数据，
        # 不依赖 registry 层单一闸门（该层曾被 config.yaml 通配符击穿）。
        if not is_superuser(user_id):
            return "无权限：仅管理员可查询数据库。"
        # 级别门（medium+）：low 档本技能不注册，这里只是纵深兜底
        if not at_least("medium"):
            return (
                "当前权限级别为 low：数据库查询需要 medium 及以上"
                "（AGENT_PERMISSION_LEVEL，改后重启生效）。"
            )
        return await db_query(sql)
