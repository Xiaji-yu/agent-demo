# FIX：P0 权限缺口修复 + Agent 权限三级收束（low/medium/high）

日期：2026-10-05　｜　性质：High（安全边界）＋ Medium（局部契约）
决策：管理员两轮确认（本文件 §0 记录决策点）；实施按 AC 先行。

---

## 0. 决策记录（管理员拍板）

| 决策点 | 结论 |
|---|---|
| P0（registry 层权限被通配符击穿） | 随本次一并修 |
| `AGENT_PERMISSION_LEVEL` 缺省值 | **medium**（fail-closed 由身份轴承担：SUPERUSERS 空 = 无人可用） |
| ssh_run / db_query 分档 | ssh_run=low，db_query=medium |
| proc_kill 两段确认 | 保留 |
| service_ctrl / docker_ctrl 二次确认 | **不加**；但保留**自保护**（拒 bot 自身 unit 与 `PG_CONTAINER`） |
| high 的任意命令 | **激进版**：独立新技能 `run_shell`（bash -c） |
| fs_delete 聊天确认码 | 保留 |
| 参数/范围键（LOG_ALLOWLIST、KILL_CONFIRM_TTL、BUILD_TIMEOUT、DB_*） | 全部保留 |
| config.yaml `skills.permissions` 段 | **整段移除** |
| 级别键进 web 写面 | **不进**（安全边界键走 SSH） |
| `system_status` | public → **管理员专属**（收编） |

---

## 1. P0：registry 层 superuser 门被通配符击穿

**现象（复现证据，verified）**：`config.yaml` 配了 `skills.permissions.superusers:
["*"]`，而插件启动把它并进 `PermissionChecker`（`permissions.py` 对 `"*"` 的语义
是「所有人都是 superuser」）。叠加 ops/action/db 三类 handler **没有** env
SUPERUSERS 二次校验（只有 fs_*/run_command/ssh_run 有），白名单群任意成员即可：

```
$ .venv/bin/python - <<EOF   # 模拟 __init__.py 的 checker 构造 + registry 执行
路人可见的 superuser 技能: ['log_tail', 'db_query']
路人执行 log_tail 结果: SENSITIVE-LOG-CONTENT
EOF
```

当时部署实况（.env 实测）：`ALLOWED_GROUPS` 5 个群、`DATABASE_URL` 生产库、
`AGENT_LOG_ALLOWLIST=/var/log`——即群友**当时就能**读 /var/log、只读查生产库、
收集主机信息；且一旦日后填了目标白名单，动作类会立刻对全员开放。

**修法**：
1. `config.yaml` 删 `skills.permissions` 段；`__init__.py` 不再合并 yaml
   superusers（checker 只吃 NoneBot config = `.env SUPERUSERS`）。
2. `ops_skills`(5)/`action_skills`(6)/`db_skills`(1) handler 补 `is_superuser`
   校验（与 fs/ssh 对齐的纵深）；`system_status` 收编 superuser。
3. registry 对无权限者返回与 unknown skill **同文案**（「直接忽略」语义，
   消除探测口）。engine 的 M2 防重试不受影响——它识别的是 handler 层
   「无权限」文案与自身的短路回包。

**回归锁**：`test_permissions.py::test_config_yaml_has_no_permissions_section`、
`test_registry.py::test_execute_permission_denied_indistinguishable_from_unknown`、
`test_tools.py::TestOpsHandlerAdminGate`、`test_action_skills.py::TestHandlerAdminGate`、
`test_db_skills.py::TestHandlerAdminGate`、`test_system_status.py::TestAdminOnly`、
`test_workspace_skills.py::TestExecutionACL::test_handler_gate_even_if_registry_bypassed`。

**变异复核（verified）**：废掉 log_tail handler 门 →
`TestOpsHandlerAdminGate` 1 failed；registry 文案改回 permission denied →
4 failed。还原后全绿。

---

## 2. 权限三级收束（AGENT_PERMISSION_LEVEL）

**模型**：身份轴（只对 SUPERUSERS 开放服务器操作类技能，普通用户任何级别都
schema 不可见、调用得 unknown）× 级别轴（只调节管理员上限）：

| 级别 | 能力 |
|---|---|
| low | ops 查询、docker_ps/logs、fs_list/read、run_command **只读子集**（`readonly_only` 拒 zip/unzip/tar/curl）、ssh_run、system_status |
| medium（缺省） | low + fs_write/mkdir/delete（删除保留确认码）+ run_command 全白名单 + service_ctrl/docker_ctrl + proc_kill（两段确认）+ run_build_script + db_query |
| high | medium + `run_shell`（bash -c） |

**关键实现**：
- `agentcore/skills/levels.py`：单点判定（缺省 medium；脏值 WARNING 一次后回退）。
- 注册期过滤（`builtin.py` 按 `at_least` 决定注册到哪档；low 档高阶技能 schema
  不可见）+ handler 内 `at_least` 复查（双层）。
- runner `permitted(..., readonly_only=)`：low 档收掉 zip/unzip/tar/curl。
- 自保护（`action_skills.py`）：`_self_unit()`/`_self_container_id()` 从
  `/proc/self/cgroup` 识别「自己」（零新配置；裸 nohup/非容器部署识别不到则无
  此保护，README 已注明）；`docker_ctrl` 另拒 `PG_CONTAINER`。
- `run_shell`（新 `shell_skill.py`）：三层闸（registry superuser / handler
  is_superuser / at_least high）；**最小环境执行**（`runner._minimal_env`——不继承
  LLM_API_KEY 等，`echo $LLM_API_KEY` 拿不到密钥）；120s 超时 `os.killpg` 杀整个
  进程组；输出 8KB/4000 字符截断；审计日志带 uid + 命令全文；不进只读并行名单。
- **从 X 改为 Y**（历史行为变更，理由=单人自用部署中目标白名单维护成本 > 收益，
  管理员知情决策）：目标白名单四键移除 → medium 起任意 unit/容器可控（自保护
  除外）；proc_kill 的 pattern 白名单移除（防线=两段确认+pid1/自身排除+10 个
  上限）；`run_build_script` 从「白名单点名」放宽为「工作区 scripts/ 全部可选」。

**回归锁**：`tests/test_levels.py`（新）、`test_runner_security.py::TestReadOnlyMode`、
`test_action_skills.py`（重写：级别门/自保护/cgroup 解析）、
`test_workspace_skills.py::TestLevelGating`、`test_tools.py::TestLevelRegistration`、
`tests/test_shell_skill.py`（新：三层闸/最小环境无 key 泄漏/超时杀进程组/截断/审计）。

**变异复核（verified）**：
- `at_least` 恒真 → 5 failed；
- `readonly_only` 检查失效 → 5 failed；
- service 自保护失效 → 1 failed；
- run_shell 改继承完整环境 → key 泄漏用例 1 failed。
还原后全绿。

---

## 3. 验证命令与结果（verified，2026-10-05）

```bash
# 默认套件
.venv/bin/python -m pytest -q
# → 2000 collected；1950 passed + 50 skipped

# 带 PG 的完整套件（qqagent_test，未触碰生产库）
TEST_DATABASE_URL="postgresql://xiaji:xiaji@127.0.0.1:5432/qqagent_test" \
  .venv/bin/python -m pytest -q
# → 1991 passed + 9 skipped

# Lint / 格式（ruff 0.9.6，与 pyproject pin 一致）
.venv/bin/python -m ruff check agentcore plugins tests bot.py scripts      # All checks passed!
.venv/bin/python -m ruff format --check agentcore plugins tests bot.py scripts  # 156 files already formatted
```

部署迁移说明：原四个白名单全空的部署 ≈ low；升级后写
`AGENT_PERMISSION_LEVEL=medium` 即获得「重启服务/改文件」全套。行为变化：
白名单群普通用户失去 log_tail/db_query/proc_* 等（修复性收缩）；`system_status`
转管理员专属；级别变更改 .env + 重启，web 设置页不含该键。

## 4. 剩余项 / 已知边界

- `run_shell` 是「无任意命令」不变量的唯一显式例外（AGENTS.md §4 已记录）：
  底线仅超时/截断/审计三道，误操作不可回退——只应在 high 档由可信管理员使用。
- 自保护的 unit/容器识别依赖 systemd cgroup；容器部署可识别自身容器 id，
  裸 nohup 部署两者都识别不到（无自保护，README 已注明）。
- `AGENT_PERMISSION_LEVEL` 改动需重启生效；未纳入 web 写面（有意）。
- 本 FIX 落地时工作区尚有先前未提交的「dangerous-admin-actions」功能改动
  （action_skills/db_skills 等当时未入库）；本修复在其上叠加，git 基线为 `1e0f5ae`。
