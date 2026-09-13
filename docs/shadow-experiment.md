# 真实 Shadow A/B 启动约定

## 策略生命周期

Learning 的 `/candidate` 和 `learning_candidate_v1` 提供 validated、shadow、promoted 策略给 shadow consumer；`/published` 和 `learning_published_v1` 只提供 promoted 策略给 active consumer。本体 shadow 永不应用参数，active 仍执行状态、版本、基线及 pin 检查。不要为了使本体读到策略而提前采纳。

## 第一轮实验

仅改变 `strong_addressivity_threshold`，其他参数保持 baseline。保留本体配置快照及策略 ID，把本体 `learning_policy_mode` 设为 `shadow`，Learning 策略进入 shadow。先检查面板读到预期策略，再开始收集。

独立 telemetry 的比较总量与人工标注样本分开。覆盖统计的分母是日志保留窗口内去重后的有效比较，不是历史累计消息数，也不包含没有实际比较的 off/active 轮次。按 policy_id + host_version 分桶，不能混用不同策略或本体版本的收益。

至少取得 2000 次有效比较、100 条已标注分歧，覆盖多个 session 与活跃时段；核对 net_gain > 0、shadow_only > baseline_only、核心指标回退不超过 1%、CI 门槛通过后，才评估是否从 shadow 晋升 promoted。数量门槛不是效果证据，Operational Coverage 也不提供准确率或训练标签。

这些是实验操作条件；本次代码交付不自动启用真实群实验或晋升策略。
