# Dynamics Learning（行为学习层）

本轮在插件内部落地学习层的第一阶段。**影子学习**：只记录、统计、推荐，不写配置、不改线上判定。

## 边界

| 层 | 职责 |
| --- | --- |
| Chat Dynamics | 实时群聊理解 / 工作记忆 / 当前话题 / 收件人 / 是否参与 |
| **Dynamics Learning** | **从标注与运行结果中找出判定偏差，给出权重建议** |
| Self Learning | 社交关系、表达风格、黑话、few-shot、人格学习 |
| LivingMemory | 长期记忆、历史事实、长期召回 |

学习层复用既有结构，不新建采集管线：决策证据来自 Evidence Ledger，真值来自场景回放的人工标注。

## 数据流

```text
回放页人工标注
  ↓  core/topic_annotations.py 已存 decision_trace（含 ledger）与冻结的 routing
  ├─ 候选归因（core/learning/candidates.py）
  │    正确话题没进候选集（生成） vs 进了却没被选中（排序）
  ↓
LearningSample（core/learning/sample.py）
  ↓  特征 = 账本原始值 + 已应用证据码 + 身份事实
     （标注保留了消息正文时，另加由正文派生的形状事实）
统计与因子差异（core/learning/stats.py）
  ↓
影子推荐（core/learning/recipient_learner.py）
  ↓
人工决定是否调整
```

## 样本只接受白名单

`LearningSample.features` 的键取自 `ROUTING_CODES`、`EVIDENCE_CODES`、`DIALOGUE_FACTORS` 与固定的 `fact.*` 消息事实；不在白名单内的键会被丢弃。**不存消息正文**，标识符有长度上限，非法行在读取时被跳过而不是让存储整体失败。

一条标注记录可产出三类样本：

| 任务 | 预测 | 真值 |
| --- | --- | --- |
| topic | `predicted_topic` | `expected_topic` |
| recipient | 追踪结论是否指向 Bot | `bot_targeted` |
| participation | 追踪结论的 `should_reply` | `expected_reply` |

没有 `bot_targeted` / `expected_reply` 的标注只会产出 topic 样本，不会猜。参与样本还有一道额外条件：trace 里的 `should_reply` 必须是真布尔值。`should_reply = null` 表示**没有做出明确决策**，把它记成"应该沉默"会让学习器把"未决"当成"决定不参与"，所以这类行被跳过并计入 `undecided_reply`。只有在 persona 模式写出了明确布尔值的地方才会产出参与样本。

三件不允许退化的事：

- **缺失不是 0。** 没被观测到的 factor 不出现在 `features` 里，`sample.present` 单独记录"哪些被观测到了"。`is_question = False` 与"这个群没开正文保存、`is_question` 根本没采集"是两种事实，前者是行为差异，后者是隐私设置的后果。
- **版本随行。** schema 2 每条样本带 `session_hash`、`policy_version`、`plugin_version`、`routing_schema_version`、`feature_schema_version`。权重一旦开始变动，混在一起的历史语料事后无法解释；schema 1 的旧行留空而不猜。
- **被扣下的行会说明原因。** `SampleBuildReport` 逐条计数跳过的样本（缺 bot id、缺标签、未决回复），CLI 会把原因打到 stderr，不会安静地少给一份语料。会话标识存的是 `session_hash`（sha256），既能按会话切分，又不直接暴露群号。

## 为什么必须存特征

只存"判错了"无法回答"为什么错"。账本已经把**因子原始值**与**当前权重下的实际贡献**分开记录，因子差异表因此能直接给出方向：

```text
dialogue_turn_factor   -0.800  (n=36 错18/对18)
dialogue_time_decay    -0.540  (n=36 错18/对18)
```

负数表示该因子在判错时更小，即它本可以把判定拉回来。

## 候选归因

话题判错有两种，修法相反，所以必须分开数：

```text
正确话题没进候选集    → 候选生成（检索）问题，重新调排序权重救不回来
正确话题进了却没选中  → 排序 / 打分问题，检索是好的
```

`core/learning/candidates.py` 因此给两个分母不同的指标。**注意不是同一个分母**：`Candidate Recall@K` 的分母是所有"真值可归属且候选被记录"的标注；`Selection Accuracy` 的分母**只有正确话题确实进了候选集的那些**。这样"检索差但排序好"和"检索好但排序差"不会互相掩盖——高选择准确率配低召回指向候选生成，反过来指向打分。

每条标注恰好落入一个归因桶：`selected` / `ranking_error` / `candidate_miss` / `not_recorded` / `new_topic_expected` / `unattributable`。

两条不能混的语义贯穿始终：

- **键不存在 ≠ 空列表。** 前者是路由器没记录候选，无从判断正确话题是否被提出，**不计入召回**；后者是它找过并且一个都没提出来，就是一次未命中。把两者并起来会把"无法归因"读成"检索失败"。
- **缺分数 ≠ 分数为 0。** 只带 `topic_id` 的候选仍然算"被提出过"（召回要数它），但不参与排序，也绝不贡献一个 0 分。

解析同时接受旧版 `[[score, id], ...]` 与你计划里的结构化行（`topic_id` / `final_score` / `rank` / `evidence`）。有 `rank` 用 `rank`，否则按分数降序，再否则保持书写顺序；路由器写的是降序，所以万一顺序不对，按分数读比按位置读安全。无法解析的条目会被丢弃并计数。当前路由器只记前 3 个候选，所以 recall@5 与 recall@3 必然相同——报告会把观测到的候选长度分布打出来，免得这个截断被读成一个测出来的数字。

## 影子推荐

`analyze_recipient` **不拟合"真值是不是 Bot"**。它拟合的是当前策略的**残差**：记录的策略判定以固定权重进入模型（`POLICY_OFFSET`，不参与拟合），所以模型学到的一切都只是对"已经跑过的那套策略"的修正。修正量再用同一批标签在**按会话分组**的留出集上重放，按 TP/FP/TN/FN、precision、recall、F1 计分，只有相对记录策略构成 Pareto 改进才作为建议报告。

方向判定必须**固定预测、只让标签变**，否则两组之间混进了"预测本身"的差异：

- **权重不足（under_used）**：对比 FN 与 TN —— 两边都判成了"不是 Bot"，看漏掉的那批是不是这个因子更高。
- **权重偏高（over_used）**：对比 FP 与 TP —— 两边都判成了"是 Bot"，看错发的那批是不是更高。
- 其余标为方向不明确并排除在建议之外。

准入不是"够 100 条"就够：`total ≥ 100`、每类 `≥ 30`、每个错误方向 `≥ 10`、会话 `≥ 3`。任一条不满足就返回"样本不足"并逐条列出原因——95 条 BOT 加 5 条 OTHER 也是 100 条，而且可能全都来自同一个群、同一种错误，用它拟合出来的方向只是看起来很确定。

Pareto 门：F1 必须**严格上升**，且 precision 与 recall 任一回退超过 `PARETO_SLACK`（0.02）即否决。只报告成功的那一次会掩盖它用另一个指标换来的收益。

**该模块不写入任何配置，也不被线上判定调用。**

## 使用

```powershell
# 从回放页导出的标注统计错误分布、因子差异与候选归因
# （候选归因随 --annotations 自动打印，不需要额外开关）
python scripts/learning_report.py --annotations annotations.json --bot-id <bot_id>

# 追加影子推荐
python scripts/learning_report.py --annotations annotations.json --bot-id <bot_id> --recommend

# 机器可读
python scripts/learning_report.py --annotations annotations.json --json
```

`--save` 把派生样本写成 JSONL。`SampleStore` 是有界追加存储库，**当前只有这条 CLI 路径会写**：插件运行时尚未接入样本落盘，store 默认关闭且没有任何运行时调用点。接入运行时采集是需要单独决定的动作，不应被文档说成「已经默认关闭」。

## 本轮未做

- 没有训练校准模型，权重未做概率校准，统计口径已在输出中标注。
- 没有自动应用：没有 Policy Store、没有回滚版本、没有自动调参。
- parent 任务暂无人工标注字段，因此不产出 parent 样本。
- Participation 的运行时代理指标（回复后是否有人接话）尚未采集，目前只消费标注里的 `expected_reply`。
- 插件运行时没有写入样本：样本只从已保存的标注派生。
- 正文派生的形状事实依赖 `console_show_message_content`；该开关关闭时标注不含正文，样本只有账本与身份事实 —— 这是隐私设置的直接后果，不是缺陷。
- 未接入群聊画像（Group Dynamics Profile）与阈值自动搜索；这些依赖先积累到足够的真实标注。
- 候选归因受两处上游限制：路由器只记前 3 个候选，所以 recall@5 目前不可能区别于 recall@3；`routing.topic_candidates` 的写入覆盖率直接决定可归因的样本量，写不进去的行只会落进 `not_recorded`，不会给出方向。
- 话题样本的真值就是标注里存下的 `expected_topic`（回放页拒绝保存指向未知话题的标签），而评测夹具用的是自由文本标签、需要靠同一会话内更早的已标注消息反推——两条路径共用指标，但真值来源不同，读数时不要混。
