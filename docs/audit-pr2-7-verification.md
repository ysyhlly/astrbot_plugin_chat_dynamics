# PR2–PR7 实施与验收

基线：v1.9.2 / `8acc3e4`，仓库 `astrbot_plugin_chat_dynamics`。这是代码实施批次说明，不表示已创建远程 PR 或发布版本。

用户确认目前没有可用的真实标注集。因此交付可运行的效果验收门槛，真实效果保持 `not_ready`；不声称多话题准确率已提升。

| 阶段 | 实现与验收证据 |
| --- | --- |
| PR2 输入契约 | `annotation_draft.build_batches` 先构建身份映射、确定目标及引用上下文，再按完整 system/user 字符数分批；调用端不截断 JSON。输入测试覆盖 60×500 中文、同前缀账号、四类提及、窗外引用；解析器分别报告 missing/duplicate/out_of_window/invalid_confidence，不补否定答案。 |
| PR3 审核一致性 | `TopicAnnotations` 在写锁中检查 draft_epoch、最新人工标签和忽略记录；清空递增代际，显式恢复忽略条目独立操作。每会话 label 与单项幂等结果同记录提交，旧标签列表可对账重建；请求结果归档而非静默过期。分片索引先登记后写入，201 会话可枚举。屏障测试覆盖中断重试、后来人工修改、原草稿 revision 清理和两人冲突；浏览器验证超时待确认、刷新恢复和同 ID 重试。 |
| PR4 有效评估 | `--check` 是确定性回归；`--check-effectiveness` 独立检查来源、真实验证会话和有效标签。拒绝未来父消息、倒序输入及会话跨 split，话题使用稳定标签配对；候选召回与排序分开。CI 输出有标签合成边界报告，真实效果为 not_ready。执行结果不测时写 not_measured，不冒充发送准确率。 |
| PR5 证据生命周期 | 实际 Addressivity 通过 TurnEvidence 消费既有 ParticipationSnapshot/Policy；上下文截止、配置和策略身份被记录，等待后的提交验证会话/epoch/成员版本/节点。Trace 一次定稿，Dashboard 与标注保留历史决策，只补 outcome/shadow。Persona 在异步人格检查后锁内重新验证，停止/reset/节点消失屏障保证旧任务不提交。沉默仍提交参与观察，发送成功才产生发言事实。 |
| PR6 等待与资源 | 增强阶段共享截止时间，明确平台寻址跳过语义等待。超时显式撤销提交许可；即使协程吞取消也不能拖住回合或迟到提交。回复、必要路由及 Persona 决策、人工草稿、后台任务共享 ProviderBudget，预留回复容量并老化排队优先级；取消同 tick 释放测试防容量泄漏。lookup/queue/generation 与回合阶段提供调用数及 P50/P95。 |
| PR7 职责拆分 | 7 个回合函数移入 turn_pipeline，发送完成回调并入 native_delivery；规范化 AST 比较确认搬移时函数体不变。主入口保留钩子及兼容方法；原有生命周期、原生投递、回放及 SDK 测试约束实际行为，无双发送/双写比较路径。诊断聚合保留在只读面板模块。 |

## 保证边界

- 单次 KV 记录是提交单位，进程内锁不被宣称为跨进程事务。兼容标签列表属于可修复投影；重建失败会在 reconciliation 中显示 pending。
- 旧版本已遗失索引的未知草稿不能仅从残缺索引反向找回；新写入采用先登记策略，避免继续产生孤儿。
- 并发容量控制不是每分钟请求数或 token 配额；本插件没有收到 Provider 配额，因此不虚构配额保证。
- 延迟记录是当前进程的保留样本，最多 2048 节点；受控性能实验不等于生产压测。
- 既有配置名称、阈值、默认值及学习策略兼容/审批门槛保持原契约。PR1 也已移植到此正确基线，见快照契约文档。

## 检查命令

```text
.venv/Scripts/python.exe -m pytest tests -q --cov=astrbot_plugin_chat_dynamics.main --cov=astrbot_plugin_chat_dynamics.core
CHAT_DYNAMICS_REQUIRE_REAL_SDK=1 .venv/Scripts/python.exe -m pytest integration -q
.venv/Scripts/python.exe scripts/evaluate_routing.py --check
.venv/Scripts/python.exe scripts/evaluate_routing.py --fixtures tests/fixtures/routing_supervised_boundaries.json --check
.venv/Scripts/python.exe -m ruff check main.py core tests integration scripts
.venv/Scripts/python.exe -m mypy
.venv/Scripts/python.exe scripts/sync_page_assets.py --check
.venv/Scripts/python.exe scripts/check_release.py --allow-empty-repo
```

## 验证结果（2026-09-16）

- 全量 `tests`：2359 passed；此后新增请求查询 API 测试另有 2 passed。
- 强制真实 AstrBot SDK：19 passed。
- 合并覆盖率：92.39%；主入口 86.37%；既有逐模块覆盖率门槛通过。
- 18 个默认回放场景零失败；合成监督边界有 3 个话题配对、4 条父消息真值，确定性检查通过；真实效果 gate 为 `not_ready`。
- 草稿审核浏览器 16 passed，回放生成浏览器 3 passed；已纳入完整浏览器集合。
- 共享工作区后续兼容检查 100 passed，覆盖 Persona、回合失效、实际 Router 提交屏障及审核请求 API；同时修正 Windows 定时器提前唤醒时不应启动下一增强阶段的边界。
- Ruff、mypy、编译、页面资源同步、中文编码扫描和发布结构检查已通过本轮检查时点。
- 受控 Provider 基准每种策略测量 12 轮，完整参数与原始样本见 `provider-budget-benchmark.json`。回复排队 P50/P95 从约 125/126ms 降至约 0.008/0.010ms，后台排队和总完成时间增加，不能解读为整体吞吐提升。

共享工作区说明：完成上述全量验证后，另一任务“修正学习层角色评估逻辑”继续修改 Persona 提示及阶段记录。这些外部修改未回退，也未归入本轮成果；本轮测试结果是对应验证时点的证据，不冒充另一任务最终状态的验收。

本轮结束时，另一任务新增的 `persona_trace.py` 和 `test_persona_review_boundaries.py` 尚有 Ruff 未用导入/fixture 重定义提示；本轮未修改其工作。PR4 的真实效果验收仍等待独立真实人工标签，不以合成回归替代。
