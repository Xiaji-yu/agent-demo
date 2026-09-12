"""把现有 tools 注册为默认 skill。"""

import logging
import os

from agentcore.skills.basic_tools import register_basic_skills
from agentcore.skills.file_sender import register_file_skills
from agentcore.skills.info_skills import register_info_skills
from agentcore.skills.ops_skills import register_ops_skills
from agentcore.skills.registry import SkillRegistry
from agentcore.skills.search import create_search_skill
from agentcore.skills.system_status import register_system_skills
from agentcore.skills.utility_skills import register_utility_skills
from agentcore.skills.web_fetch import register_web_fetch_skill
from agentcore.skills.workspace_skills import register_workspace_skills

logger = logging.getLogger(__name__)


def register_builtin_skills(registry: SkillRegistry) -> None:
    # 基础工具：安全计算器 + 天气（原先散落在旧 tools registry，已收敛到 skills）
    register_basic_skills(registry)
    logger.info("Basic skills registered: calc, get_weather")

    # 网页抓取（含 SSRF 防护 + 不可信内容围栏）
    register_web_fetch_skill(registry)
    logger.info("Web fetch skill registered: fetch_url")

    # 实用工具：时间日期 / 单位换算 / 随机
    register_utility_skills(registry)
    logger.info("Utility skills registered: now, date_calc, unit_convert, random")

    # 信息类：网页摘要（翻译走 data/skills/translator.yaml 的 prompt skill）
    register_info_skills(registry)
    logger.info("Info skill registered: summarize_url")

    # 搜索 skill：若 .env 中配置了 SEARCH_API_KEY，则自动注册
    search_key = (os.getenv("SEARCH_API_KEY") or "").strip()
    logger.info(
        "Search config: provider=%s key_set=%s",
        os.getenv("SEARCH_PROVIDER"),
        bool(search_key),
    )
    if search_key:
        try:
            manifest, handler = create_search_skill()
            registry.install(manifest, handler=handler)
            logger.info("Search skill registered: %s", manifest.name)
            # 多源搜索聚合：一次问多个方面时用，避免模型来回调用
            from agentcore.skills.manifest import SkillManifest
            from agentcore.skills.search import search_multi

            registry.install(
                SkillManifest(
                    name="search_multi",
                    description="多路联网搜索：把 2~3 个查询并行搜索、去重合并后返回，"
                    "适合一个问题包含多个方面时一次搜完。",
                    type="tool",
                    parameters=[
                        {
                            "name": "queries",
                            "type": "array",
                            "description": "查询词列表（1~3 个）",
                        },
                        {
                            "name": "per_query",
                            "type": "integer",
                            "description": "每个查询取几条，默认 3",
                        },
                    ],
                    permission="public",
                ),
                handler=search_multi,
            )
            logger.info("Search skill registered: search_multi")
        except Exception:
            logger.exception("Skip search skill due to registration failure")

    # 文件发送 skill：默认注册，但真正发送依赖 NapCat HTTP 配置
    register_file_skills(registry)
    logger.info("File skill registered: send_markdown_file")

    # 主机状态查询 skill（只读、白名单命令）
    register_system_skills(registry)
    logger.info("System skill registered: system_status")

    # 运维类（仅管理员）：进程 / 磁盘 / 端口 / 服务 / 日志
    register_ops_skills(registry)
    logger.info(
        "Ops skills registered: proc_detail, disk_usage, port_check, service_status, log_tail"
    )

    # 工作区技能（fs_* / run_command / fs_delete 二次确认）
    register_workspace_skills(registry)
    logger.info(
        "Workspace skills registered: fs_list/read/write/mkdir/delete, run_command"
    )
