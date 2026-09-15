# 回合流水线职责

`main.py` 保留 AstrBot 钩子注册、配置同步、会话注册表和对外兼容方法。准备、增强、参与准入及完成回合的计算位于 `core/turn_pipeline.py`，以显式 host 参数使用已有会话；不创建第二份状态、不增加第二条发送路径。

`core/native_delivery.py` 同时拥有原生发送 guard、事件上下文和发送完成后的记账/尾段生命周期。钩子顺序、filter/exclusive 行为、成功发送才记账的约束保持不变。

拆分时对 7 个回合函数及 1 个发送完成函数做了规范化 AST 比较：除 host 参数名称及相对导入路径外，函数体一致。随后使用原有生命周期、原生投递、路由增强及逐回合回放测试检查行为。没有双跑在线写入路径来比较。

## 状态提交

1. 锁内记录有效消息与观察。
2. 锁外等待增强，提交前检查会话、节点、epoch、成员停止版本、配置和策略身份。
3. 锁内从不可变 ParticipationSnapshot 构建 TurnEvidence，原 ParticipationPolicy 直接消费它；准入决策定稿后冻结历史 Trace。沉默消息仍提交参与状态。
4. 发送结果由 OutcomeRecorder 补充；成功平台发送后才写入 Bot 发言与额度。

## 延迟预算与观测

非明确寻址的增强总预算是已启用阶段配置上限的最大值（神经等待与话题复判），阶段共享一个单调时钟截止时间；既有配置名和默认值不变。明确平台寻址不进入前置语义等待，仍经过必要门控。

预算耗尽即记录 `enrichment_budget_exhausted` 并回到决策。复判任务即使忽略取消，也必须通过截止时间与版本检查才能提交；任务仍由宿主跟踪以便清理。每节点复判与话题标题最多三次尝试。

概览接口包含 `latency` 和 `provider_budget`：前者汇总当前保留回合的 prepare、embedding_wait、rerank_wait、enrichment、decision、first_send；后者报告 Provider lookup/queue/generation 的调用量及 P50/P95。历史快照不恢复进程专属计时起点。没有生产压测数据时，不将局部受控延迟实验描述为生产收益。
