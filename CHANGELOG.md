# Changelog

All notable changes to this plugin are recorded here.

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
