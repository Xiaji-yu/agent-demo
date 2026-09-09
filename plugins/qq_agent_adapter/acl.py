import os


def _load_list(env_name: str):
    val = os.getenv(env_name, "")
    return {x.strip() for x in val.split(",") if x.strip()}


SUPERUSERS = _load_list("SUPERUSERS")
ALLOWED_GROUPS = _load_list("ALLOWED_GROUPS")


def is_allowed(event) -> bool:
    uid = str(event.get_user_id())
    if uid in SUPERUSERS:
        return True
    if hasattr(event, "group_id"):
        if not ALLOWED_GROUPS:
            return True
        return str(event.group_id) in ALLOWED_GROUPS
    return True
