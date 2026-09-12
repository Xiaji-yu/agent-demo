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
