# Changelog

All notable changes to this plugin are recorded here.

## v1.9.1 — 审计修复、会话性能与审核体验（2026-09-15）

- 修复作息误判、门控异常放行、已发送尾段身份登记和卸载取消时的状态清理；门控记账统一使用民用时钟，仅成功发送后计入额度。
- 修复学习策略刷新与基线校验、重启后的决策证据保留、机器人草稿身份判定和旧 Provider 接口的预标注模型选择。
- 接通多模态能力探测；无原生记忆钩子时异步预热 selflearning 批准记忆，请求钩子仅使用缓存，忘记操作会使缓存失效。媒体隐私文案只描述已实现的行为。
- 乱序消息与快照恢复采用一致的时间顺序；清扫共用 debounce 活动快照，生成完成后的清扫按 30 秒节流，保留忙碌会话保护。
- 配置面板与草稿审核拆为独立组件，保留保存锁、失败回滚及人工采纳规则；改进各页面加载、错误提示、导航和草稿失效状态。
- 增加元数据、时钟、指标与翻译的一致性检查，补齐浏览器 CI 套件。
- 新增离线阈值建议脚本：读取人工标注导出，样本和独立验证充分时才给出强寻址阈值候选，不自动写入配置。

验证：本地 2218 项测试及 19 项真实 AstrBot SDK 集成测试通过；合并覆盖率 92.31%，主入口 86.49%；Ruff、mypy、发布结构及页面资源同步检查通过。

## v1.9.0 — AI 预标注：模型起草，人工采纳

学习层的真值一直是人工标注，而理由很具体：回复决策本身就是模型的判断，用模型生成的标签去训练它，
是自己给自己判卷。但冷启动是真实的 —— 消息图只留 500 条 / 60 分钟，没人标注就等于永远没有样本。
这一版把「起草」和「标注」分开：模型出草稿，人按保存才算数。

### 草稿是什么

- 新增 `core/annotation_draft.py`：按当前会话的窗口起草 **该不该回** 与 **是否在对机器人说话**，
  外加置信度与一句话理由。**不猜话题归属** —— 那一半在页面上本来就是一键选择。
- 窗口按时间顺序整段发给模型（机器人自己的消息作为上下文保留），但**只对非机器人且尚无标注的消息**起草；
  没被问到的 `msg_id` 不会成为草稿，会被丢弃并计数上报。
- 模型看不到本体的判定：参与档位、分数、结果段都不在载荷里。看过答案的草稿是橡皮图章，不是第二意见。

### 草稿不是标注

- 草稿写在自己的键 `annotation_drafts_v1_<session>`，`topic_annotations_v1_*` 里不会有它，导出、混淆统计与
  学习层都不会把它当成标签。
- 人在回放页按下「保存标注」才产生记录；保存的值与草稿一致时写入 `label_source: "human"` +
  `accepted_from: "ai"` + `draft_confidence`，改过任何一个值就不再标记。
- 标注指标新增 `ai_assisted` 计数，并在样本说明里写明其中多少条采纳了草稿。

### 面板与配置

- 回放页新增「生成 AI 草稿」与「本页全部按草稿填写」：草稿以一行提示呈现（值 + 置信度 + 理由），
  可逐条或整页填入收件人与回复标签，话题仍由人选择，确认后按保存。
- 新增配置 `annotation_draft_enabled`（默认 false）、`annotation_draft_provider`（留空回退）、
  `annotation_draft_limit`（默认 20，范围 1~40）、`annotation_draft_timeout`（默认 60 秒）。
- 新增接口 `POST annotation_draft`；前端 `apiGet` / `apiPost` 支持按调用覆盖超时（面板读 8 秒，
  等模型的那一次用 180 秒）。
- 适配器新增 `draft` 用途与 `draft_provider_id`：回复、氛围、预标注三者各自独立，一个设置不会悄悄顶掉另一个。

### 验证

- 新增 `tests/test_annotation_draft.py`（15 项）：批次只对未标注的非机器人消息起草、机器人与已标注消息只作上下文、
  窗口上限、无正文不成草稿、提示载荷不含本体判定、合法/围栏/纯文本回复、未被问到的 id 不落库、
  单字段也可成稿、置信度截断、草稿不进标注表、采纳写入来源标记、改一个值就撤销标记、无草稿的标注就是纯人工。
## v1.8.1 — 遥测口径修正与窗口上限外报

两处都不改线上行为，改的是**别人读到的数字**：

- `learning_policy_rejected_overlap` 以前在 `apply_to` 原样返回配置时 +1 —— 也就是说，`off` / `shadow`
  这类「本来就不该应用任何参数」的状态每次刷新都会被记成「策略被拒绝」，一个健康的安装看起来像坏了一样。
  现在只有**真的因为 hover/strong 阈值对重叠而整组丢弃**才走这个计数器；其余情况记到新的
  `learning_policy_not_applied`。运行时把原因写进 `last_apply_reason`（`not_applied` / `no_fields` /
  `overlap` / `applied`），调用方不必再猜是哪一种。
- 运行快照新增 `graph: {max_nodes, ttl_seconds}`：本体消息图的保留规则（默认 500 条 / 3600 秒）随快照
  一起外报，学习层因此可以说清「这条消息还有多久就不能再标注了」，而不是自己假设一遍上限。字段是追加式的，
  旧读取方忽略即可。

验证：策略测试 41 项（新增 5 项覆盖三种 `apply_to` 结果与计数器分流）、快照往返 4 项（新增 `graph` 断言）、
`ruff` / `mypy` / 全量单元与集成套件通过。
## v1.8.0 — Shadow A/B 第一阶段：记录策略判定

本体在 `learning_policy_mode = shadow` 时**不改任何行为**，但每条处理过的消息会同时算出
「baseline 会怎么判」和「策略会怎么判」，写进 schema 3 轨迹的 `shadow` 段。

- `core/learning_policy.py` 新增 `shadow_decision`：**精确复现**准入规则而不是近似它 ——
  结构化证据短路（`structural`）、无前置机器人消息的提前返回（`early_return`）、
  环境层加性分数比阈值（`ambient`）。判定与轨迹一起冻结，学习层才能把两边的结论对上；
- 只有 `shadow` 模式记录。`active` 下策略就是运行时，比较等于和自己比；`off` 下没有可比对象 ——
  两种情况写进去，都会给分歧子集塞进一列「一致」的、其实从未比较过的行；
- 策略没有移动准入阈值时返回 `None`：没有可比的东西，就不该记；
- `shadow_decision` 同时进入 `META_FIELDS`，重启不会把已记录的比较退化成缺失；
- `main.py` 里的运行期部分（基线计算、策略折叠、刷新节流、状态查询、shadow 判定）
  已在 v1.7.0 抽到 `core/learning_policy_runtime.py`，本版只在那里加一层委派。

学习层侧配套：`core/shadow.py` 做配对表与 active 门槛，详见它的 CHANGELOG。

## v1.7.0 — 学习策略消费端、旧学习层清理与跨插件契约

### 学习策略消费端（off / shadow / active）

- 新增 `core/learning_policy.py`：读取 Dynamics Learning 发布的策略并决定是否使用。
  默认 `off`（完全不影响行为）；`shadow` 只解析、算出「会改成什么」、不应用；
  `active` 应用且必须通过全部兼容性检查。
- 三项检查，各自挡住一种过期方式：
  - `policy_contract_version`：发布协议版本，不认识就直接拒绝而不是半读；
  - `validated_host_versions`：**成员判定，不是 SemVer 比较**。`1.7.0 -> 1.7.1`
    可能改掉参与度计算或门禁顺序，而策略里每个阈值都是对着旧分布校准的。列表为空表示
    「学习层不知道数据来自哪个本体版本」，按不匹配处理；`active` 要求 host 版本在列表内
    （今天是严格相等），`shadow` 允许不匹配但打 `version_mismatch` 且绝不应用；
  - `baseline_config_hash`：策略假定的基线配置摘要。运营改过其中任何一个参数，
    这份策略的数字就是对着一个已经不存在的基线校准的。
- 白名单：只应用 `ALLOWED_PARAMS` 里的键。**未知键或超出范围的取值会让整份策略不可用**，
  而不是部分应用 —— 策略是一个被验证过的**集合**，离线结果描述的是整个集合，
  只应用本机认得的那部分等于应用了一个没人量过的配置。`shadow` 仍会解析这个子集，
  因为它标记为「部分」之后是有用的观测对象。
- `dataset_fingerprint` 是来源证明，不是兼容条件：默认不因为它缺失或变化而拒绝，
  只有显式配置 `learning_policy_expected_dataset_fingerprint` 时才做完全匹配。
  「人为批准锁定」的正确位置是 `learning_policy_expected_policy_id`。
- 参数注入点在 `_with_learning_policy`，位于配置解析之后、任何读取之前，所以地址度阈值、
  话题阈值与面板的「生效配置」看到的是同一组值，后续的配置热重载也不会把策略悄悄顶掉。
  阈值对（hover < strong）不成立时整组丢弃，而不是应用一半。
- 基线摘要读的是**配置值**而不是路由器的实时属性：实时属性已经带着上一次应用的策略，
  拿它做摘要会让策略在下一次刷新时否定掉自己的兼容性，模式在两个区间之间来回跳。
  `topic_commit_threshold` 在配置里是 `0.0`（表示「推导」），因此按
  `ThreadRouter.configure_topics` 的同一条规则推导；跨仓库测试把两边钉在一起。
- 面板新增「学习层联动」分组；生效配置与已存配置不一致时，被策略覆盖的键单独列出并给出原因，
  而不是报成一个无法解释的 mismatch。
- 修复一处跨仓库耦合：Learning 的 `BASE_POLICY` 认为 `topic_commit_threshold` 默认 0.58，
  而本体的配置字段默认 0.0（由 `topic_join_threshold` 推导）。不修的话每一份发布摘要都对不上，
  `active` 永远不可达 —— 而且看起来像是「运营改过配置」。

### 清理旧 `core/learning/`

旧的插件内学习层与独立的 Dynamics Learning 插件职责重叠：两边都在把标注转成样本、
都在统计错误分布、都在给方向性建议。两份实现意味着两个答案，而运营只看到其中一个。

- 删除 `core/learning/`（sample / stats / store / recipient_learner / builder / candidates）
  与 `scripts/learning_report.py`，以及它们的三份测试；
- `core/learning/candidates.py` 移到 `core/candidate_metrics.py`：它不是学习层的一部分，
  而是离线路由评测与标注控制台读的实时指标，不依赖样本格式、学习器或存储；
- `core/integrations/selflearning*.py` 保持不动 —— 那是 Self Learning 联动，与学习层无关。

### `main.py` 分阶段拆分（第一阶段）

把学习策略的运行期部分抽到 `core/learning_policy_runtime.py`：配置基线计算、策略折叠、
刷新节流与状态查询。边界刻意收窄 —— 解析与兼容性规则留在 `learning_policy.py`，
运行期模块不知道消息怎么被路由 —— 后续两阶段（回合管线、控制台面）可以各自移动。

### 文档

新增 `docs/learning-contract.md`：两条通道、两个互不推导的协议版本、
trace schema 3 的字段表、发布契约的形态、消费侧的三项检查，
以及旧 `core/learning/` 删除后的去向对照表。

## v1.6.2 — Learning Contract v3：候选逐条证据与最终结果

- 决策轨迹升到 **schema 3**（`trace_schema_version = 3`）：
  - 新增 `routing` 段：`selected_topic` 与结构化 `topic_candidates`
    （`topic_id` / `final_score` / `rank` / `evidence`）。候选逐条证据由
    `TopicResolution.resolve` 为**每一个**被打分的候选保留，而不只是胜出的那一个 ——
    「是哪个分项把错的候选排到了前面」问不出只留胜者分解的快照。
  - 新增 `outcome` 段：`final_outcome` / `delivered` / `suppression_reason` /
    `stage`。轨迹是在决策时冻结的，最终结果当时还不存在，所以它由
    `core/outcome_recorder.py` 在各检查点写入节点元数据，并在标注快照重建时重新挂回。
- 新增 `core/outcome_recorder.py`：`not_attempted` / `suppressed` /
  `generation_failed` / `delivery_failed` / `delivered` 五个检查点集中在一处，
  不再散落在 `main.py` 的五个调用点上。投递是终态：只要有一个分片发出去了，
  这一轮就是 `delivered`。
- schema 号改由 `trace_schema_version` 承载。旧键名 `routing_schema_version` 描述的是
  routing 段，而数字描述的是整条轨迹，名字说错了事；读取端兼容旧键，本模块只写新键。
- 标识脱敏同步覆盖候选集：schema 3 在 `routing` 里重复了每一个话题标识，
  只清理 `topic` 段会让「已脱敏」这句话只对两处中的一处成立。
- 运行时快照新增 `plugin_version`（读 `metadata.yaml`，不用字面量），
  学习层据此记录策略是在哪个本体版本上验证的；读不到就是空字符串，那是「无法验证」，
  不是「匹配」。`outcome` 同时进入 `META_FIELDS`，重启不会把已记录的结果退化成缺失。

这一版回答的是一个具体误判：schema 2 里「准入正确但被作息压掉」和「路由根本没准入」
是同一条记录，所以学习层只能把前者也算成路由漏回复。schema 3 之后两者可以分开。

## v1.6.1 — 审查修复：双时钟、门控降级与存储边界

- 修复墙上时钟与单调时钟混用：决策门闩把同一个时间值同时用于作息小时判断和节点热度判断，而节点时间戳来自单调时钟，生产环境下所有节点时间窗比较都偏移约 1.7e9 秒——「还有多人在聊就不自睡」的保护失效、刚落地的提问被判成悬空 45 秒缺口、机器人刚回答过仍会触发 follow_up。现在 `decision_gate` / `daily_rhythm` / `useful_proactive` 显式区分 `now`（民用时钟，用于小时、日期与配额）与 `node_now`（单调时钟，用于节点时间窗），规则与人设两条链路都传入两个值；不传时保持旧行为，单元测试的 VirtualClock 语义不变。
- 修复对话连续性时间衰减溢出：`time_decay` 在间隔超过约 2900 秒时抛 `OverflowError`，异常穿过路由被防抖回调吞成一行日志，该轮消息无判定也不回复（安静群 48–60 分钟窗口即可命中）。指数改为饱和，超出区间返回 0.0。
- 修复客服套话清理误删正文：「希望」模式里每个分组原本都可选，单独一个「希望」即匹配，导致「我希望明天别下雨」被削成「我明天别下雨」、「希望这个回答能帮到你」整条变空。现在要求同时出现被帮对象与受益者。
- 作息自睡分支补上 `rhythm_timezone`：原来只有这一处按宿主系统时区判断，与同文件其余六处不一致，配置时区与宿主不同时会在当地白天进入自睡。
- 门控异常改为保守判定并记录日志：媒体门闩异常按「先旁听」、作息异常在未点名时保持安静、主动配额异常在未点名时不主动；人设路径的门闩异常不再静默跳过全部分寸、隐私与配额约束。
- 记忆与便签输入校验：`/notebook` 与指令入口对时长、日期与文本做有限性和区间校验，`Infinity` 不再能落盘成永久静音；记忆层兜底把单次静音夹在 720 小时内，落盘失败从 DEBUG 提到 WARNING，两处会话缓存加上限（256）。
- Web 面板：读空气的今日计数改为按所选会话统计（未知名不再回退为全局数据）；批注接口在关闭正文显示时脱敏 `decision_trace` 与收件人标识；请求体上限从请求头读取（宿主对象没有该属性）。便签读写的阻塞 IO 评估后保留在事件循环线程：其内存缓存与消息路径（到期提醒、记忆摘要）共享，只把面板挪到工作线程会引入读写竞争，收益不足以换取该风险。空白会话标识不再阻断回退。
- 集成与提示词：Hub 因重配取消在途请求时不再连带取消整条回复（仅当调用方自身被取消才向上抛）；工具回执不再把工具原始输出写进 system prompt，并随会话重置清理；Hub 凭据变量名限定为 Hub 相关名称，配置了密钥时拒绝非回环的明文 http；Hub v1 的 `/context` 响应现在必须回显 `group_id` 与 `user_id`，缺失或不等一律按 `scope_mismatch` 丢弃本次背景数据（此前缺失即跳过校验）。
- 路由：rerank 失败改为带退避的重试（30 秒起、最长 300 秒），不再一次超时即永久放弃；成题门槛只拦截新建话题，明确续接已有话题不再被清空；burst 连通扫描记忆化 pair 分数（80 节点窗口 85,320 → 3,160 次语义调用）。
- 其它：poke 去重标记在发送失败时释放；生成循环 finally 中的会话清理不再顶替 `CancelledError`；`terminate()` 的兜底快照移入 `finally` 并 shield；快照增加总字节预算（4 MB，超出时从最久未活跃会话开始丢弃）；debounce 世代字典按小时回收；批注读取丢弃损坏行而不是整体失败；embedding 零向量不再被当作有效神经向量。

## v1.6.0 — 行为学习层、对话连续性旋钮与短期语义缓存

- 新增 `core/learning/` 影子学习层：把回放人工标注与既有决策证据转成 `LearningSample`，统计各任务的错误分布与因子差异，并对收件人判定给出「权重偏高 / 权重不足」的方向性建议。样本特征只接受证据白名单，不存消息正文；样本数不足时不给建议，且全程不写入任何配置。
- 对话连续性从四个硬编码二元量改为命名权重（`core/dialogue_continuity.py`）：平滑时间衰减取代固定 60 秒窗口，轮次差与竞争对象取代「中间有消息即否决」，并新增「纯反应是否算答上」的旋钮。默认权重经标定与替换前的判定逐条一致，行为零变更。
- 五个连续性分量以 `dialogue_*` 代码写入证据账本，原始值与加权贡献分开记录，学习层因此可以拟合旋钮而不必重新推导语言。
- Embedding 缓存新增有效期（`embedding_cache_ttl_seconds`，默认 1800 秒）与 `warm()`。缓存命中会决定 `match()` 使用神经还是哈希后端，预热已知语料可消除离线回放对淘汰顺序的依赖。
- 新增 `scripts/learning_report.py`：从导出的回放标注生成样本、错误分布与因子报告，`--recommend` 追加影子推荐。
## v1.5.3 — 集成边界收口、共享语义事实与可解释路由

- 新增 `core/integrations/` 集成层：`CapabilityRegistry` 一次性发现 Self Learning、LivingMemory 与宿主 embedding 能力，业务代码不再自行 `hasattr` 试探；历史 Python 兼容方法隔离到 `legacy/`，仅供管理，不参与普通聊天注入。
- 确立原生 Hook 优先：普通 AstrBot 请求不再由本插件调用搭档 `/context` 或长期记忆检索，改由两个搭档各自的 `on_llm_request` 注入，消除重复查询与重复提示；只有绕过宿主请求钩子的内部 Agent 才使用 Self Learning Hub v1。
- 新增 `selflearning_hub_url` 与 `selflearning_hub_key_env` 配置：Hub 只读取 social / jargon / few_shots 背景文本，凭据仅从进程环境读取，不写入配置或面板，配置变更会使在途请求失效。
- 新增不可变 `MessageFeatures`：统一短句、省略、应答、问句结尾、话题起点与称呼等输入事实，缓存有界且按源文本键控，修复元数据缓存污染；各模块原有语义差异、权重与 TTL 保持不变。
- 新增 Evidence Ledger：topic / parent / recipient / participation 四域记录因子原始值与实际加权贡献，只导出白名单代码与有限数值，并明确标注未经概率校准；回放分列话题、父消息与收件人，标识脱敏统一处理。
- 控制台互联面板新增能力注册表，区分「已发现 / 可用 / 已选择」，配置面板同步暴露 Hub 设置；新增集成注册表、请求边界、消息事实与证据账本回归测试。

## v1.5.2 — 引用识别、参与档位与回放容量优化

- 引用消息在重启或原消息离开图谱后，仍可利用平台提供的作者身份识别收件人及对机器人的直接回复，不虚构原消息内容或话题。
- 人设决策接入分寸档位：活跃模式更主动参与公开讨论；懂事、活跃每会话每分钟分别允许 2 次、4 次主动加入，仅成功发送计数，点名与自然续聊不占额度。
- 新增 `replay_message_limit` 配置，默认 500，支持 80–500 条；已归属与未归属消息共用上限，回放页展示保留数量。缩小展示范围不会删除记录，也不扩大模型上下文。
- 同步配置面板、档位说明与使用文档，补充引用身份、参与额度、配置持久化及回放浏览器回归测试。

## v1.5.1 — 全面板视觉与移动端体验优化

- 统一六个面板的页头、导航选中态、卡片层次和操作反馈；手机端主题切换与品牌并排，减少导航区域占用，并增大表单文字。
- 控制台强化会话选中态、指标卡片与操作区，保留长会话标识的两行截断和紧凑高度。
- 参数配置的 `topic_reranker_provider` 改为模型提供商下拉选择，宿主配置页与插件面板均支持；留空仍沿用当前会话模型。布尔配置显示可键盘操作的开关与明确状态，优化分类、编辑提示和窄屏表单。
- 今日读空气突出氛围概览、参与档位与决策记录；分寸台强化参与度调节区与开关状态；记忆小本优化分类、记录卡片和月日并排录入。
- 场景回放优化筛选区、时间线、事件列表与详情弹窗，保留原有筛选、标注和键盘操作。
- CI 浏览器任务增加配置页、次级工作区与全站主题回归，覆盖日夜主题、移动端布局、配置保存与会话切换。

## v1.5.0 — 面板重写：双图层主题与标签工作区

- 六个面板页（console/config/today/manners/memory/replay）重写为双图层样式：`pages/shared/base.css` 收敛为无颜色的纯结构原语，`pages/shared/theme.css` 成为唯一 token 与组件层。新 day/night 调色板（day 背景 `#f5f6f4` / 正文 `#202e26` / 主色绿 `#286548`；night `#0b1419` / `#e8eee8`），旧遗留变量（`--panel`、`--stat-bg`、`--page-wash` 等）统一映射到语义变量，移除纸张纹理，字体栈改为 `Segoe UI Variable Text` 优先。新色值由主题浏览器测试锁定。
- 控制台改为三标签工作区：会话工作台、运行策略、互联诊断以 `role="tab"` 视图切换（页面私有 `pages/console/workspace.js`），面板保持挂载以免轮询丢失输入，支持方向键 / Home / End 键盘导航与 `aria-selected` 同步；console `style.css` 从约 1690 行精简到 133 行。
- today / manners / memory 重写为统一的次级工作区：`wire-card` 卡片布局、会话选择器、参与旋钮按钮、纪念簿标签页（纪念日 / 提醒 / 黑话试验）。新增浏览器契约测试覆盖三页 × day/night × 1366/375 无溢出、夜色卡片底色、旋钮写入与过期读取保护、分寸开关键盘操作与保存提示、纪念簿增忘 / 草稿加载 / 标签方向键导航 / 过期会话响应不回灌。
- `base.css` 纳入全员同步清单（console 不再例外），`sync_page_assets.py` 与发布校验同步更新；`SYNC_INTO_PAGES.md` 说明改为六页发布。
- 响应式收紧：≤600px 概览指标两列、≤360px 单列；console 320px、各页 1366/375 宽度下无横向溢出，触摸目标保持 ≥44px。config 分类页断言宽度调整为 375px 并补充脏值回退与空搜索用例；replay 甘特图新增窄屏响应式检查与无网络重载下的客户端话题过滤用例；console 相关浏览器测试补齐标签页导航步骤。
- 视觉修复：分寸台空的保存提示条不再以悬浮胶囊形式遮挡「社交与作息」卡片；today/manners/memory 的观察会话下拉在窄屏占满整行，选项文本不再被裁剪。
- 修复 Embedding 资源计数在 Python 3.10 及以下把超时记为普通失败的问题（`asyncio.TimeoutError` 与内建 `TimeoutError` 在 3.11 才合并）；测试用 `VirtualClock.advance` 现在会等完成级联沉降并触发窗口内迟注册的定时器，端到端与场景回归在本机 Python 3.10 上恢复稳定。

## v1.4.3 — 参与策略拆分、等待回答状态与有状态回放

- 参与评分从 `AddressivityRouter` 抽出为纯策略 `ParticipationPolicy`：适配器只采集身份、引用、时间、插话数量、语义匹配、线程与 hover 事实，策略不访问 DAG、时钟、模型或正文，也不重复调用身份与收件人推断。默认阈值、全部权重、理由文本与 0.28 hover 上限完全不变，抽取前的 29 组公共结果固化为 `participation_legacy.json`，行为等价由回归锁定。
- 新增结构化证据账本 `Evidence(code, family, strength, source)`：记录实际加减分贡献，按 baseline / recipient / platform / temporal / dialogue / topic 六族求和，决策记录只导出已知 code、family、source 与有限数值。
- 新增只读 `ActiveDialogue` 投影：从 `last_bot_node` 沿 Bot 分片链回溯最初的用户触发消息，记录 `last_bot_was_question`，不新增需要在发送、取消、重置、裁剪时同步的第二份可变状态。Bot 提问后，目标参与者的第一条不间断回答（60 秒内、无句末分隔、其间无人插话）作为结构性证据，可识别 `1.21.4` 这类无任何语义重叠的回答；明确 @ 他人仍在更早层级胜出，未发送的草稿不构成锚点。
- 修复路由证据残留：话题被 burst、待定后续或 LLM 重排确认后，`topic_ambiguous` 与 `topic_not_formed` 会从 evidence 中移除，最终结论与最终理由不再互相矛盾；重排同时补齐 `addressee_ambiguous`，话题提交不改变收件人结论。
- 回放评估改为有状态执行：逐条按生产顺序“过期缓冲 → 对已提交状态评分 → 提交本条决策”，复用 `SessionRuntime.commit_participation`；Bot 消息只作为锚点参与，不评分。报告新增逐条结果与实际状态（pending hover、对话对象、是否等待回答、中间发言者），trace 不再使用占位值。
- 夹具支持逐条期望与确定性时间偏移（`at`），锁定跨消息边界：旁观者连续两句仍为 hover、同一批消息由对话对象发出则升为 strong、对话对象回答后第三方保持 hover、超过 TTL 的 hover 在评分前失效。
- 新增话题与父消息指标：会话内相等关系比较（与标签命名无关）得出 Wrong Merge / Fragmentation 与精确率/召回率，Parent 精确匹配与覆盖率，候选列表另出 R@1/3/5；缺标签一律不计分，`--check` 只在有监督数据存在时约束；收件人混淆矩阵支持按场景分组并输出精确率/召回率。
- 新增资源计数：Embedding 的 provider 调用、超时、失败、无 provider、缓存命中、单飞合并、超限回退与在途占用；上下文构建输出当前/背景/序列化字符数。两者都不包含正文、昵称或 provider 错误文本，也不进入 LLM 载荷。
- 回放隐藏正文时一并脱敏 Bot 消息标识；决策记录补充 `waiting_for_answer`、`last_bot_was_question`、`last_bot_message_id`。
- 新增文档：[参与策略](docs/participation_policy.md)、[路由证据一致性](docs/routing_evidence.md)、[资源观测](docs/routing_observability.md)。

## v1.4.2 — 收件人契约与话题歧义解耦

- 话题歧义不再否决已明确的收件人：路由分别输出 `topic_ambiguous` 与 `addressee_ambiguous`，Addressivity、消息语义与参与判断只读取收件人歧义；旧快照按置信度兼容，聚合字段仅保留诊断与异步升级用途。
- 统一 Bot 身份识别：新增 `BotIdentityMatcher`，平台提及、呼语与讨论对象各自独立判定；单字中文昵称按句首、标点与命令/问句结构识别，`显卡`、`助手席` 等子串不再误判。快路径、消息语义、Addressivity 与人设共用同一实现。
- 支持多收件人：`@小明 群间你怎么看` 同时记录人类与 Bot，显式 @、平台唤醒与呼语合并为一个收件人集合，不再丢弃第二收件人。
- 收件人推断收敛到 `RecipientResolver` 的不可变结果，引用他人但在对 Bot 说话、进行中对话续接等分支保留原有证据与阈值；纯函数调用不修改会话状态、待定缓冲或话题。
- 新增决策记录：普通与人设两条链路输出 schema 2 决策快照，回放详情可展开查看；隐藏消息内容时清空收件人标识、隐去对话对象并只保留中间发言者数量。人设路径在模型决策与参与闸门之后才写入是否回复。
- 新增固定离线回放评估器与合成基准：14 个场景在普通与人设模式各出一份漏判/误判矩阵，报告带配置与夹具哈希、权重版本；启动时断言加载的是仓库根目录实现，避免测到被忽略的嵌套旧副本。CI 增加 `--check` 门禁，比较报告只读且只比较双方都记录过的字段。
- 收件人标注 schema v2：原话题标注四字段保持兼容，新增收件人是否正确、是否对 Bot 说话、收件人与讨论对象 ID、是否应回复及收件人错误类型；保存时快照当时的诊断与决策记录，实时状态变化不改写已标注内容，正文默认不保存。
- 统一 Agent 上下文构建：自有请求与宿主钩子共用一份归属数据，每条背景消息统一截断 1200 字；自有请求只跳过重复的会话归属注入，氛围、自学习、媒体提示与人设钩子保留。
- Embedding 不同文本的在途请求上限为 8，超出立即回退 hashed 而不排队，同文本等待者仍共享请求；重配置后过期结果与异常不能写回当前代次；匹配结果自带 backend 归属，不再依赖共享的 last_backend。
- 话题神经评分不再计算随即丢弃的哈希向量，也不再重复话题画像已完成的时间窗过滤；现有画像缓存、评分阈值与召回范围保持不变。
- 快路径补充：显式“还没说完/等我说完”仍进入防抖，句首昵称之后的剩余内容参与是否快速放行的判断，呼语清理只删除识别到的位置。
- 新增设计文档 [docs/v1.4.2-group-chat-optimization.md](docs/v1.4.2-group-chat-optimization.md)，逐项标注本版落地范围与推迟项（独立参与策略、进行中对话显式状态、结构化证据、话题与父消息指标、Top-K）。

## v1.4.1 — 作息唤醒与输出容错修复

- 修复白天普通晚安导致长时间入睡；记录实际苏醒时间，恢复醒后窗口，并保持强制入睡语义。
- 新增 `rhythm_timezone` IANA 时区设置，统一作息时段、每日配额与失眠判断，留空兼容系统本地时区。
- 决策输出支持一层完整 JSON Markdown 代码围栏，继续严格校验字段、大小和引用目标。
- 回复分段保留中文标点及原英文空格，合并时不再插入多余中文间空格。
- 会话 JSON 损坏、编码错误或读取失败时告警并降级为空状态，保留原文件及 UMO 归属检查。
- 增加单字中文交错对话回放，验证明确引用、@ 机器人及旁观短句的对象边界；词元算法保持保守，等待真实标注数据评估。

## v1.4.0 — 会话路由提速与生成链路加固

- Embedding 与话题 LLM 等待移到会话锁外，显式唤醒走快速路径；标题后台生成，支持在途去重与失败退避重试，过期结果不会覆盖新会话状态。
- 缓存话题画像、仅刷新受影响话题，减少重复质心和关键词计算；优化 DAG 上下文排序，避免重复线性查找。
- 分离 thread/topic 身份，统一称呼识别；父消息检索使用真实交互证据，高置信自然续聊可以回填待定话题，保留未成题引用和对象证据。
- 新增独立的话题加入、歧义和分差阈值，保留旧参数默认行为；配置只在变化时同步，空闲期不再重复序列化与保存面板状态。
- 普通回复默认 60 秒总超时、工具 Agent 与人设完整处理默认 120 秒；超时和取消释放任务与名额，旧任务不会清除新任务所有权。
- 人设桥接不可用时启动降级到规则模式，并在控制台显示原因；主动启用前仍校验宿主能力。
- 情绪与群记事本使用宿主稳定数据目录，文件名追加 UMO 哈希并校验记录身份；旧文件复制保留、不覆盖目标，缺少 UMO 身份的历史记录不自动归属，避免跨会话串数据。
- 补充并发、超时恢复、存储隔离及浏览器回归；限制 pytest 默认收集范围，CI 增加异步静态规则与基础模块类型检查。

## v1.3.12 — 响应提速、强唤醒修复与简化配置

- 参数页默认收敛为 7 项常用设置，增加生效范围提示和高级设置切换；保留全部参数、搜索与未保存输入。模型列表暂时不可用时仍可读取和保存配置。
- 修复较弱后续消息误使正在生成的强唤醒回复失效，以及生成任务启动、收尾时待处理请求丢失的竞态。
- 支持只有 At 组件、正文为空的机器人呼唤，保留防抖合并；@ 他人不触发此补偿。
- legacy 延迟回复将模型生成耗时计入首段等待，减少重复模拟等待，后续分片节奏和配置默认值不变。
- 新版 AstrBot 人格解析器负责会话人格读取，移除桥接器重复读取，保留旧版兼容路径。
- 统一六个页面的日夜配色、导航图标和手机布局，保留现有表单与页面功能。
- 自学习发送后通知使用独立的后台超时预算，避免正常处理被前台读取超时打断；过期回调不再覆盖当前诊断。

## v1.3.11 — 参数分类与主题布局

- 参数页将 82 项设置归入 15 个明确分类，话题参数不再放在“其他”。
- 拆分社交分寸、主动参与、媒体和记忆设置，新增分类跳转及按分类名称搜索。
- 调整桌面双列与手机单列布局；跳转、搜索保留未保存输入，配置名称和默认值不变。
- 插件主题使用独立属性，避免 AstrBot 宿主主题更新覆盖插件的日夜配色，并同步各页面资源。

## v1.3.10 — 话题归属与审计修复

- 成题前保持未知话题，过滤媒体占位与重复内容；回放展示确认后的话题，避免无效话题块。
- 修复话题回填后的歧义状态不一致，保留各分片独立的引用和收件人。
- 语义特征采用有界缓存与预编译正则，减少逐条路由的重复计算；诊断日志不再包含正文及身份字段。
- 配置保存前检查宿主能力，失败或取消时恢复旧值，避免不可用的人设模式落盘；仲裁例外遵循实际阈值。
- 页面首屏提前应用日夜主题，改善资源加载期间的背景闪烁；补齐 HTML 转义。
- 发布检查加入共享页面资产一致性校验，补充配置、路由、隐私及浏览器回归测试，并更正审计报告的宿主鉴权结论。

## v1.3.9 — 设置页优化与面板持久化

- 设置页优化桌面和手机排版，增加设置搜索、全部展开/收起，并记住分组状态。
- 会话消息图、话题归档、统计、氛围和最近判定通过插件 KV 持久化，正常重载自动恢复。
- 每 30 秒保存一次，卸载前保存，清空会话同步更新存储；未发送任务不恢复。
- 恢复时保持 UMO 隔离、换算停机时间，并恢复迟到回复关系；损坏数据跳过处理。

## v1.3.8 — LLM 话题聚合与自动标题

- LLM 辅助覆盖模糊归属和拟新建话题，结合候选摘要及最近对话减少话题碎片；超时保留本地判断。
- 每个活跃话题生成一次简短中文标题，后续消息沿用，归档后保留；重置期间的过期结果不会写回。
- 场景回放显示生成标题，隐藏消息内容时仍使用匿名编号。
- `topic_reranker_enabled` 默认开启；已有显式 false 配置保持关闭。复用 `topic_reranker_provider` 和 `topic_reranker_timeout`。

## v1.3.7 — 流式话题归属与人工纠错

- 模糊消息先暂存，后续明确证据可回填，超时保持未知，避免污染正式话题。
- 启用话题质心、代表消息、候选上下文与边界检测，并加入可选 LLM 复判和会话内历史摘要召回。
- 场景回放支持人工纠错、错误类型统计及 JSON 导出。
- 修复路由测试残留的未使用导入，恢复完整 Ruff CI 检查。

## v1.3.6 — 场景回放时钟对齐与甘特图交互优化

- **回放时间校准**：将对话图谱节点的单调内部时钟换算为绝对真实时间戳（wall time），使消息节点与场景判断事件在甘特图时间轴上准确重合与关联。
- **甘特图交互优化**：为回放色块引入自适应最小宽度（84px）与弹性约束（`clamp`），防止短场景和瞬时事件在移动端与小屏下挤压为不可见细条，提升触控与点击交互体验。
- **端到端契约**：补齐时间换算单元测试与浏览器端响应式布局尺寸断言。

## v1.3.5 — 主题回放与戳一戳 LLM 回复

- 场景回放改为按群聊主题排列的甘特图，强化日夜主题配色；点击色块打开详情，查看开口情况及逐次判断。主题名称遵循内容显示设置，旧判断按时间关联或归入未关联主题。
- 修复回放请求乱序及失败时残留旧详情的问题。
- 插件作者统一为 `ysyhlly`。

- 戳一戳文字回复改用已配置的回复 LLM，根据当前会话上下文和连续戳动次数生成短句；保留戳回与防刷屏策略，移除固定模板词。模型失败或返回空内容时保持安静，重置期间生成的过期回复不会发送。

## v1.3.4 — 品牌定名「群间」· CI 修复与发布契约

### 品牌与文档

- 插件定名「群间 · Chat Dynamics」：`metadata.yaml`、`@register`、插件市场与控制台 i18n 统一新名称与新简介。
- README 重写为用户向文档：快速开始、两种互动模式对照表、常用配置、控制台指令与使用边界。

### CI 修复

- 控制台搭档快照按 `native_request.hooks_available()` 区分 native 分发能力：legacy+exclusive 且宿主钩子不可用时才显示 `native_hooks_bypassed`。
- 单测替身隔离宿主请求钩子：无注册钩子的环境不再误报 native 分发失败；真实分发契约由 integration/ 覆盖。
- 修复戳一戳兼容性测试在真实 AstrBot SDK 下 `sys.modules` 未导入即取键的 KeyError。
- 新增约 90 个行为测试补齐覆盖率门槛：作息状态机、控制台快照、native 请求钩子、主动插话、媒体/平台桥接、群记忆、投递回执与主插件流程。

## v1.3.3 — 今日作息 · 面板视觉 · 搭档互联（首个公开发布版）

### 今日作息

- Keep 晚安 stickers in daily rhythm (media listen no longer skips wind-down); honor `group_memory_enabled=false`; keep native Image/Record instead of flattening to plain text; config page shows quota 0 as 0.
- Default plugin pages to a daytime paper-white UI; keep the previous night observatory as a toggle (`日间` / `夜间`).
- Pass Image/Record to the host model on @/quote: do not swallow addressed media-only turns, attach files on native `tool_loop_agent` / persona agent events, and request understand even when L2 is off.
- Keep `@register` version in lockstep with `metadata.yaml` (`v1.3.3`).
- Keep winding/asleep across local midnight; morning still wakes. Dashboard `status()` follows the same rule.
- Commit brief-wake counts and `brief_wake` state on send (`note_spoke`), not on `evaluate`.
- Let whitelist gap-fill / 晚安 / 早安 override the default no-ambient arbiter gate; cooling, private-topic and energy-asymmetry still win.
- Detect hanging questions behind the current flush tip so live DAG gap-fill works.
- Honor `proactive_quota_per_hour/topic = 0` and `rhythm_day_share_slots = 0` instead of coercing them back to defaults.
- Keep `@register` repo slot empty (5th arg is the update URL, not the long description).
- Do not let `no_gap` veto a rhythm `day_share` act; console quota 0 stays 0.
- Wire group-notebook snippets into rhythm/day-share; implement night self-sleep when `rhythm_allow_self_sleep` is on.
- Add per-session daily rhythm state machine (`awake` / `winding_down` / `asleep_*` / `brief_wake` / `insomniac`).
- Hard split: saying goodnight enters **收束中**, never jumps straight to asleep; cold 20–40min then sleep; hot chat delays.
- Goodnight text quota default 1 (max 2); first wave may reply, later silence; asleep ambient proactive=0; plain goodnight ≠ wake.
- Wake whitelist (@/quote/help/media/command) → one short reply → brief_wake cooldown → sleep again.
- Optional morning hi + day share slots (0–2) using proactive quota; prefer gap-fill/memory; insomnia default OFF.
- Priority: deciding > manners/media > rhythm > quota. Atmosphere prop, not a real calendar.
- Dashboard read_air status + why_silent codes; manners chips for rhythm switches.

### 面板视觉与主题偏好

- 六个后台页面共用颜色、字体、侧栏、卡片和控件样式，统一日夜模式并适配窄屏。
- 日夜偏好按后台账号存入插件 KV，沙箱禁用 localStorage 时也可恢复；跨页保留当前主题，修复旧 URL 参数覆盖选择的问题。
- 保存失败可重试，连续切换串行保存；延迟返回的偏好不会覆盖用户的新选择。

### SelfLearning 互联补齐

- 三类直连能力全部进入主模型背景，保留批准记忆正文与黑话释义，逐一测试 11 个兼容方法名；保留 UMO/用户边界、审批、长度限制以及忘记/静音检查。
- legacy + exclusive 补上宿主请求钩子分发，兼容 SDK 4.16/4.27；事件副本解除停止状态，原事件保持停止。
- 诊断展示输入采集和管理命令入口；原生学习采集、命令权限仍由宿主管理，不重复自动执行。
- 将已适配的关系提示接入主模型请求，限定当前 UMO 和用户；逐搭档跳过原生注入，保留长度限制和迟到结果检查。
- 自管发送成功后异步调用 SelfLearning 的 `on_bot_message_sent`，传入实际送达片段的独立事件视图；失败不采集、写入不重试、卸载取消并等待。
- 诊断增加 `direct_methods` 和 `delivery_hooks`，区分兼容方法名与实际发现的接口。

### @ 双回复

- 接管事件在 LLM 请求钩子再次拦截额外的宿主请求，覆盖 filter 下其它处理器显式发起请求或改写禁止标志的情况；插件自己的 Agent 使用独立请求标记，正常继承人设和请求钩子。
- 补齐 exclusive 模式的默认 LLM 禁止标志，避免停止结果被清空后，@／引用消息同时进入宿主默认请求和插件延迟回复；重复消息拦截也使用同一保护。
- Forbid AstrBot's default @/wake agent with `should_call_llm(True)` (`call_llm=True` on 4.16/4.27). Incomplete/short/`persona_model` @ no longer dual-sends with the host pipeline.
- Discard a pending incomplete-@ debounce buffer when a later complete @ takes the native fast path.
- Observe streamed native @ answers onto the DAG without rewriting `STREAMING_FINISH` into a second send.

### 戳一戳

- Stop classifying QQ poke / 戳一戳 as a media attachment (`[媒体附件]`).
- Treat poke-at-bot as a social tap: short local replies with more variety, optional poke-back, and streak-aware annoyed lines. Poke-at-others stays quiet and is not sent to the vision/media gate.
- Stop the poke event after one reply so the host pipeline cannot send a second copy; never send a spoken line and a poke-back together.
- Leave other plugins' commands alone after AstrBot strips wake prefixes (`/签到` arrives as `签到`); do not take over or decorate those turns.

### 场景回放

- Ship the scene-replay page as a color-block timeline of why the bot spoke or stayed quiet, with no plaintext and optional presence knobs.

### 审计复核修复

- Sanitize UMO filenames (strip `:`) and write mood/notebook JSON atomically so Windows persistence no longer fails silently.
- Map monotonic plugin clocks onto civil local time for daily-rhythm hours; keep wall-clock tests unchanged.
- Persona-model `decision_gate.evaluate` / `note_spoke` use `wall_time()` so morning/goodnight/day-key/mute windows follow civil time; ambient interval math stays monotonic.
- Persist deep cooling as wall-epoch expiry and restore onto the new process monotonic clock so a restart cannot stretch 15 minutes into many hours.
- Keep CHILL_FADE in the 5–7 MPM hysteresis band instead of flapping into FAST_BANTER.
- Release persona `model_admission` if submit is cancelled before the turn is queued; stamp fast-path flushes with debounce generations after `/dynamics_stop`.
- Do not treat noun `结果`, `90's`, or `5'10"` as incomplete; ignore hanging questions that already have a human answer; keep session sweeper alive after prune errors.

### 记忆搭档互联修复

- Discover live companion instances through AstrBot `StarMetadata.star_cls`; inspect Self Learning and LivingMemory independently, and refresh after initialization/reload.
- Distinguish native request-hook integration from optional direct APIs. Expose per-plugin capabilities/errors instead of treating a missing guessed API as a broken native integration.
- Await scoped direct APIs with a bounded timeout, preserve user arguments, retain failure diagnostics, and cancel/await pending IO on unload.
- Consume opt-in approved mood tags before requests without duplicating native companion injection; await slang approval before notebook writes and reject unapproved generic candidates.
- Honor temporary content-part flags when saving owned Agent history, preventing companion memory hints from being persisted again.
- Add companion regression tests and real AstrBot 4.16/4.27 metadata/dispatcher/runner coverage; update existing SDK smoke expectations for the current route set and media opt-in.

## v1.3.2 — 读空气深化 × 有用主动

- Add occasion sub-state `deciding` (schedule/vote/pick/分工); priority conflict/cool > deciding > help > vent > banter; uncertain ≠ deciding.
- Add useful proactive module: gap-fill whitelist (hanging Q / appointment gap / help follow-up / cold memory nudge), tight hour+topic quotas, same-gap-once.
- Cold-field public-memory nudge **lively-only**; missing group_memory degrades quietly.
- Newcomer caution + pace align (delay/length only; never grants more proactive).
- Wire into decision_gate after manners/media/occasion; why-silent codes: 决策中不插科 / 主动配额用尽 / 新人·更收 / 等待缺口闭合.
- Dashboard read_air exposes deciding + proactive_used/cap; manners chips for gap-fill / cold nudge / newcomer.

## v1.3.1 — 媒体读空气

- Add L1 media air gate for image/voice (defaults ON): short labels steer speak/force; uncertain → listen.
- Add L2 understand-reply switch (default OFF) so only strong relevance pushes multimodal to the main agent.
- Privacy-strict default: private/ID-sensitive media skips detail description and memory; prefer silence unless strongly addressed.
- Console/config reuse existing classes for media toggles + multimodal degrade note; why-silent surfaces media reasons.
- Heuristic-first offline path; quiet degrade when vision/STT/multimodal missing.

## v1.3.0

- Add presence knob (`ghost|sensible|lively`) and social manners (relay baton / private field / hyped quota) with conservative defaults.
- Add occasion skin (serious_help / banter / vent / conflict / neutral) and Chinese why-silent reasons on the console.
- Optional selflearning bridge with quiet degrade; mood/topic tags + group notebook (anniversaries / one-shot reminders / slang trials).
- Console gains today read-air summary, partner lamp, presence control and notebook lite — reusing existing visual language (no reskin).
- Config page groups new switches under「你想 bot 怎样」.

### persona-driven interaction

- Add opt-in `decision_mode=persona_model`, an immutable complete-turn context and validated participation decisions using the effective AstrBot persona.
- Use the real main-agent builder/runner for persona, history, tools and attachments; keep legacy as the migration default and fail explicitly when the bridge is unavailable.
- Preserve distinct users' queued requests, invalidate same-user stale drafts, enforce decision timeout/concurrency and ambient-opening budgets, and support decision-only shadow observation.
- Commit only delivered reply content; keep tool execution receipts separate, preserve media tool results and avoid persisting internal decision JSON.
- Fix lost debounced text, missing selected context, short-reaction incompleteness false positives, negated emotion labels, arbitrary emoji fallback and destructive Markdown emphasis cleanup.
- Add real SDK builder/runner contracts for AstrBot 4.16.0 and 4.27.5, and end-to-end offline lifecycle/queue/send regression scenarios.

### UI wireframe appendix

- Add separate AstrBot pages: 今日读空气 / 分寸台 / 记忆小本 (+ 场景回放 P2 stub).
- Reuse console CSS via `pages/shared/base.css`; keep UMO console as observatory with quiet links.
- Extend `read_air` with confidence/thermometer/decisions; notebook `remove_slang` / `mark_done`.

## v1.2.0

- Stabilize UMO-isolated concurrent generation, reset/cooling ordering, and unload cleanup.
- Add bounded input/session retention, shadow observation mode, provider compatibility split, console presets, and privacy-safe diagnostics.
- Add session-scoped state/send locks, epoch barriers, bounded session/text retention, and a single-writer cooling persistence worker.
- Split explicit `reply_provider` and `vibe_provider` while retaining the legacy `provider` fallback; add `shadow_mode`, privacy-aware console snapshots, rate limits, and preset APIs.
- Keep `vibe_llm_enabled=false` as the public default and add a short failure backoff for invalid calibration labels.
- Make pure media events suppress native LLM in filter mode (or stop exclusive events), keep mixed text/media turns, and record successful native follow-up fragments in the DAG.
- Require real SDK smoke collection in CI, add coverage and release-package gates, and redact message content in the console by default.

## v1.1.0

- Honor `ambient_intervention` in filter mode: weak, high-value turns can be handed back to the native agent; hover / cooling / private topics stay silent.
- Persist deep-cooling timestamps in plugin KV so a reload does not unmute a cooled room.
- Default `pipeline_mode=filter`, disable ambient intervention and vibe LLM calibration, skip debounce for complete @/quote turns, and pass media through.
- Treat polite `好的~~` tails as complete, split acknowledgements from low-effort fillers, and stop treating snake_case as Markdown italics.

- Add optional AstrBot EmbeddingProvider access: neural vectors are cached in-process, semantic edges can use cosine once both sides are ready, and hashed embeddings remain the default fallback.
- Replace lexical-only semantic edges with hashed n-gram embeddings plus a synonym-group concept classifier, still without third-party model weights.
- Replace hash-of-reply Emoji with a scene/emotion acknowledgment policy that stays silent for serious, private, technical, and long replies.
- Assign explicit `thread_id`s, add mention and semantic candidate edges, include sibling branches in thread context, and keep a same-user hover queue for re-evaluation.
- Preserve every debounced source message in the DAG, reconcile out-of-order replies, reject cycles, chain bot fragments, and avoid whole-room context fallback.
- Re-evaluate safe-hover turns when the same user supplies a related follow-up within 120 seconds.
- Add session-scoped debounce discard generations so reset cannot re-inject buffered pre-reset input.
- Split Unicode Emoji and media telemetry, add conservative scene/emotion labels, and include the rolling window plus telemetry in LLM vibe calibration.
- Expose telemetrics window, banter/chill MPM thresholds, and WTS feature weights in the admin schema.
- Expand WTS diagnostics with topic relevance, professionalism, question value, unique-speaker participation, five-minute fatigue, and a conservative private-topic boundary.
- Add soft fragment length control, dynamic inter-burst intervals that follow fragment length and group rate, deterministic casual phrasing, and optional Emoji reactions.
- Align runtime configuration validation with `_conf_schema.json`, including upper bounds and strict integer validation for `max_fragments`.
- Treat an explicit `False` from either AstrBot send path as a failed send.
- Add a default-disabled `vibe_llm_enabled` switch, prevent duplicate concurrent classifiers per UMO session, and expose calibration source/count/age in the console API.
- Reject invalid `/dynamics cool` durations instead of silently applying 15 minutes.
- Document exclusive takeover, UMO session identity, media handling, LLM calibration, retention limits, and API response envelopes.
- Add real AstrBot 4.16/current-4.x SDK smoke tests and GitHub Actions coverage.

## v1.0.1

- Added UMO-isolated runtime state, lifecycle-safe task cancellation, successful-send-only bot state, validated configuration, and the plugin console APIs.
