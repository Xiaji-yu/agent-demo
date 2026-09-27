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
# 页面（HTML+CSS+JS，自包含、不引任何外部资源——这是局域网管理页，不能依赖外网）。
# **必须是 raw string**：内容里的反斜杠要原样进浏览器（如 JS 的 join("\n")）。
# 普通三引号会把 \n 吃成真实换行 → 整个 <script> 语法错误、页面白屏，
# 而且只有浏览器（或 node --check）才看得出来，pytest 抓不到。
_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-demo 总览</title>
<style>
:root{
  --paper:#f5f4f1; --paper-2:#efeeea; --ink:#111111; --ink-soft:#3a3a3a;
  --muted:#8a8782; --line:#111111; --line-soft:#cbc9c3; --white:#fff;
  --alert:#b23c2a;
  --sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei','PingFang SC','Hiragino Sans GB',sans-serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;
  --pad:clamp(18px,3.6vw,54px); --maxw:1440px; --ease:cubic-bezier(.2,.7,.2,1);
}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
::selection{background:var(--ink);color:var(--paper)}
:focus-visible{outline:1px solid var(--ink);outline-offset:3px}
h1,h2,h3,p{margin:0}
input,button{font:inherit;color:inherit}
.mono{font-family:var(--mono);font-size:11px;font-weight:400;letter-spacing:.14em;text-transform:uppercase}
.wrap{width:100%;max-width:var(--maxw);margin:0 auto;padding:0 var(--pad)}

/* 页眉 ------------------------------------------------------------------ */
.hdr{position:sticky;top:0;z-index:10;background:var(--paper);border-bottom:1px solid var(--line)}
.hdr__in{display:flex;align-items:center;gap:18px;height:56px}
.hdr__brand{display:flex;align-items:baseline;gap:12px;min-width:0}
.hdr__mark{font-size:13px;line-height:1}
.hdr__name{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.2em}
.hdr__sub{color:var(--muted)}
.hdr__right{margin-left:auto;color:var(--muted);white-space:nowrap}
.dot{display:inline-block;width:7px;height:7px;border:1px solid var(--line);
  border-radius:var(--radius-pill,999px);margin-right:7px;vertical-align:1px}
.dot--on{background:var(--ink)}
.dot--off{background:repeating-linear-gradient(45deg,var(--ink) 0 1px,transparent 1px 4px)}

/* 斜纹分隔条（点阵/工程图纸感） ---------------------------------------- */
.hatch{height:16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
  background-image:repeating-linear-gradient(45deg,var(--ink) 0 1px,transparent 1px 5px);
  background-size:auto 100%;opacity:.55}
@media (prefers-reduced-motion:no-preference){
  .hatch{background-size:254.5585px 100%;animation:hatchFlow 32s linear infinite}
  @keyframes hatchFlow{to{background-position:-254.5585px 0}}
}

/* 登录门 ---------------------------------------------------------------- */
.gate{display:flex;align-items:center;justify-content:center;min-height:calc(100vh - 73px);
  padding:clamp(28px,7vh,90px) var(--pad)}
.gate__box{width:100%;max-width:560px;border:1px solid var(--line);background:var(--paper);padding:clamp(22px,3.4vw,40px)}
.gate__kicker{color:var(--muted);margin-bottom:14px}
.gate__title{font-size:clamp(24px,3.4vw,38px);font-weight:600;letter-spacing:-.02em;line-height:1.1;margin-bottom:12px}
.gate__lead{color:var(--ink-soft);font-size:14px;line-height:1.85;margin-bottom:26px}
.field{display:flex;gap:10px;align-items:stretch}
.field input{flex:1;min-width:0;height:44px;padding:0 14px;background:var(--paper);
  border:1px solid var(--line);font-family:var(--mono);font-size:13px;letter-spacing:.08em}
.field input::placeholder{color:var(--muted);letter-spacing:.16em}
.gate__err{color:var(--alert);margin-top:14px;letter-spacing:.06em;text-transform:none;
  font-family:var(--sans);font-size:12.5px;white-space:pre-wrap}

/* 按钮：悬停时黑色自左填满 --------------------------------------------- */
.btn{position:relative;display:inline-flex;align-items:center;justify-content:center;height:44px;
  padding:0 26px;border:1px solid var(--line);border-radius:999px;background:var(--paper);
  color:var(--ink);font-size:13.5px;white-space:nowrap;overflow:hidden;cursor:pointer;
  transition:color .45s var(--ease)}
.btn>span{position:relative;z-index:1}
.btn::before{content:'';position:absolute;inset:0;z-index:0;background:var(--ink);
  transform:translateX(-101%);transition:transform .55s var(--ease)}
.btn:hover::before,.btn:focus-visible::before{transform:translateX(0)}
.btn:hover,.btn:focus-visible{color:var(--paper)}
.btn--solid{background:var(--ink);color:var(--paper)}
.btn--solid::before{background:var(--paper)}
.btn--solid:hover,.btn--solid:focus-visible{color:var(--ink)}

/* 首屏数据带 ------------------------------------------------------------ */
.hero{padding:clamp(30px,4.4vw,64px) 0 clamp(24px,3vw,44px)}
.hero__lead{color:var(--muted);padding-bottom:16px;border-bottom:1px solid var(--line)}
.hero__grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(216px,1fr));
  gap:0 clamp(18px,2.4vw,44px);margin-top:clamp(18px,2.6vw,34px)}
.stat{padding:16px 0;border-bottom:1px solid var(--line-soft)}
.stat__k{color:var(--muted);margin-bottom:8px}
.stat__v{font-family:var(--mono);font-size:clamp(19px,2.5vw,29px);font-weight:500;
  letter-spacing:-.01em;line-height:1.15;word-break:break-all}
.stat__x{color:var(--muted);font-size:12px;margin-top:6px}

/* 章节标题 -------------------------------------------------------------- */
.sec{margin:clamp(34px,4.6vw,66px) 0 0}
.sec-head{display:flex;align-items:baseline;gap:16px;padding-bottom:14px;
  border-bottom:1px solid var(--line);margin-bottom:clamp(20px,2.6vw,34px)}
.sec-head__num{color:var(--muted)}
.sec-head__title{font-size:clamp(19px,2vw,26px);font-weight:600;letter-spacing:-.015em;line-height:1.1}
.sec-head__en{margin-left:auto;color:var(--muted);white-space:nowrap}
.sec__grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:clamp(14px,1.8vw,24px)}

/* 面板：1px 描边、无圆角、表头等宽大写 --------------------------------- */
.panel{border:1px solid var(--line);background:var(--paper);display:flex;flex-direction:column}
.panel__hd{display:flex;align-items:center;gap:10px;padding:11px 16px;color:var(--ink);
  border-bottom:1px solid var(--line);background:var(--paper-2)}
.panel__hd em{margin-left:auto;font-style:normal;color:var(--muted)}
.panel__bd{padding:6px 16px 14px}
.row{display:flex;align-items:baseline;justify-content:space-between;gap:16px;
  padding:9px 0;border-bottom:1px solid var(--line-soft)}
.row:last-child{border-bottom:0}
.row__k{color:var(--ink-soft);font-size:13.5px}
.row__v{font-family:var(--mono);font-size:13px;text-align:right;word-break:break-all}
.row__v--muted{color:var(--muted)}
.row--stack{display:block}
.row--stack .row__v{text-align:left;margin-top:4px;color:var(--muted);font-size:11.5px}

/* 状态标记：黑白两色表达，不用红绿 ------------------------------------- */
.chip{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;
  padding:1px 9px;font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
  white-space:nowrap}
.chip--ink{background:var(--ink);color:var(--paper)}
.chip--hatch{background-image:repeating-linear-gradient(45deg,var(--ink) 0 1px,transparent 1px 4px)}
.chip--alert{color:var(--alert);border-color:var(--alert)}

/* 进度：8px 描边条 ------------------------------------------------------ */
.bar{position:relative;height:8px;border:1px solid var(--line);margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--ink);transition:width .9s var(--ease)}
.bar.is-over i{background-image:repeating-linear-gradient(45deg,var(--ink) 0 1px,transparent 1px 4px)}

/* 表格：细线、行悬停 ---------------------------------------------------- */
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);font-weight:400;text-align:left;padding:8px 8px 8px 0;border-bottom:1px solid var(--line)}
.tbl td{padding:9px 8px 9px 0;border-bottom:1px solid var(--line-soft);
  font-family:var(--mono);font-size:12px;vertical-align:top}
.tbl tr:last-child td{border-bottom:0}
.tbl tbody tr{transition:background-color .35s var(--ease)}
.tbl tbody tr:hover{background:rgba(17,17,17,.028)}
.tbl .num{text-align:right;white-space:nowrap}
.tbl .kind{font-family:var(--sans);font-size:13px}

/* 事件流 ---------------------------------------------------------------- */
.ev{color:var(--muted);font-size:11.5px;font-family:var(--mono);
  max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* 错误块 ---------------------------------------------------------------- */
.errs{color:var(--alert);font-family:var(--mono);font-size:11.5px;letter-spacing:.04em;white-space:pre-wrap}
.foot{padding:clamp(26px,3.4vw,48px) 0 clamp(34px,5vw,72px);color:var(--muted);
  border-top:1px solid var(--line);margin-top:clamp(34px,4.6vw,66px)}

@media (max-width:640px){
  .hdr__sub{display:none}
  .sec-head__en{display:none}
  .hero__grid{grid-template-columns:1fr 1fr}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;transition-duration:.001ms!important}
}
</style>
</head>
<body>

<header class="hdr">
  <div class="wrap hdr__in">
    <div class="hdr__brand">
      <span class="hdr__mark">&#9670;</span>
      <span class="hdr__name">AGENT-DEMO</span>
      <span class="mono hdr__sub">CONTROL / READ-ONLY OVERVIEW</span>
    </div>
    <div class="hdr__right mono" id="hdrStatus">&mdash;</div>
  </div>
</header>
<div class="hatch"></div>

<div id="login" class="gate">
  <div class="gate__box">
    <div class="mono gate__kicker">RESTRICTED / READ-ONLY</div>
    <h1 class="gate__title">agent-demo 总览</h1>
    <p class="gate__lead">输入 <span class="mono">AGENT_WEB_TOKEN</span> 进入。token 只存在当前标签页
      （sessionStorage），不进 URL、不落盘；也可以在地址后加
      <span class="mono">#token=&lt;值&gt;</span>，页面读到手即从地址栏抹除。</p>
    <div class="field">
      <input id="tok" type="password" placeholder="TOKEN" autocomplete="off" spellcheck="false">
      <button class="btn btn--solid" onclick="save()"><span>进入</span></button>
    </div>
    <div id="lerr" class="gate__err"></div>
  </div>
</div>

<main id="app" class="wrap" hidden>
  <section class="hero">
    <div class="mono hero__lead">LIVE STATUS</div>
    <div class="hero__grid" id="hero"></div>
  </section>
  <div class="hatch"></div>
  <div id="sections"></div>
  <div class="foot mono">只读视图 &middot; 每 5s 刷新 &middot; <span id="ts">&mdash;</span></div>
</main>

<script>
const KEY = "agent_web_token";
function tok() { return sessionStorage.getItem(KEY) || ""; }
function save() {
  sessionStorage.setItem(KEY, document.getElementById("tok").value.trim());
  load();
}
// 支持 #token=xxx：fragment 不会发给服务器（不进反代/访问日志，比 ?token= 安全），
// 读到手就收进 sessionStorage 并立刻从地址栏抹掉，避免 token 留在历史/截屏里。
(function () {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    sessionStorage.setItem(KEY, decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname + location.search);
  }
})();

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function num(n) { return (n === null || n === undefined) ? "—" : Number(n).toLocaleString(); }
function pct(a, b) { return (b > 0) ? Math.min(100, Math.round(a / b * 100)) : 0; }
function row(k, v, cls) {
  return '<div class="row"><span class="row__k">' + k + '</span><span class="row__v ' + (cls || "") + '">' + v + "</span></div>";
}
function chip(t, kind) { return '<span class="chip ' + (kind || "") + '">' + t + "</span>"; }
function panel(label, note, inner) {
  return '<div class="panel"><div class="panel__hd mono">' + label +
    (note ? "<em>" + esc(note) + "</em>" : "") + '</div><div class="panel__bd">' + inner + "</div></div>";
}
function secHead(n, title, en) {
  return '<div class="sec-head"><span class="mono sec-head__num">' + n + '</span>' +
    '<span class="sec-head__title">' + title + '</span>' +
    '<span class="mono sec-head__en">' + en + "</span></div>";
}
function stat(k, v, x) {
  return '<div class="stat"><div class="mono stat__k">' + k +
    '</div><div class="stat__v">' + v + "</div>" + (x ? '<div class="stat__x">' + x + "</div>" : "") + "</div>";
}

async function load() {
  let d;
  try {
    const r = await fetch("api/overview", { headers: { "Authorization": "Bearer " + tok() } });
    if (r.status === 401 || r.status === 403) {
      document.getElementById("login").hidden = false;
      document.getElementById("app").hidden = true;
      document.getElementById("lerr").textContent =
        r.status === 403 ? "来源 IP 不在允许列表内。" : "token 不正确。";
      return;
    }
    d = await r.json();
  } catch (e) {
    document.getElementById("lerr").textContent = "请求失败：" + e;
    return;
  }
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
  const when = new Date(d.generated_at * 1000);
  document.getElementById("ts").textContent = when.toLocaleString();

  const l = d.llm || {};
  const degraded = l.line === "fallback";
  const online = (d.bots || []).length > 0;
  document.getElementById("hdrStatus").innerHTML =
    '<span class="dot ' + (online ? "dot--on" : "dot--off") + '"></span>' +
    (online ? "ONLINE" : "OFFLINE") + " &nbsp;/&nbsp; " + when.toLocaleTimeString();

  /* ---- 首屏数据带 ---- */
  const b = d.budget || {}, t = (b.today || {}), h = b.history || {};
  const used = t.total || 0, cap = b.daily_tokens || 0;
  let hero = "";
  hero += stat("当前模型", esc(l.active || "—"),
    degraded ? chip("备用线路", "chip--hatch") : chip("主线路", "chip--ink"));
  hero += stat("今日对话", num(used) + ' <span class="mono" style="font-size:12px;color:var(--muted)">tok</span>',
    cap ? num(cap) + " 上限 · " + pct(used, cap) + "%" + (b.enforce ? " · 硬闸" : " · 软闸") : "未设上限");
  hero += stat("今日请求", num(t.chat_requests),
    b.cost_today === null || b.cost_today === undefined ? "未配单价" : "≈ " + Number(b.cost_today).toFixed(2) + " 元");
  hero += stat("协议端", online ? (d.bots || []).length + ' <span class="mono" style="font-size:12px;color:var(--muted)">个</span>' : "0",
    online ? "reverse-WS 已连接" : "reverse-WS 未连接");
  document.getElementById("hero").innerHTML = hero;

  /* ---- 01 运行状态 ---- */
  let s1 = "";
  s1 += panel("LLM 线路",
    degraded ? "DEGRADED" : "PRIMARY",
    row("当前生效", '<span class="mono">' + esc(l.active || "—") + "</span>") +
    row("线路", degraded ? chip("备用 · 主模型不可用", "chip--alert") : chip("主线路", "chip--ink")) +
    row("主模型", '<span class="mono">' + esc(l.primary || "—") + "</span>") +
    row("备用模型", l.fallback ? '<span class="mono">' + esc(l.fallback) + "</span>" : "未配置", "row__v--muted"));
  if (cap) {
    const p = pct(used, cap);
    s1 += panel("今日预算", p + "%",
      row("已用", num(used) + " / " + num(cap)) +
      row("闸门", b.enforce ? chip("硬闸 · 超限拦截", "chip--hatch") : "软闸 · 仅告警") +
      '<div class="bar' + (p >= 100 ? " is-over" : "") + '"><i style="width:' + p + '%"></i></div>');
  }
  s1 += panel("运行组件", null,
    row("记忆后端", '<span class="mono">' + esc(d.memory_backend || "未初始化") + "</span>") +
    row("默认人格", esc(d.persona_default || "（无）")) +
    row("已装 skill", num((d.skills || []).length)) +
    (d.flags ? row("群上下文", d.flags.group_context ? chip("开启", "chip--ink") : "关闭") +
      row("vision", d.flags.vision ? chip("开启", "chip--ink") : "关闭") : ""));
  s1 += panel("协议端", online ? "ONLINE" : "OFFLINE",
    online ? (d.bots || []).map(x => row("bot " + esc(x.self_id), chip("已连接", "chip--ink"))).join("")
      : row("状态", chip("未连接", "chip--alert")));

  /* ---- 02 用量 ---- */
  let s2 = "";
  s2 += panel("今日构成", t.date || null,
    row("prompt", num(t.prompt)) +
    row("completion", num(t.completion)) +
    row("合计", num(t.total)) +
    row("embedding", num(t.embedding_tokens) + " tok / " + num(t.embedding_requests) + " 次", "row__v--muted"));
  s2 += panel("历史累计",
    h.chat_requests ? num(h.chat_requests) + " 次请求" : null,
    row("对话 token", num(h.total)) +
    row("prompt / completion", num(h.prompt) + " / " + num(h.completion)) +
    row("embedding token", num(h.embedding_tokens), "row__v--muted"));
  const models = Object.entries(t.by_model || {})
    .sort((a, c) => (c[1].prompt + c[1].completion) - (a[1].prompt + a[1].completion));
  s2 += panel("今日按模型", models.length ? models.length + " 个" : null,
    models.length
      ? '<table class="tbl"><thead><tr><th>model</th><th class="num">prompt</th>' +
        '<th class="num">completion</th><th class="num">req</th></tr></thead><tbody>' +
        models.map(([m, v]) => "<tr><td>" + esc(m) + '</td><td class="num">' + num(v.prompt) +
          '</td><td class="num">' + num(v.completion) + '</td><td class="num">' + num(v.requests) +
          "</td></tr>").join("") + "</tbody></table>"
      : '<div class="row row__v--muted">今日暂无调用</div>');
  const routes = Object.entries(t.by_route || {})
    .sort((a, c) => (c[1].prompt + c[1].completion) - (a[1].prompt + a[1].completion)).slice(0, 8);
  s2 += panel("今日按路由", routes.length ? "TOP " + routes.length : null,
    routes.length
      ? '<table class="tbl"><thead><tr><th>route</th><th class="num">tok</th>' +
        '<th class="num">req</th></tr></thead><tbody>' +
        routes.map(([k, v]) => "<tr><td>" + esc(k) + '</td><td class="num">' +
          num(v.prompt + v.completion) + '</td><td class="num">' + num(v.requests) +
          "</td></tr>").join("") + "</tbody></table>"
      : '<div class="row row__v--muted">今日暂无调用</div>');

  /* ---- 03 知识库 ---- */
  let s3 = "";
  if (d.kb) {
    const ks = d.kb.stats || {}, kd = d.kb.describe || {};
    s3 += panel("知识库", kd.enabled ? "ENABLED" : "DISABLED",
      row("状态", kd.enabled ? chip("启用", "chip--ink") : "关闭") +
      row("chunks", num(ks.chunks)) +
      row("来源数", num(ks.sources)) +
      row("top_k / 阈值", num(kd.top_k) + " / " + (kd.threshold === null || kd.threshold === undefined ? "—" : kd.threshold)) +
      row("向量", kd.embedding === "on" ? chip("开启", "chip--ink") : "关闭") +
      row("蒸馏 cron", '<span class="mono">' + esc(kd.digest_cron || "—") + "</span>", "row__v--muted"));
  } else {
    s3 += panel("知识库", null, '<div class="row row__v--muted">未初始化</div>');
  }
  const skills = d.skills || [];
  const pub = skills.filter(s => s.permission === "public").length;
  s3 += panel("已注册 skill", skills.length + " 个",
    row("public", num(pub)) +
    row("非 public", num(skills.length - pub)) +
    row("清单", skills.map(s => '<span class="mono">' + esc(s.name) + "</span>").join(" &middot; "), "row--stack"));

  /* ---- 04 事件 ---- */
  const evs = d.recent_events || [];
  let s4 = panel("最近事件", evs.length ? evs.length + " 条" : null,
    evs.length
      ? '<table class="tbl"><thead><tr><th>time</th><th>kind</th><th>detail</th></tr></thead><tbody>' +
        evs.map(e => {
          const detail = Object.fromEntries(Object.entries(e).filter(([k]) => k !== "ts" && k !== "kind"));
          return "<tr><td>" + new Date(e.ts * 1000).toLocaleTimeString() + "</td>" +
            '<td class="kind">' + esc(e.kind) + "</td>" +
            '<td><div class="ev" title="' + esc(JSON.stringify(detail)) + '">' +
            esc(JSON.stringify(detail)) + "</div></td></tr>";
        }).join("") + "</tbody></table>"
      : '<div class="row row__v--muted">暂无事件</div>');
  if (d.errors && d.errors.length) {
    s4 += panel("取数失败的部分", d.errors.length + " 项",
      '<div class="errs">' + d.errors.map(esc).join("\n") + "</div>");
  }

  document.getElementById("sections").innerHTML =
    '<section class="sec">' + secHead("01", "运行状态", "RUNTIME") +
      '<div class="sec__grid">' + s1 + "</div></section>" +
    '<section class="sec">' + secHead("02", "用量与成本", "USAGE") +
      '<div class="sec__grid">' + s2 + "</div></section>" +
    '<section class="sec">' + secHead("03", "知识与技能", "KNOWLEDGE") +
      '<div class="sec__grid">' + s3 + "</div></section>" +
    '<section class="sec">' + secHead("04", "事件", "EVENTS") +
      '<div class="sec__grid">' + s4 + "</div></section>";
}
load();
setInterval(load, 5000);
</script>
</body>
</html>"""


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
