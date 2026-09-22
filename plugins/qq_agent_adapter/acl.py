import os


def _load_list(env_name: str):
    val = os.getenv(env_name, "")
    return {x.strip() for x in val.split(",") if x.strip()}


def _get_superusers() -> set[str]:
    try:
        from nonebot import get_driver

        return set(get_driver().config.superusers or [])
    except Exception:
        # 无 nonebot driver（测试/脚本）：与 workspace.utils 用同一套解析，避免双源漂移
        from agentcore.workspace.utils import load_superusers

        return load_superusers()


ALLOWED_GROUPS = _load_list("ALLOWED_GROUPS")


def is_allowed(event) -> bool:
    uid = str(event.get_user_id())
    superusers = _get_superusers()
    # superuser 全开
    if superusers and uid in superusers:
        return True
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
    """
    uid = str(event.get_user_id())
    if not uid or uid not in _get_superusers():
        return False
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        return True
    return bool(ALLOWED_GROUPS) and str(group_id) in ALLOWED_GROUPS
