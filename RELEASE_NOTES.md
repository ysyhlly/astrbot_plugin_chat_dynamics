# astrbot_plugin_chat_dynamics v1.19.0

## v1.19.0 — 2026-11-26

- **正式版发布（GA）**：合并全部 `v1.19.0-rc.x` 系列安全加固、存储与性能调度优化及跨仓工程治理。
- **群间见闻主动查询联动**：更新见闻决策与回复指引，支持合并工具触发的 `horizons` 话题上下文回传反馈。
- **全系列统一契约**：统一管理鉴权与 CSRF 防护、共享 `ProviderBudget` 并发池/遥测及全系列配置 SSOT。

## v1.19.0-rc.11 — 2026-11-25

- 安全加固（Batch 1）：全系列统一管理鉴权与 CSRF 防护，加固 Web API 权限与输入边界。
- 数据与存储修复（Batch 2）：补齐 `narrative` 数据库索引、改为非阻塞 WAL checkpoint，并封堵存储保留策略缺口。
- 性能与调度优化（Batch 3）：修复边界取消与 `afterthought` 锁竞争，优化前缀缓存与 `ProviderBudget` 队列隔离。
- 工程与发布治理（Batch 4）：升级跨仓资产同步脚本、全系列 CI 门禁与面板冲突防护。

## v1.19.0-rc.10 — 2026-11-24

- 升级全系列共享 `ProviderBudget` 与 `series_panels.py`：`ProviderBudgetBusyError` 增加结构化 `retry_after`/`reason`，新增只读预检 `provider_circuit_remaining()`、半开单探针恢复，并排除本地边界/策略拒绝计入上游熔断。
- 修复 `afterthought`/`catchup` 合成事件缺少 `_chat_dynamics_boundary_uid` 导致的 100% 失败与主回复错误率污染，独立 `purpose='afterthought'`，`AgentBridge` 还原 `BoundaryCancelled`，对轻微旁白超长做清洗降级。
- `narrative-delivery.sqlite3` 启用 `PRAGMA auto_vacuum=INCREMENTAL`、定期 `incremental_vacuum`/`wal_checkpoint(TRUNCATE)`、`context_archive`/`archive_only` TTL 与行数上限、`character_blobs` 指纹去重及死信 recipient 7 天过期。
- 解锁 `systemone` Prompt 缓存（移出静态区动态 `learning_context`、补齐 `cached_tokens` 遥测），优化 `title` 路由与 `DiscussionReranker` 超时/单探针退避，止损 `user_revisions` 在途回复浪费。

## v1.19.0-rc.9 — 2026-11-22

- 升级全系列共享 `ProviderBudget` 并发池并新增系列 Token/缓存/延迟遥测存储与看板（`core/series_telemetry.py`、`series-telemetry.sqlite3` WAL 小时/日桶聚合与 30 天自动清理、`/series_telemetry` API 及控制台遥测区块）。
- 总控矩阵（`series_matrix`）重构为全并发聚合 + 3 秒短 TTL 缓存与即时失效，新增整行批量切换与模板下发接口（`/series_matrix_batch`）及矩阵快捷操作菜单。
- 新增内置 4 套新群配置模板（`full_companion` / `light_chat` / `observer_archive` / `core_only`）与持久化自定义模板 CRUD（`core/series_templates.py`、`/series_templates`），同时接入控制台矩阵与快速引导（Setup）页。
- 扩展 `get_series_shared_config()` 暴露规范化时区、`reply_provider`、`exclude_groups` 与实时 `in_quiet_hours`；`series-messages.js` 拆分为轻量加载器 + `en`/`ja`/`ru` 按需语言分包并统一八仓 `series_panels.py`。

## v1.19.0-rc.8 — 2026-11-21

- 升级群间系列面板 v2：新增全系列 8 插件群聊总控矩阵（`series_matrix` / `series_matrix_toggle`）、跨插件联邦快捷搜索（`series_search` / `Ctrl+K`）与共享 UI Kit 联动。
- 合入自然节奏延迟补聊独立媒体副本保留及全路径释放修复（`+rhythm.3`），完整保留戳一戳优化与纯 `Star` 自动注册。
- 修复群控矩阵分群角色优先级、`takeover_all` 单群排除回显以及联邦搜索慢同步插件事件循环超时隔离。
- 完善真实 SDK 路由契约测试并将 v2 后端/浏览器与自然节奏测试纳入维护套件。

## v1.19.0-rc.7 — 2026-11-20

- 移除 `main.py` 中已弃用的 `@register(...)` 装饰器，全面迁移至 AstrBot 4.x/5.x 推荐的纯 `Star` 子类自动注册，消除 `register_star` 弃用告警。

## v1.19.0-rc.6 — 2026-11-20

- 优化决策与回复链路的前缀缓存稳定性与 Token 成本（CD-1..CD-6）：隔离易变系统尾部、精简上下文区块，并保留生产环境验证的回复请求提示与工具护栏。
- 合并自然回复节奏（`+rhythm.2`）与回复归属（`reply_attribution`）挂钩。
- 全面优化戳一戳交互：支持 `poke_text` 与 `poke_meme` 复合回应模式、连续戳合并、按好感分层的轻戳响应、同行互戳（`poke_peer`）被动/起哄/活跃对话触发及上下文展开。

## v1.19.0-rc.5

- **新增四步「快速引导」配置向导**：首次打开且尚未配置生效群时自动进入引导，支持自动发现群聊、按已配置的 AstrBot 模型服务智能推荐参与决策方式（`jev` / `llm`）、用户手动触发的只读模型连通性探测、四套开箱场景预设（已有自定义配置重入时默认保留高级调参）以及局部差异保存与群间系列组件状态提示；随时可点「暂不引导」跳过，也可在导航栏或「参数配置」页重新进入。
- **合并语气上下文与反讽评价决策修复**：引入 65,536-token 宽决策窗口与允许来源回溯、带发送者归属的引用/前文语气证据，以及针对反讽与真诚夸赞的二级评价复核。

## v1.19.0-rc.4

支持在原版 AstrBot 4.29.4 的插件区域直接切换已更新的群间面板。入口和目标均需更新，左侧菜单保留入口。

CI 使用与发布批次对应的视觉工坊提交运行真实 SDK 图文联动测试。

叙事快照按游标小批回填，并按待精简状态索引归档记录。面板提供安装版本与源码提交号。

整合系列面板、记忆与见闻上下文、公开话题参与、图片追问与表情元数据保护，以及戳戳和静默动作。保留配置中的模型与决策路由。统一发布版本和安装包元数据。

这是发布候选。需要 AstrBot 4.29.4 或兼容的 4.x 版本。安装前保存旧插件目录、配置及对应 plugin_data；停止目标插件后安装，保留数据库与配置，再启用并检查加载日志。

如升级失败，先停用新版本并保留故障现场与新产生的数据；恢复升级前的代码与相匹配的数据备份。不要直接把旧数据库覆盖到运行中的插件，也不要假设较旧版本能够读取新版本写入的数据。

本候选的测试结果见发布批次验证报告。自动化检查不等于真实 QQ 收发或真实模型对话验收。
