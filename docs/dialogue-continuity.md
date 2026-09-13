# 对话连续性（Dialogue Continuity）

`core/active_dialogue.py` 原本用四个硬编码二元量判断"这条消息是不是在回答 Bot 上一个问题"：

```text
固定 60 秒窗口 · 布尔回答形状 · 中间有任何消息即否决 · 指定参与者身份
```

这类常量无法被学习：在它们之上拟合权重，只能得到"把 60 改成 65"这种结论。本轮把可学习的部分拆成命名权重（`core/dialogue_continuity.py`），把不可学习的部分留作结构门禁。

## 模型

```text
score = (w_time · time_decay + w_answer · answer_credit)
        × turn_factor × competitor_factor
```

| 旋钮 | 默认 | 含义 |
| --- | --- | --- |
| `time` / `answer` | 0.60 / 0.40 | 时间与形状的加权 |
| `time_midpoint` / `time_slope` | 60.0 / 4.0 | 逻辑曲线：60 秒处恰好 0.5 |
| `turn_weight` | 2.0 | 中间隔了几条（轮次差） |
| `competitor_weight` | 1.0 | 其中几条来自目标参与者以外的发言者 |
| `reaction_penalty` | 0.0 | 纯反应（"好的"、"？？？"）要不要算答上了 |
| `threshold` | 0.69 | 接受阈值 |

**结构门禁（不参与学习）**：Bot 上一轮必须是问句；必须是目标参与者本人；回答不能早于问题。

## 行为不变性

默认权重经过标定，与替换前的判定逐条一致，并由 `tests/test_dialogue_continuity.py` 用参数化网格对照旧规则验证。旧规则接受"形状成立、无插入、0 < dt ≤ 60 秒"；默认得分在同条件下接受到 dt ≤ 60.27 秒，差异只有 0.27 秒的窄带。

路由回放与 baseline 对比为 changes: []，即本轮没有改变任何判定结果。

## 为什么"话题是否保持"没有成为旋钮

接受一条对话回答会把对话话题强制写给该消息，因此在当前流水线位置做节点级话题比较不是独立信号。等话题归属先于收件人解析后，它才会成为真正的旋钮；在那之前不加入一个恒为 1.0 的假旋钮。

## 已放弃的一版设计

最初曾给"省略/应答"形状部分加分（0.35）。测试发现所有这类形状本来就落在旧谓词的 `is_answer_like` 内，该分支**不可达**，因此改为可达的 `reaction_penalty`：它作用在"纯反应是否算回答"这个真实存在的判定上。

## 证据入账

五个分量以 `dialogue_*` 代码写入 Evidence Ledger，**原始值与该因子的影响量分开记录**：

加权项记录它在最终得分里的份额（含它经过的乘性因子）；乘性因子记录「把它置为中性后得分会变成多少」的差值，即它把得分推离了多远。得分是因子之积，不存在可加和分解，因此这些行**刻意不等于**得分之和 —— 报一个并不存在的「占比」比报位移更糟。

```text
dialogue_time_decay          raw=1.0000 contribution=0.6000
dialogue_turn_factor         raw=1.0000 contribution=1.0000
dialogue_continuity_score    raw=1.0000 contribution=1.0000
```

学习层因此可以拟合这些旋钮，而不必重新推导语言。
