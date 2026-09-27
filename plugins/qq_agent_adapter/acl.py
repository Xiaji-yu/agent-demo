import logging
import os

logger = logging.getLogger(__name__)


def _load_list(env_name: str):
    val = os.getenv(env_name, "")
    return {x.strip() for x in val.split(",") if x.strip()}


def _load_blocked_users() -> set[str]:
    """黑名单额外做一次 ASCII 数字校验：QQ 号必须 `isascii() and isdigit()`。

    全角数字（`１２３`）之类写错的条目永远无法命中真实 user_id，等于黑名单
    静默失效——这是 fail-open 方向的配置错误，必须在启动时喊出来（AGENTS §5
    「全角数字通过 isdigit」坑）。
    """
    entries = _load_list("BLOCKED_USERS")
    for entry in entries:
        if not (entry.isascii() and entry.isdigit()):
            logger.warning(
                "[acl] BLOCKED_USERS 含非 QQ 号条目 %r（永远无法命中），请检查拼写",
                entry,
            )
    return entries


def _get_superusers() -> set[str]:
    try:
        from nonebot import get_driver

        return set(get_driver().config.superusers or [])
    except Exception:
        # 无 nonebot driver（测试/脚本）：与 workspace.utils 用同一套解析，避免双源漂移
        from agentcore.workspace.utils import load_superusers

        return load_superusers()


ALLOWED_GROUPS = _load_list("ALLOWED_GROUPS")
BLOCKED_USERS = _load_blocked_users()


def is_blocked(event) -> bool:
    """黑名单命中判定（BLOCKED_USERS，用户级）。

    **superuser 豁免**：主人永远可用，避免配置/运行期误拉黑把自己锁死
    （2026-09 与用户确认的优先级语义）。空名单 = 不启用，行为与旧版完全一致。
    """
    uid = str(event.get_user_id() or "")
    if not uid or uid in _get_superusers():
        return False
    return uid in BLOCKED_USERS


def is_allowed(event) -> bool:
    uid = str(event.get_user_id())
    superusers = _get_superusers()
    # superuser 全开（也在黑名单之上：见 is_blocked 的豁免语义）
    if superusers and uid in superusers:
        return True
    # 用户黑名单：命中即拒（群聊/私聊都挡）。放在 superuser 判断之后，
    # 所以这里只可能拦到普通用户；白名单群里其他人不受影响
    if uid and uid in BLOCKED_USERS:
        return False
    # 群聊：仅白名单群允许（空集合表示没有群允许）
    # 注意用 `is not None` 而不是 hasattr：部分适配器/测试替身的私聊事件也带
    # group_id 属性（值为 None），用 hasattr 会把私聊误判成群聊分支
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        return bool(ALLOWED_GROUPS) and str(group_id) in ALLOWED_GROUPS
    # 私聊：仅 superuser 允许（空集合表示没有私聊允许）
    return False


def is_superuser_id(user_id: str) -> bool:
    """按 user_id 判定是否 superuser（无 event 对象的调用方用，如 skill handler）。

    与 ``is_allowed`` 的私聊分支同一判据：空集合 = 谁都不是。点歌 skill 在私聊
    入口用它做 ACL（主聊天路径本就会拦，这里是纵深防御）。
    """
    uid = str(user_id or "")
    if not uid:
        return False
    return uid in _get_superusers()


def is_strict_allowed(event) -> bool:
    """「会暴露本机信息」的入口判据：必须是 superuser，且群聊还须在白名单群里。

    与 :func:`is_allowed` 的差别：``is_allowed`` 对 superuser **无条件**放行（不看
    群），因为那只影响他自己的对话；戳一戳回的是整机概览（主机名、最忙进程命令行、
    服务与端口），superuser 在一个没授权的群里被戳不该把机器信息发出去。所以这里
    额外要求 ``group_id`` 命中 ``ALLOWED_GROUPS``。私聊只要 superuser 即可。

    空 superuser 集合 = 谁都不是（fail-closed，与 :func:`is_allowed` 一致）。

    黑名单在这里是 no-op：本判据已要求 superuser，而 superuser 对黑名单豁免。
    """
    uid = str(event.get_user_id())
    if not uid or uid not in _get_superusers():
        return False
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        return True
    return bool(ALLOWED_GROUPS) and str(group_id) in ALLOWED_GROUPS


async def deny(matcher, event, message: str) -> None:
    """``is_allowed`` 拒绝后的统一出口（主聊天路径与 admin 命令共用）。

    - 命中黑名单：**静默**——不回复任何内容，只记 WARNING 日志。不告诉对方
      他被拉黑（避免激化，也不给探测者确认信号）。
    - 其余越界（不在白名单群 / 非 superuser 私聊）：维持调用方原有文案明说。

    matcher.finish 的 FinishedException（nonebot 流程控制）必须原样抛出，
    由框架收尾——本函数不捕获。
    """
    if is_blocked(event):
        group_id = getattr(event, "group_id", None)
        logger.warning(
            "[acl] 黑名单命中 user=%s group=%s，已静默拒绝",
            event.get_user_id(),
            group_id,
        )
        await matcher.finish()
    await matcher.finish(message)
