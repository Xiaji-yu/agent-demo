"""Web 只读总览：在浏览器里看运行状态（**零写入接口**）。

安全模型——这是本仓新增的攻击面，写错等于把 bot 数据暴露全网：

1. **fail-closed**：未配置 ``AGENT_WEB_TOKEN`` 时**整个 web 面不挂载**。
   默认部署（照抄 .env.example 但没设 token） therefore 没有任何暴露。
2. token 从 ``Authorization: Bearer <token>`` 取，``secrets.compare_digest``
   恒等比较；**不接受 URL query**（query 会进反代/访问日志）。
3. 可选 ``AGENT_WEB_ALLOW_CIDRS`` 源 IP 白名单（直连场景）。反代部署下
   ``request.client.host`` 是反代地址，那种场景应在反代层做 IP 限制。
4. 路由统一挂在 ``/agent-web`` 前缀，避开 OneBot 反向 WS 的路径。
5. **只读**：没有任何写接口，因而不存在"网页改坏配置/删数据"的风险。
   后续要加写入时必须同时补：审计（``diagnostics.record``）+ 二次确认 +
   备份原值。见 BACKLOG 的 Web 分项。

数据口径：所有数字都来自现成来源（``budget`` 账本、``kb.stats()``、
``LLMClient.model_status()``、``driver._agent_*``），取不到就置 null 并在
``errors`` 里说明原因——**不猜、不补零**（恒真数据比没有更糟）。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import time
from typing import Any

from agentcore.diagnostics import recent as _recent_events

logger = logging.getLogger(__name__)

# 必须**模块级**导入：本文件有 `from __future__ import annotations`，注解是惰性
# 字符串，FastAPI 靠模块全局解析它们——若在函数内 import，`request: Request`
# 会被当成 query 参数（实测 422 missing query request）。非 fastapi driver 的
# 部署下降级为 None，mount_web 直接不挂载。
try:
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse
except Exception:  # pragma: no cover - 没有 fastapi 的部署
    Depends = FastAPI = HTTPException = Request = None  # type: ignore[assignment]
    HTMLResponse = JSONResponse = None  # type: ignore[assignment]

PREFIX = "/agent-web"

# 历史累计要遍历所有月份账本，不该跟着 5s 轮询反复算
_HISTORY_TTL = 60.0
_history_cache: dict[str, Any] = {"at": 0.0, "value": None}


# ---------- 配置 ----------
def _web_token() -> str:
    return (os.getenv("AGENT_WEB_TOKEN") or "").strip()


def _allow_cidrs() -> list[str]:
    raw = (os.getenv("AGENT_WEB_ALLOW_CIDRS") or "").strip()
    return [c.strip() for c in raw.split(",") if c.strip()]


def _ip_allowed(host: str) -> bool:
    """未配置白名单 = 只靠 token；配置了则源 IP 必须在任一网段内。"""
    cidrs = _allow_cidrs()
    if not cidrs:
        return True
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    for c in cidrs:
        try:
            if addr in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            logger.warning("AGENT_WEB_ALLOW_CIDRS 含非法网段：%r", c)
    return False


# ---------- 数据装配（never raises） ----------
async def build_overview() -> dict:
    """汇总总览数据。任何一块取不到只记 ``errors``，绝不影响其他块与 HTTP 200。"""
    out: dict[str, Any] = {"generated_at": time.time(), "errors": []}

    def _fail(section: str, exc: Exception) -> None:
        out["errors"].append(f"{section}: {type(exc).__name__}")

    # LLM 主/备实时线路（刚加的能力，主页最该看的一项）
    try:
        from agentcore.skills.registry import get_shared_llm_client

        out["llm"] = get_shared_llm_client().model_status()
    except Exception as e:
        out["llm"] = None
        _fail("llm", e)

    # 协议端连接：reverse-WS 下 driver.bots 只含**已连接**的 bot，空 = 没连上
    try:
        from nonebot import get_driver

        driver = get_driver()
        out["bots"] = [
            {"self_id": str(sid)} for sid in (getattr(driver, "bots", None) or {})
        ]
    except Exception as e:
        out["bots"] = []
        _fail("bots", e)

    # 预算（今日 + 成本 + 限额；历史累计带 TTL 缓存）
    try:
        from agentcore.budget import get_budget

        b = get_budget()
        day = b.today()
        now = time.monotonic()
        if now - _history_cache["at"] > _HISTORY_TTL:
            total = b.total()
            # 先算后记账：total() 一抛就把「at=now + value=None」一起写进去，
            # 5s 轮询下预算块要闪一分钟「读取失败」且 today 数据陪着一起丢
            _history_cache["at"] = now
            _history_cache["value"] = total
        out["budget"] = {
            "today": day,
            "cost_today": b.estimate_cost(),
            "daily_tokens": b.daily_tokens,
            "enforce": b.enforce,
            "history": _history_cache["value"],
        }
    except Exception as e:
        out["budget"] = None
        _fail("budget", e)

    # 组件：记忆后端 / 人格 / skill 数 / 知识库
    # key 先落默认值：取数失败时是 null 而不是"键不存在"（前端要能稳定渲染）
    out.setdefault("memory_backend", None)
    out.setdefault("skills", [])
    out.setdefault("persona_default", None)
    try:
        from nonebot import get_driver

        driver = get_driver()
        memory = getattr(driver, "_agent_memory", None)
        out["memory_backend"] = type(memory).__name__ if memory is not None else None
        engine = getattr(driver, "_agent_engine", None)
        skills = getattr(engine, "skills", None)
        out["skills"] = (
            [
                {"name": s.name, "permission": s.permission}
                for s in skills.skills.values()
            ]
            if skills is not None
            else []
        )
        pm = getattr(driver, "_agent_persona_manager", None)
        if pm is not None:
            default = pm.default()
            out["persona_default"] = default.name if default else None
        else:
            out["persona_default"] = None
    except Exception as e:
        _fail("components", e)

    try:
        from nonebot import get_driver

        kb = getattr(get_driver(), "_agent_kb", None)
        if kb is None:
            out["kb"] = None
        else:
            out["kb"] = {"stats": await kb.stats(), "describe": kb.describe()}
    except Exception as e:
        out["kb"] = None
        _fail("kb", e)

    # 运行时开关（这些函数本来就是调用时读 env，值即当前生效值）
    try:
        from plugins.qq_agent_adapter.group_context import context_enabled
        from plugins.qq_agent_adapter.pipeline import vision_enabled

        out["flags"] = {
            "group_context": context_enabled(),
            "vision": vision_enabled(),
        }
    except Exception as e:
        out["flags"] = None
        _fail("flags", e)

    out["recent_events"] = _recent_events(20)
    return out


# ---------- HTTP 面 ----------
_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-demo 总览</title>
<style>
 :root { color-scheme: light dark; }
 * { box-sizing: border-box; }
 body { font: 14px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
        margin: 0; padding: 20px; background: #f6f7f9; color: #1c1e21; }
 @media (prefers-color-scheme: dark) {
   body { background: #16181d; color: #e6e8eb; }
   .card { background: #1f2229; border-color: #2c3038; }
   code, .mono { color: #9ecbff; }
 }
 h1 { font-size: 18px; margin: 0 0 4px; }
 .sub { color: #6b7280; font-size: 12px; margin-bottom: 16px; }
 .grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
 .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px; }
 .card h2 { font-size: 13px; margin: 0 0 8px; color: #6b7280; font-weight: 600; }
 .kv { display: flex; justify-content: space-between; gap: 12px; padding: 3px 0; }
 .kv span:last-child { font-variant-numeric: tabular-nums; text-align: right; }
 .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
 .ok { color: #16a34a; } .warn { color: #d97706; } .bad { color: #dc2626; }
 table { width: 100%; border-collapse: collapse; font-size: 12px; }
 td, th { text-align: left; padding: 3px 6px 3px 0; }
 th { color: #6b7280; font-weight: 600; }
 .num { text-align: right; font-variant-numeric: tabular-nums; }
 #login { max-width: 420px; margin: 12vh auto; }
 #login .card { padding: 20px; }
 input { width: 100%; padding: 8px 10px; border: 1px solid #d1d5db; border-radius: 8px;
         font-size: 14px; margin: 8px 0; }
 button { padding: 8px 14px; border: 0; border-radius: 8px; background: #2563eb;
          color: #fff; font-size: 14px; cursor: pointer; }
 .err { color: #dc2626; font-size: 12px; white-space: pre-wrap; }
</style>
</head>
<body>
<div id="login" class="card">
  <h2>agent-demo 总览</h2>
  <div class="sub">请输入 AGENT_WEB_TOKEN（只存当前标签页的 sessionStorage，不落盘）</div>
  <input id="tok" type="password" placeholder="token" autocomplete="off">
  <button onclick="save()">进入</button>
  <div id="lerr" class="err"></div>
</div>
<div id="app" hidden>
  <h1>agent-demo 总览</h1>
  <div class="sub">只读视图 · 每 5s 刷新 · <span id="ts"></span></div>
  <div class="grid" id="grid"></div>
</div>
<script>
const KEY = "agent_web_token";
function tok() { return sessionStorage.getItem(KEY) || ""; }
function save() { sessionStorage.setItem(KEY, document.getElementById("tok").value.trim()); load(); }
// 支持 #token=xxx：fragment 不会发给服务器（不进反代/访问日志，比 ?token= 安全），
// 读到手就收进 sessionStorage 并立刻从地址栏抹掉，避免token留在历史/截屏里。
(function () {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    sessionStorage.setItem(KEY, decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname + location.search);
  }
})();
function fmt(n) { return (n === null || n === undefined) ? "—" : Number(n).toLocaleString(); }
function pct(a, b) { return (b > 0) ? Math.round(a / b * 100) + "%" : "—"; }
function kv(k, v, cls) {
  return '<div class="kv"><span>' + k + '</span><span class="' + (cls || "") + '">' + v + "</span></div>";
}
function card(title, inner) { return '<div class="card"><h2>' + title + "</h2>" + inner + "</div>"; }
function esc(s) { return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

async function load() {
  let d;
  try {
    const r = await fetch("api/overview", { headers: { "Authorization": "Bearer " + tok() } });
    if (r.status === 401 || r.status === 403) {
      document.getElementById("login").hidden = false;
      document.getElementById("app").hidden = true;
      document.getElementById("lerr").textContent = "token 不正确或来源 IP 不被允许";
      return;
    }
    d = await r.json();
  } catch (e) {
    document.getElementById("lerr").textContent = "请求失败：" + e;
    return;
  }
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
  document.getElementById("ts").textContent = new Date(d.generated_at * 1000).toLocaleString();

  const g = [];
  // LLM 线路
  let l = d.llm;
  g.push(card("LLM 线路", l
    ? kv("当前生效", '<span class="mono">' + esc(l.active) + "</span>")
      + kv("线路", l.line === "fallback"
          ? '<span class="bad">备用（主模型不可用）</span>' : '<span class="ok">主线路</span>')
      + kv("主模型", '<span class="mono">' + esc(l.primary) + "</span>")
      + kv("备用模型", '<span class="mono">' + esc(l.fallback || "未配置") + "</span>")
    : '<span class="bad">读取失败</span>'));

  // 协议端
  g.push(card("协议端连接", d.bots.length
    ? d.bots.map(b => kv("bot " + esc(b.self_id), '<span class="ok">已连接</span>')).join("")
    : kv("reverse-WS", '<span class="bad">未连接</span>')));

  // 预算
  let b = d.budget;
  if (b && b.today) {
    const t = b.today;
    g.push(card("今日用量",
      kv("对话 token", fmt(t.total) + (b.daily_tokens ? " / " + fmt(b.daily_tokens) + "（" + pct(t.total, b.daily_tokens) + "）" : ""),
         b.enforce && b.daily_tokens && t.total >= b.daily_tokens ? "bad" : "")
      + kv("请求数", fmt(t.chat_requests))
      + kv("估算成本", b.cost_today === null ? "未配单价" : "≈ " + b.cost_today.toFixed(2) + " 元")
      + kv("闸门", b.enforce ? '<span class="warn">硬闸</span>' : "软")));
    const rows = Object.entries(t.by_model || {}).sort((a, c) => (c[1].prompt + c[1].completion) - (a[1].prompt + a[1].completion));
    g.push(card("今日按模型", rows.length
      ? "<table><tr><th>模型</th><th class='num'>prompt</th><th class='num'>completion</th><th class='num'>次数</th></tr>"
        + rows.map(([m, v]) => "<tr><td class='mono'>" + esc(m) + "</td><td class='num'>" + fmt(v.prompt)
          + "</td><td class='num'>" + fmt(v.completion) + "</td><td class='num'>" + fmt(v.requests) + "</td></tr>").join("")
        + "</table>"
      : "今日无调用"));
    const h = b.history;
    if (h) g.push(card("历史累计",
      kv("对话 token", fmt(h.total)) + kv("请求数", fmt(h.chat_requests))
      + kv("embedding token", fmt(h.embedding_tokens))));
  } else {
    g.push(card("预算", '<span class="bad">读取失败</span>'));
  }

  // 组件
  g.push(card("组件",
    kv("记忆后端", '<span class="mono">' + esc(d.memory_backend || "未初始化") + "</span>")
    + kv("默认人格", esc(d.persona_default || "（无）"))
    + kv("已装 skill", fmt((d.skills || []).length))
    + (d.flags ? kv("群上下文", d.flags.group_context ? "开" : "关") + kv("vision", d.flags.vision ? "开" : "关") : "")));

  // 知识库
  g.push(card("知识库", d.kb
    ? kv("状态", d.kb.describe.enabled ? '<span class="ok">启用</span>' : "关闭")
      + kv("chunks", fmt(d.kb.stats && d.kb.stats.chunks))
      + kv("来源数", fmt(d.kb.stats && d.kb.stats.sources))
    : "未初始化"));

  // 最近事件
  const evs = d.recent_events || [];
  g.push(card("最近事件", evs.length
    ? "<table>" + evs.map(e => "<tr><td class='mono'>" + new Date(e.ts * 1000).toLocaleTimeString()
        + "</td><td>" + esc(e.kind) + "</td><td class='mono'>" + esc(JSON.stringify(
            Object.fromEntries(Object.entries(e).filter(([k]) => k !== "ts" && k !== "kind")))) + "</td></tr>").join("") + "</table>"
    : "（无）"));

  if (d.errors && d.errors.length) {
    g.push(card("取数失败的部分", '<div class="err">' + d.errors.map(esc).join("\\n") + "</div>"));
  }
  document.getElementById("grid").innerHTML = g.join("");
}
load();
setInterval(load, 5000);
</script>
</body>
</html>
"""


def mount_web(app=None) -> bool:
    """把只读总览挂到 ASGI 应用上；返回是否挂载。

    ``app`` 为 None 时取 NoneBot 的 app（生产路径）。测试可传独立 FastAPI
    实例，避免污染全局 NoneBot 应用。未配 token / 取不到 app 都不挂载。
    """
    token = _web_token()
    if not token:
        logger.info("web 总览未挂载：AGENT_WEB_TOKEN 未配置（fail-closed）")
        return False
    if not token.isascii():
        # compare_digest 对非 ASCII str 抛 TypeError（会变 500 而非 401），
        # 整个 web 面不可用——挂载期直接拒绝并给出可行动的告警
        logger.error(
            "web 总览未挂载：AGENT_WEB_TOKEN 含非 ASCII 字符，请改用纯 ASCII token"
        )
        return False
    if Request is None or FastAPI is None:
        logger.info("web 总览未挂载：当前环境没有 fastapi")
        return False
    try:
        if app is None:
            from nonebot import get_app

            app = get_app()
        if not isinstance(app, FastAPI):
            logger.warning("web 总览未挂载：当前 driver 的 app 不是 FastAPI 实例")
            return False

        async def _auth(request: Request) -> None:
            if not _ip_allowed(request.client.host if request.client else ""):
                raise HTTPException(status_code=403, detail="source ip not allowed")
            header = request.headers.get("authorization") or ""
            supplied = header[7:].strip() if header[:7].lower() == "bearer " else ""
            # 恒等比较：避免按字节比较带来的时序侧信道
            if not supplied or not secrets.compare_digest(supplied, token):
                raise HTTPException(status_code=401, detail="invalid token")

        # 认证依赖放 decorator 的 dependencies=（而非参数默认值）：既是 FastAPI
        # 对"只做鉴权的依赖"的惯用法，也避开 bugbear B008。
        #
        # **页面故意不挂 _auth**：它只是登录表单 + JS，本身零机密。若连页面也锁，
        # 未认证用户拿到的就是 401 JSON，永远看不到输入框（鸡生蛋，线上实测）。
        # 数据面（api/overview）仍然强制鉴权。
        @app.get(f"{PREFIX}/", response_class=HTMLResponse)
        async def _page() -> HTMLResponse:
            return HTMLResponse(_PAGE)

        @app.get(f"{PREFIX}/api/overview", dependencies=[Depends(_auth)])
        async def _overview() -> JSONResponse:
            return JSONResponse(await build_overview())

        logger.info("web 总览已挂载：%s/（token 已配置）", PREFIX)
        return True
    except Exception:
        logger.exception("web 总览挂载失败（不影响 bot 运行）")
        return False
