# AgentJev 群聊决策训练入口

仓库中的 `decision_backend=jev` 调用 TypeSafe Jev API。`aimeigaoshou/agent-jev` 是另一套开源模型与 `/api/evaluate` 协议，不能把它的权重当作 TypeSafe 服务或 Qwen 语言模型直接加载。AgentJev 的模型与训练代码应固定到已审查的上游版本；本仓库尚未启用它接管线上决策。

## 已落地的数据入口

`scripts/agentjev_prepare.py` 读取插件的逐题 DecisionDataset JSONL，按同一 `session_id + request_id` 合并为一次决策 case。只接收版本 2、带 `snapshot_version=1`、有来源清楚的完整教师硬标签、无证据不足或决策冲突的请求。修复后的真实记录把原始结构化快照存在 `metadata.source_state`，脚本重新核实触发正文与每条 target 候选正文；仅有 tokenizer 字符串且缺少快照版本的旧记录会被拒绝。

`target.0` 等逐候选 Noul 会合并为一个 Choice，附带 `none`。多个目标同时为正时保留在拒收计数中，因为当前插件允许多目标，不能无依据地把它们压成一个主目标。旧 `recipient` Noul 问的是是否指向机器人，不是候选用户排序；新采集的 `recipient_choice` 直接提供至多四位有消息证据的群友及 `none`，离线转换后成为 AgentJev 的 `recipient` Choice。正例必须同时具有这一题和五档长度题；`join` 与 `action` 仍需一致。

插件现已在决策学习请求中额外收集五档 `reply_length` Choice 和单一主要收件人 Choice。它们只在真正回应的 case 中进入训练；不回应时的假设长度和对象不会污染标签。运行时有效长度答案会映射到现有 `TurnDecision.length` 三档，并把更细的长度指导附在 `response_goal`。缺失这项答案时保留原有长度决策。新的收件人题目前只用于训练采集，不改变既有收件人路由。

准备命令：

```powershell
python scripts/agentjev_prepare.py <v2-export.jsonl> output/agentjev-private
```

输出 `train.jsonl`、`train_sampled.jsonl`、`calibration.jsonl`、`test.jsonl`、`report.json`。所有输出含群聊正文，只能存于私有、已忽略的目录。默认至少需要 1,000 个完整 case，且每个自然分区至少各有 10 个 join 正例和负例。`train_sampled` 最多把每个正例使用 3 次；校准和测试保持自然分布。会话连续段及近重复快照沿用插件现有分组切分，整个请求不会跨分区。报告按 case 计数，而不是按题目计数。

## 当前远程数据审计（2026-09-23）

在 `win-desktop` 的实时 `decision-learning.sqlite3` 上只读核对（2026-09-23 04:50，采集持续增长）：修复后有 1,750 条 `snapshot_version=1` 的逐题记录、288 个请求；其中仅 **11 个**满足完整请求、有效标签与候选正文要求，且 **全部是 `join=false`**。拒收的请求为：250 个缺教师标签、13 个质量标记冲突、14 个缺触发正文。标注队列中仍有 pending/leased 项。早先导出的 1,971 个 v2 “完整请求”大多没有修复后的结构化候选证据，不作为本轮训练来源。旧 QQ pilot 已被标记为 retired，也不输入领域训练。

已将五档长度与主要收件人采集更新到目标 AstrBot；最终更新前的文件备份在 `/opt/astrbot/laya-decision/backups/agentjev-collection-20260923-050027`，首次长度更新前的备份在同目录的 `agentjev-collection-20260923-044855`。重启后容器内核对八个同轮问题，决策学习仍为 `shadow`、采样率 5%，没有上线 AgentJev 权重或改变接管阶段。

06:54 的标注队列复核发现新快照已持续写入（包括 `recipient_choice` 与 `reply_length`），但有大量租约过期及重试耗尽。实际日志证明：启动初期 `ProviderNotFoundError`，运行时还出现 8 秒标注超时和无效教师答复。已将后台标注独立超时设为 30 秒、租约设为至少 90 秒，并对提供者未就绪增加退避；线上决策超时仍为 8 秒。标注失败现在只记录错误类别与计数，不记录群聊正文。更新前备份为 `/opt/astrbot/laya-decision/backups/agentjev-annotation-20260923-065428`。旧的耗尽任务不会自动被算作有效标签，需要在教师稳定后按快照版本定向恢复并重新审计。06:57 整案复查时有 3,909 条新快照记录、639 个请求，只有 18 个完整 case，全部为 join 负例；另有两个 join 逐题正例尚未成为完整 case。教师仍有无效答复和 30 秒超时，不能宣称队列问题已全部解决。

因此现在不能启动有意义的 AgentJev 微调、长度模型训练或校准。尤其当前没有可评估的 join 正例。线上接管维持关闭；积累到门槛并完成独立人工复核后，再进行 head 验证、LoRA/小学习率微调、独立校准与自然分布测试。测试重点为 join 的召回与误参与率，以及 recipient/target 的 Top-1、NONE 和完整请求正确率。

群聊领域继续预训练需要独立的完整原始群聊语料。AgentJev 权重没有语言模型头，不能直接对它做下一个 token 训练；若先训练 Qwen3 群聊 backbone，再用于 AgentJev，必须验证权重结构兼容并重新训练/校准决策头。现有 5 个 case 和退役 pilot 都不足以验证这条迁移路线。

上游模型与代码：[AgentJev 模型卡](https://huggingface.co/aimeigaoshou/agent-jev)、[训练仓库](https://github.com/malevrigns/agent-jev)。
