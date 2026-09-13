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
  ↓  core/topic_annotations.py 已存 decision_trace（含 ledger）
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

没有 `bot_targeted` / `expected_reply` 的标注只会产出 topic 样本，不会猜。

## 为什么必须存特征

只存"判错了"无法回答"为什么错"。账本已经把**因子原始值**与**当前权重下的实际贡献**分开记录，因子差异表因此能直接给出方向：

```text
dialogue_turn_factor   -0.800  (n=36 错18/对18)
dialogue_time_decay    -0.540  (n=36 错18/对18)
```

负数表示该因子在判错时更小，即它本可以把判定拉回来。

## 影子推荐

`analyze_recipient` 对 recipient 样本拟合一个逻辑回归（纯 Python，确定性梯度下降），并把联合效应与边际差异对照：

- **权重偏高**：判错时该因子更大，且拟合系数为负 —— 系统过度信任它。
- **权重不足**：判错时该因子更小，且拟合系数为正 —— 系统没有充分利用它。
- 其余一律标为方向不明确并排除在建议之外。

样本数低于阈值（默认 100，与"累计足够样本再动权重"的原则一致）时返回"样本不足"，不给方向性建议，避免用手选样本拟合噪声。

**该模块不写入任何配置，也不被线上判定调用。**

## 使用

```powershell
# 从回放页导出的标注统计错误分布与因子差异
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
