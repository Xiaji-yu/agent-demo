"""agent-demo 启动入口（独立运行）"""
import json
import logging
import logging.handlers
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 让 agentcore / 插件的 INFO 日志（LLM step、skill call、[msg]/[reply] 等）可见
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

# M7 日志归档：控制台之外按天落盘轮转（午夜切分、保留 N 天，过期自动清理）。
# 日志含聊天内容明文，data/logs/ 已 gitignore 绝不入库
_log_keep = int(os.getenv("AGENT_LOG_KEEP_DAYS", "14") or "0")
if _log_keep > 0:
    _log_dir = Path(os.getenv("AGENT_LOG_DIR", "data/logs"))
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.handlers.TimedRotatingFileHandler(
        _log_dir / "agent.log", when="midnight", backupCount=_log_keep, encoding="utf-8"
    )
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(_file_handler)

# NoneBot + pydantic v2 兼容：SUPERUSERS 期望 set[str]，
# 但纯数字 env var 会被推断为 int，这里提前转成 JSON 数组。
_raw_superusers = os.getenv("SUPERUSERS", "")
if _raw_superusers and not _raw_superusers.strip().startswith("["):
    _list = [x.strip() for x in _raw_superusers.split(",") if x.strip()]
    os.environ["SUPERUSERS"] = json.dumps(_list)

from nonebot import get_driver, init  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter  # noqa: E402

init()

driver = get_driver()
driver.register_adapter(OneBotV11Adapter)


@driver.on_shutdown
async def _close_agent():
    # 停机前把防抖窗口中未到期的消息立即执行，避免静默丢失
    try:
        import plugins.qq_agent_adapter.matcher as _matcher

        deb = _matcher.get_debouncer()
        if deb is not None:
            await deb.flush_all()
    except Exception:
        logging.getLogger(__name__).exception("debounce flush on shutdown failed")
    memory = getattr(driver, "_agent_memory", None)
    if memory is not None:
        await memory.aclose()
    # P1-6：回收共享 LLM httpx 连接池，避免反复启停/热重载累积未关闭连接
    try:
        from agentcore.skills.registry import close_shared_llm_client

        await close_shared_llm_client()
    except Exception:
        logging.getLogger(__name__).exception("close shared llm client failed")
    # L21：回收 search 技能的常驻 httpx 连接池（与 P1-6 同型的停机收尾）
    try:
        from agentcore.skills.search import aclose_search_client

        await aclose_search_client()
    except Exception:
        logging.getLogger(__name__).exception("close search client failed")


from nonebot import load_plugins  # noqa: E402

load_plugins("plugins")

if __name__ == "__main__":
    import nonebot

    nonebot.run()
