# 审计优化续接（2026-09-15）

对应原任务最后确认的顺序：静态护栏 1–4、图排序 6、清扫 5，再拆分配置与审核模块 8、增加离线建议脚本 10。

## 已实现

- 元数据生产者与读者、持久化字段、指标注册和双份翻译的一致性检查。元数据、时钟与指标调用用 AST 解析，区分比较与赋值、顶层与嵌套键；时钟与指标检查还确认扫描到调用，避免空匹配通过。
- `DynamicsDecisionGate` 注入民用时钟；四个实时 `note_*` 方法不再接收调用方时间戳。`evaluate` 保留历史墙钟和独立的消息图时钟。发言成功后的额度记账共用一次取时。
- 消息图仅在乱序插入后排序；最近窗口、线程顺序、最后活动和容量淘汰遵循同一时间顺序，同时间戳保持插入顺序。快照恢复也标记需要重排。
- 清扫一次取得 debounce 活动时间和忙碌会话快照，整个同步清扫期间复用。生成完成后的清扫间隔至少 30 秒，定期清扫继续执行。锁、在途生成、待处理输入与后台任务仍阻止驱逐。
- `ConfigPanel` 承担配置保存、预设和 provider 列表，`AnnotationReview` 承担草稿生成与审核；主入口保留原方法签名并委托。配置保存锁与失败回滚、UMO 隔离和人工采纳规则保持。
- `scripts/suggest_thresholds.py` 读取标注导出，仅对可重放的 legacy ambient 强寻址阈值给出候选。样本不足、版本或基线混合、验证无改善时不给建议；不写配置。详见 [离线阈值建议](threshold-suggestions.md)。

## 回归重点

- 乱序消息的最近窗口、稳定排序、容量淘汰及乱序快照恢复。
- 多用户 debounce 活动合并、忙碌会话不被驱逐、生成清扫节流与定期清扫。
- 单次时钟采样、历史回放双时钟、仅成功发送计数。
- 异步保存取消与失败回滚、审核只采纳选中的有效草稿。
- 真实 `shadow_decision → build_routing_trace → TopicAnnotations` 导出可供脚本使用，超大数字和冲突重复记录被排除。

本轮没有实施其他候选项：语义窗口缓存、统一所有会话容器、容量策略调整及浏览器重试策略。管理命令与笔记本接口仍在主入口。

## 验证结果

- `python -m pytest tests -q`：2218 passed（包含本机可执行的浏览器套件）。
- `CHAT_DYNAMICS_REQUIRE_REAL_SDK=1 python -m pytest integration -q`：19 passed，实际 SDK 为 AstrBot 4.27.5。
- 合并覆盖率 92.31%，主入口 86.49%，`scripts/check_coverage.py` 通过。
- Ruff、mypy、compileall、发布结构检查与页面资源同步检查通过；新增组件及文档的中文编码扫描无异常。

测试日志位于工作区 `artifacts/continuation-tests-final.log` 和 `artifacts/continuation-sdk.log`，覆盖率报告为 `artifacts/continuation-coverage.json`。这是本地验证结果，未发布新版本。
