"""把现有 tools 注册为默认 skill。"""

import logging
import os

from agentcore.skills.action_skills import register_action_skills
from agentcore.skills.basic_tools import register_basic_skills
from agentcore.skills.db_skills import register_db_skills
from agentcore.skills.file_sender import register_file_skills
from agentcore.skills.info_skills import register_info_skills
from agentcore.skills.levels import at_least, current_level
from agentcore.skills.ops_skills import register_ops_skills
from agentcore.skills.registry import SkillRegistry
from agentcore.skills.search import create_search_skill
from agentcore.skills.shell_skill import register_shell_skill
from agentcore.skills.ssh_skill import register_ssh_skills
from agentcore.skills.system_status import register_system_skills
from agentcore.skills.utility_skills import register_utility_skills
from agentcore.skills.web_fetch import register_web_fetch_skill
from agentcore.skills.workspace_skills import register_workspace_skills

logger = logging.getLogger(__name__)


def register_builtin_skills(registry: SkillRegistry) -> None:
    # 权限级别（AGENT_PERMISSION_LEVEL，缺省 medium）：决定服务器操作类技能
    # 注册到哪一档。级别只调节 SUPERUSERS 的上限——普通用户任何级别都不可见。
    level = current_level()
    logger.info("Permission level: %s", level)

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

    # MediaWiki 在线直查（wiki_prts / wiki_blhx…）：站点表来自
    # agentcore/skills/wiki_lookup.py，AGENT_WIKI_SITES 选启用的站点（none 全关）。
    # 查询结果由引擎按不可信数据围栏（engine._result_needs_fence）。
    from agentcore.skills.wiki_lookup import register_wiki_skills

    wiki_names = register_wiki_skills(registry)
    if wiki_names:
        logger.info("Wiki skills registered: %s", ", ".join(wiki_names))
    else:
        logger.info("Wiki skills disabled (AGENT_WIKI_SITES=none)")

    # 文件发送 skill：默认注册，但真正发送依赖协议端 HTTP 配置
    register_file_skills(registry)
    logger.info("File skill registered: send_markdown_file")

    # 主机状态查询 skill（只读、白名单命令）
    register_system_skills(registry)
    logger.info("System skill registered: system_status")

    # 运维类（仅管理员，low+ 只读）：进程 / 磁盘 / 端口 / 服务 / 日志
    register_ops_skills(registry)
    logger.info(
        "Ops skills registered: proc_detail, disk_usage, port_check, service_status, log_tail"
    )

    # 工作区技能（fs_* / run_command）：内部按级别注册——low 只有
    # fs_list/fs_read + run_command（只读子集），medium+ 追加写入三件套
    register_workspace_skills(registry)
    logger.info(
        "Workspace skills registered: fs_list/read%s, run_command (level=%s)",
        "+write/mkdir/delete" if at_least("medium") else "",
        level,
    )

    # SSH 远程只读诊断（仅管理员，low+）：host/凭据全部来自 .env，未配置即整体关闭
    register_ssh_skills(registry)
    logger.info("SSH skill registered: ssh_run")

    # 管理员动作类（仅 superuser）：docker_ps/logs 只读 low+；
    # 服务/容器控制、两段确认杀进程、构建脚本 medium+（注册期过滤 + 函数内
    # 级别门双层）。自保护：拒 bot 自身 unit 与 DB 容器（见 action_skills docstring）。
    register_action_skills(registry)
    logger.info(
        "Action skills registered: docker_ps, docker_logs%s",
        ", service_ctrl, docker_ctrl, proc_kill, run_build_script"
        if at_least("medium")
        else "（low 档：动作类不注册）",
    )

    # 只读数据库查询（仅 superuser，medium+）：单条 SELECT/WITH + 只读事务兜底，
    # 未配置 DATABASE_URL 即整体关闭
    if at_least("medium"):
        register_db_skills(registry)
        logger.info("DB skill registered: db_query")
    else:
        logger.info("DB skill skipped (level=low)")

    # run_shell（仅 superuser，high 专属）：bash -c 任意命令，三层闸 + 最小环境
    if at_least("high"):
        register_shell_skill(registry)
        logger.info("Shell skill registered: run_shell")
    else:
        logger.info("Shell skill skipped (level<high)")

    # 只读工具白名单（审查 C3）：引擎只在「一步内的全部工具调用均为只读」时
    # 并行执行。这里是**唯一审计点**——新工具默认非只读（fail-closed），
    # 确认无副作用后再加入；play_music/send_markdown_file/fs_write 等
    # 副作用工具永远不进这个名单。
    registry.mark_read_only(
        "calc",
        "get_weather",
        "fetch_url",
        "summarize_url",
        "now",
        "date_calc",
        "unit_convert",
        "random",
        "search_web",
        "search_multi",
        "wiki_prts",
        "wiki_blhx",
        "system_status",
        "reminder_list",
        "fs_list",
        "fs_read",
        "proc_detail",
        "disk_usage",
        "port_check",
        "service_status",
        "log_tail",
        # 纯查询、无副作用的只读面（动作类 skill 一律 fail-closed 不进本名单：
        # service_ctrl/docker_ctrl/proc_kill/run_build_script 都有副作用）。
        # db_query 在只读事务内执行、无副作用，但结果要进 prompt——暂按 fail-closed
        # 口径**不进**本名单（连接池/时延与注入后果都更重），保持串行执行。
        "docker_ps",
        "docker_logs",
    )
