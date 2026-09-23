# AgentJev 群聊决策训练入口

决策学习的默认学生现为开源 AgentJev，走 `agentjev.decision.v1` 的 `/api/evaluate` 协议；`decision_backend=jev` 仍是另一条 TypeSafe Jev 人设决策路径。旧 Laya 学生仅保留显式兼容选项。AgentJev 权重尚未部署到 `win-desktop`，也没有运行中的服务，因此目前只有接口与采集入口，没有真实学生推理或线上接管。

## 已落地的数据入口

`scripts/agentjev_prepare.py` 读取插件的逐题 DecisionDataset JSONL，按同一 `session_id + request_id` 合并为一次决策 case。只接收版本 2、带 `snapshot_version=1`、有来源清楚的完整教师硬标签、无证据不足或决策冲突的请求。修复后的真实记录把原始结构化快照存在 `metadata.source_state`，脚本重新核实触发正文与每条 target 候选正文；仅有 tokenizer 字符串且缺少快照版本的旧记录会被拒绝。若本轮教师看到经过 `/prepare` 处理的输入，训练使用那份实际输入，原始快照仅用于证据完整性核对。输出把当前消息放在历史之前，并用训练时相同的 tokenizer 和序列预算验证每道题仍能看到当前消息；放不下时拒收该 case。

`target.0` 等逐候选 Noul 会合并为一个 Choice，附带 `none`。多个目标同时为正时保留在拒收计数中，因为当前插件允许多目标，不能无依据地把它们压成一个主目标。旧 `recipient` Noul 问的是是否指向机器人，不是候选用户排序；新采集的 `recipient_choice` 直接提供至多八位有消息证据的群友及 `none`，离线转换后成为 AgentJev 的 `recipient` Choice。正例必须同时具有这一题和五档长度题；`join` 与 `action` 仍需一致。

插件现已在决策学习请求中额外收集五档 `reply_length` Choice 和单一主要收件人 Choice。在线教师只回答原有必需题，额外两题由后台标注，不占用本轮参与决策时限；已获准的学生答案仍可在线提供长度。它们只在真正回应的 case 中进入训练；不回应时的假设长度和对象不会污染标签。运行时有效长度答案会映射到现有 `TurnDecision.length` 三档，并把更细的长度指导附在 `response_goal`。缺失这项答案时保留原有长度决策。新的收件人题目前只用于训练采集，不改变既有收件人路由。

AgentJev 适配器将一次请求的 `join`、主要 `recipient`、独占 `target`、`action`、`reply_length` 合并为一组 typed questions。训练导出与在线推理共用 `core/agentjev_state.py` 的状态格式和请求长度上限，保留消息路由语义、参与策略和有限历史；过长的路径在本地拒收。目标选项末尾含稳定消息身份指纹，以免上游只保留候选尾部时，把正文结尾相同的两个目标压成相同输入。**此前导出的 AgentJev JSONL 必须重新生成，不能与新版在线输入混用。**推理服务缺失、输出格式错误或请求超时均不产生学生决策；`active` 模式还需要独立校准与明确审批元数据，当前公开协议没有这些元数据，故不会让未验收的学生接管。服务运行后，在 AstrBot 容器可达地址上配置 `agentjev_base_url`；Docker 内的 `127.0.0.1` 通常不是宿主 WSL 的服务地址。校准前保持 `shadow`。

准备命令：

```powershell
python scripts/agentjev_prepare.py <v2-export.jsonl> output/agentjev-private --tokenizer <AgentJev模型或tokenizer路径> --max-len 512 --max-state-tokens 256
```

输出 `train.jsonl`、`train_sampled.jsonl`、`calibration.jsonl`、`test.jsonl`、`report.json`。所有输出含群聊正文，只能存于私有、已忽略的目录。`--max-len` 和 `--max-state-tokens` 必须与后续 AgentJev 训练、推理配置相同。默认至少需要 1,000 个完整 case，且每个自然分区至少各有 10 个 join 正例和负例。`train_sampled` 最多把每个正例使用 3 次；校准和测试保持自然分布。会话连续段及近重复快照沿用插件现有分组切分，整个请求不会跨分区。报告按 case 计数，而不是按题目计数。

## 当前远程数据审计（2026-09-23）

在 `win-desktop` 的实时 `decision-learning.sqlite3` 上只读核对（2026-09-23 04:50，采集持续增长）：修复后有 1,750 条 `snapshot_version=1` 的逐题记录、288 个请求；其中仅 **11 个**满足完整请求、有效标签与候选正文要求，且 **全部是 `join=false`**。拒收的请求为：250 个缺教师标签、13 个质量标记冲突、14 个缺触发正文。标注队列中仍有 pending/leased 项。早先导出的 1,971 个 v2 “完整请求”大多没有修复后的结构化候选证据，不作为本轮训练来源。旧 QQ pilot 已被标记为 retired，也不输入领域训练。

已将五档长度与主要收件人采集更新到目标 AstrBot；最终更新前的文件备份在 `/opt/astrbot/laya-decision/backups/agentjev-collection-20260923-050027`，首次长度更新前的备份在同目录的 `agentjev-collection-20260923-044855`。重启后容器内核对八个同轮问题，决策学习仍为 `shadow`、采样率 5%，没有上线 AgentJev 权重或改变接管阶段。

06:54 的标注队列复核发现新快照已持续写入（包括 `recipient_choice` 与 `reply_length`），但有大量租约过期及重试耗尽。实际日志证明：启动初期 `ProviderNotFoundError`，运行时还出现 8 秒标注超时和无效教师答复。已将后台标注独立超时设为 30 秒、租约设为至少 90 秒，并对提供者未就绪增加退避；线上决策超时仍为 8 秒。标注失败现在只记录错误类别与计数，不记录群聊正文。更新前备份为 `/opt/astrbot/laya-decision/backups/agentjev-annotation-20260923-065428`。旧的耗尽任务不会自动被算作有效标签，需要在教师稳定后按快照版本定向恢复并重新审计。06:57 整案复查时有 3,909 条新快照记录、639 个请求，只有 18 个完整 case，全部为 join 负例；另有两个 join 逐题正例尚未成为完整 case。教师仍有无效答复和 30 秒超时，不能宣称队列问题已全部解决。

新的队列处理把模型服务未就绪和超时延后重试；格式持续错误的任务会显示为 `failed`，不再保持一个过期 `leased` 状态。修复教师后可以只恢复版本 1、快照仍有效且没有标签的耗尽任务：

```sh
python scripts/decision_dataset.py --database /path/to/decision-learning.sqlite3 requeue-exhausted --snapshot-version 1 --limit 1000
```

恢复命令会重新触发教师调用，应先检查提供者状态和标注预算。标注优先级的每分钟覆盖统计改用只读标签摘要，不再持有采集写入锁扫描整库快照。

因此现在不能启动有意义的 AgentJev 微调、长度模型训练或校准。尤其当前没有可评估的 join 正例。线上接管维持关闭；积累到门槛并完成独立人工复核后，再进行 head 验证、LoRA/小学习率微调、独立校准与自然分布测试。测试重点为 join 的召回与误参与率，以及 recipient/target 的 Top-1、NONE 和完整请求正确率。训练前还须用同一 tokenizer 核对在线请求与训练样本的状态长度、题目文案和选项顺序；当前适配器尚无真实模型端到端验证。

群聊领域继续预训练需要独立的完整原始群聊语料。AgentJev 权重没有语言模型头，不能直接对它做下一个 token 训练；若先训练 Qwen3 群聊 backbone，再用于 AgentJev，必须验证权重结构兼容并重新训练/校准决策头。现有 5 个 case 和退役 pilot 都不足以验证这条迁移路线。

上游模型与代码：[AgentJev 模型卡](https://huggingface.co/aimeigaoshou/agent-jev)、[训练仓库](https://github.com/malevrigns/agent-jev)。
