# 群聊收件人修复：第一步

这是第一步的历史记录。后续落地情况和验证范围见 [完整实施记录](group_chat_optimization.md)。

本次落实讨论中要求先独立修复的歧义串线问题。当前代码确实以
`topic_ambiguous or addressee_confidence < 0.72` 生成聚合歧义，
Addressivity 和 MessageSemantics 又以该聚合字段否决收件人证据。

现在路由显式输出 `addressee_ambiguous`；参与判断和消息归因 certainty
只读取收件人歧义。`topic_ambiguous` 独立保留。旧快照缺少新字段时，
优先从收件人置信度恢复；连置信度也没有时才兼容原聚合字段。
原 `ambiguous` 仍供诊断、异步路由升级使用，不在这次 hotfix 中删除。
话题回填不修改收件人证据。

回归覆盖明确 Bot 收件人、明确其他收件人、旧快照、低置信收件人，
以及真实 ThreadRouter 对未形成话题消息的新字段输出。

本次不代表整份优化报告完成。后续按讨论中的渐进顺序继续：

1. Decision Trace、固定回放与基线，保证 shadow 只读且不改变会话状态。
2. 统一身份匹配和收件人契约，处理多人收件人、呼语与讨论对象。
3. 迁移快路径、人设等消费者，再评估 Participation 证据族。
4. 在回放护栏下分别评估话题与 embedding 优化、统一 Agent 上下文。

不应在这次缺少基线的 hotfix 中同时改变路由权重或话题召回策略。
