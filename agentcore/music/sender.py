"""OneBot HTTP 发送语音消息段。

走 HTTP 而不是 ``MessageSegment``：这样 ``agentcore/music/`` 全程不碰 nonebot，
点歌路由因此可以被**条件导入**——没配 API / HTTP / 依赖时整个模块不加载，对核心零影响。
若改用 ``MessageSegment`` 就得落在 ``plugins/`` 里并常驻注册。

已知限制（记为取舍，不修）：HTTP 发送时用哪个账号由 ``NAPCAT_HTTP_URL`` 指向的
协议端决定，**无法指定"触发本次请求的那个 bot"**。单账号部署无影响。
"""

from __future__ import annotations

import base64
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


# 防御性上限：正常 300 秒歌约 530 KB silk / 710 KB base64；超限直接拒发
_MAX_SILK_BASE64_BYTES = 8 * 1024 * 1024  # 8 MB
# 三态与 outbound._try_forward 同一套纪律：只有 FAILED 才允许调用方降级重发，
# UNCERTAIN 一律不重发——请求可能已经抵达实现并发送成功，只是响应丢了
SEND_OK = "ok"
SEND_FAILED = "failed"
SEND_UNCERTAIN = "uncertain"


def onebot_http_url() -> str:
    return (os.getenv("NAPCAT_HTTP_URL") or "").strip().rstrip("/")


def onebot_http_token() -> str:
    return (os.getenv("NAPCAT_HTTP_TOKEN") or "").strip()


def http_configured() -> bool:
    return bool(onebot_http_url())


def _is_uncertain(err: BaseException) -> bool:
    """复用全仓唯一判据（只 import 不修改），避免两处规则各自漂移。

    延迟导入：``agentcore.skills.file_sender`` 对 nonebot 做受保护的可选导入，
    函数内取用可让本模块在 nonebot 未初始化时也能被导入。
    """
    from agentcore.skills.file_sender import is_uncertain_send_error

    return is_uncertain_send_error(err)


async def send_group_voice(group_id: int, silk: bytes, *, label: str = "") -> str:
    """把 silk 字节作为群语音发送，返回 ``SEND_OK`` / ``SEND_FAILED`` / ``SEND_UNCERTAIN``。

    ``file`` 必须是 ``base64://`` 前缀，**不能用** ``data:audio/...;base64,``：
    后者会被实现侧当成本地文件路径去 stat，报 ``ENAMETOOLONG``（SnowLuma issue #236）。
    """
    # 没配地址就明确失败：music_route 的注册闸门正常时走不到这里，
    # 但防御住"env 被运行中清空"这类边缘态，别把请求打到无意义的地方
    if not onebot_http_url():
        logger.warning("群语音发送失败：NAPCAT_HTTP_URL 未配置")
        return SEND_FAILED
    url = f"{onebot_http_url()}/send_group_msg"
    encoded = base64.b64encode(silk).decode("ascii")
    if len(encoded) > _MAX_SILK_BASE64_BYTES:
        logger.error(
            "群语音发送失败：silk 过大（base64=%d 字节，上限 %d）",
            len(encoded),
            _MAX_SILK_BASE64_BYTES,
        )
        return SEND_FAILED
    payload = {
        "group_id": int(group_id),
        "message": [
            {
                "type": "record",
                "data": {"file": f"base64://{encoded}"},
            }
        ],
    }
    headers = {"Content-Type": "application/json"}
    token = onebot_http_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception as e:
                logger.error(
                    "群语音发送响应不可解析：group=%s err=%s —— 可能已送达，不再重发",
                    group_id,
                    e,
                    exc_info=True,
                )
                return SEND_UNCERTAIN
    except Exception as e:
        if _is_uncertain(e):
            # 超时/断连：请求可能已经送达，绝不重发，否则同一条语音到用户手里两遍
            logger.error(
                "群语音发送结果未确认：group=%s err=%s —— 可能已送达，不再重发",
                group_id,
                e,
                exc_info=True,
            )
            return SEND_UNCERTAIN
        logger.warning("群语音发送失败：group=%s err=%s", group_id, e, exc_info=True)
        return SEND_FAILED

    if not isinstance(data, dict):
        logger.warning(
            "群语音发送返回非预期结构：group=%s data=%r", group_id, str(data)[:200]
        )
        return SEND_FAILED
    message_id = data.get("message_id")
    if str(data.get("status")) == "ok" or message_id:
        # 记 message_id 与实际载荷大小：上游已知语音上传失败会误报成功
        # （SnowLuma issue #422），这两项是事后排查"用户说没收到"的唯一线索
        logger.info(
            "群语音已发送：group=%s msg_id=%s silk=%d 字节%s",
            group_id,
            message_id,
            len(silk),
            f" | {label}" if label else "",
        )
        return SEND_OK
    logger.warning(
        "群语音发送返回业务失败：group=%s data=%r", group_id, str(data)[:200]
    )
    return SEND_FAILED
