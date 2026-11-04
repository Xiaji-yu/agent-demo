# FIX — PR #17（1301348）评审轮补录：声称核对 + 流程销账

> **性质**：复盘/补录（非 commit 区间修复记录，按 §7 约定的 `FIX-<主题>-<日期>` 命名）。
> 对应评审 [`REVIEW-a9a28cf..6867995.md`](REVIEW-a9a28cf..6867995.md) 的 **M15**（声称失真）与 **M19**（流程缺账）。

## 1. 为什么要补这份记录（M19）

`1301348`（PR #17 的修复 commit）自述「上游 a9a28cf 评审 + 本 fork **六路**只读子代理并行评审
+ 主代理自审」，但：

- `review/` 下**没有**该轮的 `REVIEW-*` 报告，也没有 `FIX-*` 修复记录
  （`git diff a9a28cf..HEAD -- review/` 的新增文件均属其它轮次）；
- `REVIEW-upstream-a9a28cf.md` 只覆盖上游 + 差异面，不含这六路；
- §9 索引在该轮入口处断裂——本轮「衔接复核」**只能以 commit message 为输入**。

后果在 M15 被实证：commit message 有 3 组共 5 项「声称面 > 落码面」，接手者按 message
检索这批修复会找不到本体。本文逐项核对并标注真实落点；流程侧的整改见 §4。

## 2. 逐项核对（M15，全部为本次实测）

| # | 1301348 的声称 | 实际状态 | 最终落地 |
|---|---|---|---|
| a | 人格三项：`.default_persona` marker 的 `_read()` 兜底、`ABOUT.txt` 空行守卫、`save()` 两文件非事务告警 | **main 无对应机制**：`grep -rn 'default_persona\|ABOUT' agentcore/personas/` 为空；main 的人格是单文件 `data/personas/<name>.md`（`manager.py` 单文件原子写），这三项属于分支 `a7e221b` 的**目录布局**，`git merge-base --is-ancestor a7e221b main` 为 false | 未落 main，也无必要——main 从来不是那个布局（同 `e019db6` 删 `migrate_layout` 的判断） |
| b | 「创建即 0600（8 站点）」含 `scripts/compose_init.py`（模板与兜底两分支） | **`scripts/compose_init.py` 在 main 不存在**（`git log --all --diff-filter=A` 仅 `e5e7c90`/`d9e084d` 两个分支提交过）；真正落 main 的站点是 `totp/budget/notify_budget/jobs/personas.backup/personas.manager/llm.providers/outbox` 等 8 处 | 声称的「8 站点」在 main 上成立，但站点清单里那一条是分支 WIP |
| c | 备份脚本「KEEP=0 假成功已改为 case 拒 0」「`find\|wc` 失败已显式接住」 | **`1301348` 的树里两项都没有**（`git show 1301348:scripts/backup_usb.sh` grep「KEEP=0 必须拒」为 0 命中）；直到 `89b7eaa`（fix: CI 四条真失败）才真正落上，即 HEAD `:24` 的 I3 与 `:205-211` 的 I4 | `89b7eaa`（晚于 1301348 的后续 commit 补落） |

**结论**：`1301348` 的「本轮修掉评审候选全部项」按字面不成立——其中两项在它自己的树里就是旧的，
另两项属于从未合入的分支布局。**代码本身没有因此出错**（三项要么不需要、要么后来真修了），
失真的是记录：按 message 去对应 commit 里找修复，会找不到。

## 3. 现状（2026-11-04 核验）

- 备份脚本两项修复：**已在 main**（`89b7eaa` → HEAD），`tests/test_backup.py` 有 KEEP=0 与
  find 失败的回归；
- 「创建即 0600」：main 的 8 处站点均在，`tests/test_outbox.py`（M12 修复后）等有权限断言；
- 人格三项：main 单文件布局不需要它们；`data/personas` 的演进由
  `tests/test_personas.py::TestSeedRuntimeDir` 覆盖。

## 4. 流程补账（M19 的整改）

1. **每轮评审必须落三件产物**：`REVIEW-<start>..<end>.md` + `FIX-<start>..<end>.md` + §9 索引行。
   缺一件即视为该轮未闭环，`scripts/check_review_docs.py` 的归档闸会拦（索引完整性已机械检查）。
2. **commit message 的「已修复」必须能对上 diff**：本周期起，主代理在写 FIX 文档时逐条核对
   message 声称与落码（`REVIEW-a9a28cf..6867995` §3 的「声称核对」节即此惯例），
   对不上就记 M（本仓 §3 严重度定义里「声称与实现不符（含文档、commit message 夸大）」本就是 M）。
3. **分支 WIP 与 main 的差异要显式标注**：`a7e221b`/UGREEN 一线的布局与 main 不同，
   跨分支搬运修复时应在 FIX 文档标注「落 main / 仅分支 WIP」——`FIX-group-guest-mode.md` 的
   「事后修正」节与本文 §2 即此惯例的两个实例。
