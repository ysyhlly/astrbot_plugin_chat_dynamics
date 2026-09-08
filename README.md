# AstrBot 细腻群聊动态过滤器 (Chat Dynamics)

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue.svg)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.12+-green.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-purple.svg)](LICENSE)

> **默认是过滤器，不是替代者。** 对生效群聊做防抖、指代门闩、冷却和回复装饰；主回复仍走 AstrBot 原管线（人格、会话历史、工具、图片）。只有打开 `pipeline_mode=exclusive` 才会 `stop_event()` 独占吞掉其它插件。

## 人设驱动互动（可选新模式）

识图要求优先提供具体角色名与作品、物体名称及有依据的品牌型号，并保留图中文字、位置、识别依据和不确定项，再交给主模型。无法确认的名称不强行编造；真人照片不凭脸猜身份。人设模式在当前回合的独立识图提示中加入这些要求，不修改宿主全局配置；主模型直接看图时也使用这些要求。默认过滤模式若宿主已先生成图片描述，会使用当前 UMO 配置的独立识图 Provider 补充一次详细识别（最多等待 30 秒），成功后把结果交给主模型，失败保留原描述。没有独立描述时不额外调用识图模型。

消息上下文携带发送者、话题、候选父消息、收件人及各自置信度。实际 @ 对象优先决定收件人，引用作者单独保留；例如引用群友并 `@bot 他说得对吗`，讨论材料来自群友，提问对象是 bot。没有明确收件人时，模型可结合话题与轮次推断；仅仅谈论机器人不等于向它提问。分析限定在当前 UMO。

短期会话路由默认开启：在最近几分钟的有限消息窗口中检索话题与候选父消息，短回复优先使用同一作者的上下文，避免被其它话题的最后一句带偏。只有置信度和候选差距足够的推断才建立回复边；神经向量到达后可修正推断，平台引用与 @ 关系保持优先。详见 [会话路由说明](docs/conversation-router.md)。

这些数据会进入默认过滤模式的主模型请求、独占模式的回复请求，以及 `persona_model` 的决策和主 Agent 回复上下文。主模型结合正文理解实际语义；本地推测不保证识别人名暗指、反讽或省略的收件对象。

设置 `decision_mode=persona_model` 后，完整回合先由模型判断是否参与、回应对象、互动状态与长度，再由 AstrBot 主 Agent 生成回复。人设沿用当前 UMO 实际生效配置；没有第二份插件人格。默认 `legacy` 不变，升级不会自动增加模型调用或主动插话。

新模式的状态包括旁听、闲聊、专注、支持、玩笑、收尾；表达方式由当前人设决定。未被 @ 的消息也可进入模型决策。`ambient_intervention`、WTS 门槛、窗口级私密标签、自动能量冷却和氛围 LLM 不再决定是否开口；群速、指代等仍作为参考。管理员冷却是硬边界，期间不调用模型。

| 新配置 | 默认值 | 行为 |
| --- | --- | --- |
| `decision_mode` | `legacy` | 可切换为 `persona_model`；不兼容的宿主桥接会明确报错，不降级为无人格回复 |
| `decision_provider` | 空 | 决策 Provider；为空沿用 `reply_provider` → `provider` → UMO 当前 Provider |
| `decision_timeout` | `8.0` | 1–30 秒，包含全局排队；一次调用、不重试 |

每个会话最多一个决策执行，全局最多四个，单会话另有八个待处理槽位。过载的环境消息静默；明确请求采用队列背压保留，轮到时跳过决策，保守交给 Agent。决策超时或结果不合法同样只为明确求助回退。决策模型不调用工具；只有回复 Agent 可以使用宿主允许的工具。

环境主动开场每分钟最多两次；最近两分钟同一参与者继续接话不算新开场。默认单条回复，长段落必要时拆成至多三条；代码、公式、链接和列表不硬切。不自动加 Emoji，不额外把长文改成口语。首条额外等待最多一秒，片段间隔最多 1.5 秒。

完整碎发、来源、引用和附件交给 Agent；纯媒体不再一律拦截，媒体理解能力仍取决于宿主版本、适配器和 Provider。DAG 背景按引用和参与者筛选。人设/会话切换、同一用户补充、reset/cool/unload 会使旧结果失效；不同用户的明确请求分别排队。已开始的外部工具动作不能随回复取消而撤销，不自动重试；临时执行回执与送达历史分离。

本轮 3 的边界保持按模式区分：`legacy` 的强指向只清理当前用户的 hover；`persona_model` 中 `@bot` 即使没有平台 Reply 也可沿明确的 mention 父边续聊。`legacy` 原生分段发送时，同一 UMO 内同一用户的新强指向消息（@、昵称或引用）会停止尚未发送的尾段，其他用户或弱指向消息不会打断，已发送内容会保留。本轮不新增配置或命令。

控制台显示最近一次决策动作、状态、目标消息 ID、耗时、失败分类及队列长度；不会展示模型思维过程。`shadow_mode=true` 会调用决策模型并显示预计行为，但不生成回复、不改动宿主回复或提交人格互动状态。因此观察模式仍会产生决策调用费用。

迁移顺序：保持群白名单 → 设置新模式并启用 shadow → 检查决策与桥接状态 → 在单个测试群关闭 shadow → 用对话回放比较相关性、打扰程度、人设一致性和延迟。回滚时切回 `legacy`，在途回复会失效。真实 SDK 契约测试使用离线 Provider 验证主 Agent 构建与运行，不能替代真实群适配器、媒体与实际模型的 staging 验收。

以下五阶段与 WTS 说明主要描述 legacy 模式。legacy 的延迟回复仍使用旧调用方式；只有原生直通路径和新模式的主 Agent 桥接保证加载宿主人格/历史/工具，不应把旧 `tool_loop_agent(event=...)` 包装理解为完整宿主管线。

---


## selflearning / LivingMemory 可选搭档

Chat Dynamics 管回复时机和分寸；[Self Learning](https://github.com/NickCharlie/astrbot_plugin_self_learning) 与 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 的公开主线通过 AstrBot `on_llm_request` 钩子注入学习内容和记忆，不要求它们提供 `memory_api`。

| 控制台状态 | 含义 |
| --- | --- |
| 原生钩子 | 已发现启用且初始化就绪的搭档；由 AstrBot 分发钩子。表示接入方式，不代表本轮一定召回到记忆 |
| 接口已发现 | 第三方分支提供已识别的直连接口；是否支持记忆、关系或黑话分别列出，调用错误会保留在状态中 |
| 降级中 | 初始化未完成、接口不支持或最近直连调用失败；查看每个搭档的诊断，不阻断正常回复 |
| 未安装 / 已关闭 | 未发现启用的搭档，或关闭了 dynamics 的可选桥接 |

默认 `pipeline_mode=filter` 保留平台原生回复链路；`decision_mode=persona_model` 的自管 Agent 也会分发请求钩子。搭档负责自己的记忆召回、关系和黑话注入，dynamics 不再重复调用这些原生钩子。关闭 `selflearning_integration` 只关闭本插件的发现和直连功能，不会禁用其他插件在 AstrBot 注册的钩子。

提供直连 API 的分支需接受 UMO 会话参数（`umo`、`unified_msg_origin` 或 `session_id`）；支持同步与异步方法，用户参数按签名传递。读取已批准记忆/黑话时，通用查询结果必须明确标记 `approved=true` 或 `status=approved`。不会退回无会话范围的全局查询。直连异步 IO 有 2 秒等待预算，卸载时取消并等待。

`selflearning_integration=true` 时，三类直连数据全部进入主模型：批准记忆的正文、关系提示、批准黑话及含义，分别最多四条、每字段最多 400 字；不是发言阈值或系统指令。按搭档分别跳过原生注入，避免重复召回。读取限定当前 UMO，用户相关数据进一步限定当前发送者，异步读取预算为两秒。本地忘记或静音会阻止远端记忆正文再次注入，等待期间发生的忘记同样生效。

`mood_memory_enabled=false` 和 `slang_trial_enabled=false` 默认值不变，分别控制本地情绪追踪和黑话试用写入，不阻断已启用互联的批准背景读取。关闭 `selflearning_integration` 可关闭本插件直连读取和自管送达通知；其他插件自己的宿主钩子仍由其自身配置管理。

当前适配 3 类直连能力、11 个兼容方法名（批准记忆 4、关系 3、黑话 4），不是 11 个已确认在线 API。诊断快照的 `direct_methods` 列出实际发现的方法。SelfLearning 公开主线通过请求注入与发送后采集两个钩子衔接：默认原生回复由宿主通知；插件自管回复仅在发送成功后异步通知 `on_bot_message_sent`，不上传草稿或失败片段、不自动重试写入。最多保留 32 个待处理通知，单次回调等待 2 秒，卸载取消并等待；调用完成不代表上游数据库必然写入，上游采集开关与过滤规则仍生效。

`decision_mode=legacy` + `pipeline_mode=exclusive` 的 tool loop 现在也先通过宿主分发请求钩子，保留搭档注入的系统提示、临时文本、工具与上下文。复制事件解除停止状态，原事件仍保持停止，避免宿主重复回复；宿主缺少所需钩子 API 时控制台仍显示降级。分寸三件与群记忆小本默认开（纪念日/约定须显式写入才有效）；分寸旋钮默认「懂事」。

[Group Chat Plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus) 是另一套回复调度插件，不是记忆 API。它的主线回复链路也支持平台记忆钩子，但与 dynamics 同群同时接管回复会产生调度竞争；应让一个插件负责该群的回复，不能用“记忆已连接”来判断两套调度兼容。

**页面入口**

- 群聊动态控制台：今日读空气、分寸旋钮、搭档灯、群记忆小本、场景回放（色块时间轨，默认无明文）
- 插件参数配置：「你想 bot 怎样」分组（`presence_knob`、分寸三开关、媒体门闩、记忆开关、`selflearning_integration`）


## 媒体读空气（v1.3.1）

图/语音主要用来判断**能不能插话、用什么力道**（L1 门闩），不是默认看图答题。

| 层 | 默认 | 行为 |
| --- | --- | --- |
| L1 会看图 / 会听语音 | 开 | 短标签：整活旁听、求助可接、隐私跳过、不确定旁听；与 @/引用、谁的场咬合 |
| L2 允许理解接话 | **关** | 仅强相关（@、引用 bot、明确「看/听这个」、求助面向 bot）才把媒体交主 Agent；有额外成本 |
| 隐私严格档 | 开 | 证件/私密感不描述细节、不写记忆；未强点名优先沉默 |

无视觉/听写或宿主不支持多模态时：**仅用启发式门闩**，L2 安静降级，不阻断回复链路。控制台「为什么没回」可见例如：`媒体·别人的场` / `语音·信息量低` / `图片·隐私跳过` / `媒体·不确定旁听`。

QQ 戳一戳不是图片/文件：不会走媒体门闩，也不会写成「媒体附件」。戳机器人只回一次（短句或反戳其一，多种口吻，连戳会变嫌弃），并截停事件避免宿主再发一条；戳别人则安静旁听。其他插件指令（含唤醒前缀被剥掉后的 `/签到`）不接管、不装饰。



## 今日作息（v1.3.3）

氛围道具，**不是真日历**。日间可早安/少量分享；夜晚先陪散场再入睡。

| 能力 | 默认 | 说明 |
|------|------|------|
| 今日作息总开关 | **开** | 每会话状态：还醒着 / 收束中 / 已睡·收束后 / 已睡·自己睡 / 短醒·被吵醒 / 失眠中 |
| 收束 ≠ 已睡 | 硬规则 | 回一句晚安只进收束中；冷场约 20～40 分钟后再睡；热聊推迟 |
| 文字晚安配额 | **1**（最多 2） | 首波可回，后续静默；不占白天捧场配额 |
| 被吵醒 | **开** | 仅 @/点名、引用睡前、强求助/媒体向 bot、命令 → 短回 → 冷却再睡 |
| 睡不着 | **关** | 若开：极低概率 + 周期硬顶，至多一句 |
| 白天分享槽 | **1**（0～2） | 耗主动配额；优先缺口/公共记忆；禁硬编炫耀日程 |

原因码白话：`收束中·晚安已回过` / `收束中·后续静默` / `已睡·普通晚安不吵醒` / `短醒冷却中` / `热聊中·推迟睡点` / `多数人已歇·入睡`。优先级：决策态 > 社交分寸/媒体门闩 > 作息 > 配额。

## 读空气深化 × 有用主动（v1.3.2）

独立增量：更懂「是不是在办事」，主动优先补缺口；新人更收、节奏跟群。

| 能力 | 默认 | 说明 |
|------|------|------|
| 决策中场合 | **开** | 约时间/投票/选型/分工；不插科，只可短澄清；不确定不当决策态 |
| 缺口补全主动 | **开** | 悬空问句、约定缺口、求助后沉默等白名单才主动 |
| 冷场公共记忆轻唤 | **开**（仅活跃档生效） | 无记忆小本不编群史；隐身/懂事不靠此通道主动 |
| 新人更收 | **开** | 提高插话门槛；禁止环境主动点名；不做新人档案页 |
| 节奏对齐 | **开** | 只调延迟与片段长度；不单独授权更多主动 |
| 主动配额 | **开**（2/小时，1/话题） | 用尽后纯响应（只答 @/强相关）；同一缺口只主动一次 |

原因码白话：`决策中不插科` / `主动配额用尽` / `新人·更收` / `等待缺口闭合`。接在现有 decision_gate 门闩后，不另起规划器。


## 🌟 核心痛点与解决思路

在真实的群聊交互中，传统的聊天机器人往往显得十分生硬机械：
- **话没说完就抢答**：人类分段碎发三句话，机器人对前两句分别作答，严重打断话题节奏；
- **公屏多线程交错混淆**：群里不同成员同时在聊不同的事情，传统机器人死板地按时间倒序拼上下文，导致张冠李戴；
- **冷场强行尬聊或抢戏**：群友发单字“哦/6”或聊私密话题，机器人依然不知趣地长篇大论；
- **秒回大段长文**：几千毫秒内吐出包含大量 Markdown 标题、加粗和列表符的严肃文本，瞬间破坏群聊气氛，带有浓厚“客服味”。

**AstrBot Chat Dynamics** 挂在原回复链路前后，按五个阶段过滤，而不是再造一个 bot：

```
[公屏群消息流入]
       │
       ▼
[Priority 100 Hook] ── filter 模式用 should_call_llm(True) 禁止宿主默认 LLM；exclusive 才 stop_event()
       │
       ▼
【阶段 1】输入防抖与碎发聚合器 (Turn-Taking & Debounce Buffer)
  - 默认 3.5s 基础滑动窗口防抖，按 `(UMO session_key, user_id)` 聚合
  - 语法悬挂判定（未完成连词“但是/因为”、破折号、省略号），默认拉伸至 6.5s，单轮最多 12s
       │
       ▼ (完整 Conversational Turn)
【阶段 2】对话图谱与指代路由 (DAG Conversation Graph & Addressivity)
  - 显式 `thread_id`、引用/@mention/碎发边与语义候选边共同维护 DAG；语义边使用哈希 n-gram embedding 与概念分类器，不引入第三方模型
  - 回溯包含祖先链和近邻兄弟分支；没有显式父链时只选取同线程 / 同参与者 / 机器人候选，不混入全群最近消息
  - 定向度打分：强指代 / 安全悬停 / 弱指代；同一用户的后续补充可在悬停队列中累积证据并重评
       │
       ▼
【阶段 3】氛围与能量探针 (Vibe & Energy Analyzer)
  - 物理能量、场景、情绪作为独立读数：MPM、平均字符数、独立发言人数、Emoji/媒体比例、正式标点比例
  - 本地提取技术求助、玩梗、情绪支持、私密边界及正负/紧张情绪标签；窗口和碎梗/冷场阈值可在管理面板调节
  - 施密特触发器滞后状态机：平滑识别 `fast_banter` / `serious_inquiry` / `chill_fade`
       │
       ▼
【阶段 4】插话与离场仲裁器 (Intervention & Back-off Arbiter)
  - 动态发言欲望打分 (WTS)，综合衡量指代、话题相关度、专业度、问题价值、多人参与度、模式门槛与发言疲劳；权重可配置
  - 能量极度不对称判定（连续两轮单字/表情即切断对话，自然隐退）
  - 冷场壁垒深度冷却期（默认 15 分钟，允许配置 0~180 分钟）
       │
       ▼ (WTS >= 动态阈值)
【阶段 5】拟人化行为合成器 (Pacing & Style Shaper)
  - 格式降级：闲聊模式下自动清洗 Markdown 标题、列表、加粗与生硬标点
  - 模拟人类打字延迟：思考停顿 + 动态字数打字耗时 (CPS)；碎发间隔随片段长度和群速率调整
  - 输出碎化引擎：长文本按自然边界或软长度目标拆分，实际最多 3 条；超长严肃回复也受保护
```

---

## 🛠️ 五大核心组件

### 1. 输入防抖与碎发聚合器 (`core/debounce.py`, `core/incompleteness.py`)
- **滑动窗口防抖 (Sliding Window Debounce)**：基于用户级别进行防抖合并，有效解决“打字慢分多条发送”被当作多轮对话的问题。
- **语义悬挂判定 (Incompleteness Heuristic)**：检测句子末尾未完成特征（“因为……”、“主要是……”、“但是”、“——”、“……”），一旦判定为悬挂态，计时器自动从 3.5s 动态延展至 6.5s（最高硬上限 12s），给人类充足打字时间。

### 2. 多线程指代与对话图谱追踪器 (`core/graph.py`, `core/addressivity.py`)
- **DAG 关系图**：每条原始消息独立入图并分配 `thread_id`。显式引用、@mention 和同一防抖回合碎句会继承线程。语义候选边默认使用哈希 n-gram embedding 与同义组分类器；可开启 AstrBot Embedding Provider，用神经网络向量（带进程内 LRU 缓存）识别转述。神经网络未就绪或调用失败时自动回退哈希向量。语义边**不会**把并行话题合并进同一线程。
- **安全悬停策略 (Safe Hover)**：首次落入模糊区间时静默入图并进入悬停队列；同一用户 120 秒内的引用、续接词或词汇复用可累积多条证据并重新判定。

### 3. 氛围与能量探针 (`core/telemetrics.py`, `core/vibe_analyzer.py`)
- **实时指标**：
  - **MPM (Messages Per Minute)**：滑动窗口消息产生速率。
  - **Average Characters**：平均每条消息的字符数。
  - **Unicode Emoji Ratio / Media Ratio**：分别统计 Emoji 与 CQ 图片、贴纸等媒体消息；保留旧的合并比例供兼容使用，不分析普通图片内容。
  - **Punctuation Formality**：包含正式标点的消息比例。
- **状态滞后切换 (Hysteresis)**：使用施密特触发器逻辑，避免在临界阈值附近剧烈抖动切换状态。
- **本地场景与情绪标签**：保守识别技术求助、玩梗、情绪支持、私密边界，以及正向、负向、紧张情绪。
- **低频 LLM 校准**：默认关闭；开启后每个会话至少累计 12 个完整回合，最多每 120 秒调用一次氛围 Provider。校准提示包含滑窗消息、遥测和本地标签。

### 4. 插话与离场仲裁器 (`core/arbiter.py`)
- **发言欲望打分 (WTS)**：综合定向度、话题相关度、内容专业度、问题价值、多人参与度、群氛围和五分钟发言疲劳；权重可在管理面板调节，并在控制台暴露最近一次分项结果。
- **能量极度不对称**：对方连续两轮回复“哦”、“666”、“嗯”等低信息量敷衍内容时，直接切断主动对话链路。
- **冷场与私密边界**：15 分钟深度冷却；保守私密话题标签会抑制环境插话，明确 @bot/引用仍可正常求助。
- **自然收尾**：结束时杜绝任何“随时找我”的机械客服语言。

### 5. 拟人化行为合成器 (`core/pacer.py`, `core/style_shaper.py`)
- **输出碎化**：长文本优先按自然停顿拆分，无标点文本按软长度目标切分，实际最多 3 条；严肃模式仅在内容过长时拆分。
- **打字延迟模拟**：根据字数和思考停顿模拟真实打字时长，告别非人秒回。
- **格式同频降级**：闲聊状态自动剔除 Markdown、标号列表并替换少量书面连接语。
- **认可动作**：可选启用。根据触发句的场景/情绪选择至多一个 Emoji（😂 / 👍 / 🙏 / 👀）；严肃模式、私密话题、技术求助和长回复不加。这是确定性规则，不是表情包资源库。

---

## ⚙️ 配置说明 (`_conf_schema.json`)

可在 AstrBot 管理面板中进行图形化配置：

首次启用建议按这个顺序操作：启用插件 → 填写 `takeover_groups`（或明确开启 `takeover_all`）→ 填完整 `bot_names` → 保持 `pipeline_mode=filter` → 先开启 `shadow_mode` 观察决策 → 在群内使用明确 @ / 引用测试 → 确认指标正常后关闭 shadow mode，再按需开启环境插话、氛围 LLM 和 Embedding。`enable=true` 不代表自动接管所有群；白名单为空且 `takeover_all=false` 时不会处理任何群。

| 配置项 | 类型 | 默认值 | 允许范围 / 详细说明 |
|:---|:---|:---|:---|
| `enable` | boolean | `true` | 是否全局启用本插件 |
| `pipeline_mode` | string | `filter` | `filter`：只关门/装饰，不吞其它插件；`exclusive`：旧独占 `stop_event` |
| `ambient_intervention` | boolean | `false` | 未被点名时是否允许按 WTS 把高价值弱指代交回原 Agent。悬停/冷却/私密仍不插话 |
| `takeover_all` | boolean | `false` | 是否对所有未被排除的群生效。默认关闭 |
| `takeover_groups` | array | `[]` | 接管群聊白名单。白名单为空且 `takeover_all=false` 时不接管任何群 |
| `exclude_groups` | array | `[]` | 排除群聊黑名单（优先级高于白名单 / 全接管） |
| `provider` | string | `""` | 旧版统一 Provider（兼容项）；当专用 Provider 为空时同时作为主回复和氛围校准 Provider |
| `reply_provider` | string | `""` | 主回复 Provider；为空时回退 `provider`，再为空时按当前 UMO 获取 |
| `vibe_provider` | string | `""` | 氛围校准 Provider；为空时回退 `provider`，再为空时按当前 UMO 获取 |
| `bot_names` | array | `[]` | 机器人昵称。请填完整名字，避免使用容易误伤的短词 |
| `command_prefix` | string | `"/"` | 此前缀开头的消息绕过本插件；该选项不会向 AstrBot 注册新的指令前缀 |
| `debounce_base_cooldown` | number | `3.5` | `0..30` 秒 |
| `debounce_extended_cooldown` | number | `6.5` | `0..60` 秒；不得小于基础等待，否则按基础等待归一化 |
| `debounce_max_cap` | number | `12.0` | `0..120` 秒；不得小于前两项，否则按较大值归一化 |
| `strong_addressivity_threshold` | number | `0.70` | `0..1`；强指代判定阈值 |
| `safe_hover_threshold` | number | `0.40` | `0..1` 且必须小于强指代阈值 |
| `deep_cooling_minutes` | number | `15.0` | `0..180` 分钟；`0` 表示自动深度冷却立即到期 |
| `chars_per_second` | number | `25.0` | `1..100` 字/秒 |
| `base_thinking_delay` | number | `0.8` | `0..10` 秒 |
| `max_fragments` | integer | `3` | `1..3`；装饰原管线回复时最多拆成几条 |
| `max_fragment_chars` | integer | `120` | `40..500`；单条消息的软长度目标 |
| `inter_burst_interval` | number | `1.2` | `0.6..3` 秒；碎发基础间隔，会按长度和模式调整 |
| `casual_emoji_enabled` | boolean | `false` | 是否在非正式短回复末尾追加一个场景匹配的认可 Emoji |
| `strip_markdown_in_banter` | boolean | `true` | 是否在快速闲聊模式下清洗 Markdown 格式 |
| `vibe_llm_enabled` | boolean | `false` | 是否允许低频调用 Chat Provider 校准氛围。默认关闭 |
| `shadow_mode` | boolean | `false` | 观察模式：不改宿主回复。legacy 不调用 LLM；persona_model 仍调用决策模型并记录预计动作 |
| `console_show_message_content` | boolean | `false` | 控制台是否显示截断后的消息正文；默认脱敏 |
| `telemetrics_window_seconds` | number | `60.0` | `15..300` 秒；MPM 与标签使用的滑动窗口 |
| `fast_banter_enter_mpm` | number | `12.0` | `4..40`；进入快速碎梗的 MPM |
| `chill_fade_enter_mpm` | number | `3.0` | `0.5..12`；进入能量衰退的 MPM |
| `wts_topic_weight` | number | `0.12` | `0..0.5`；话题相关度权重 |
| `wts_professionalism_weight` | number | `0.08` | `0..0.5`；专业度权重 |
| `wts_question_weight` | number | `0.08` | `0..0.5`；问题价值权重 |
| `wts_participation_weight` | number | `0.06` | `0..0.5`；多人参与度权重 |
| `wts_fatigue_weight` | number | `1.0` | `0..2`；五分钟发言疲劳惩罚倍率 |
| `neural_embedding_enabled` | boolean | `false` | 是否调用 AstrBot Embedding Provider；失败时回退本地哈希向量 |
| `embedding_provider` | string | `""` | Embedding 服务商 ID；留空则使用第一个可用 Embedding Provider |
| `neural_link_threshold` | number | `0.78` | `0.5..0.95`；两侧都有神经网络向量时建立语义边的余弦阈值 |
| `embedding_cache_size` | integer | `512` | `64..4096`；神经网络向量 LRU 缓存条数 |

---

## 🔒 接管、会话与生命周期边界

- **过滤范围**：只有启用且命中 `takeover_all` / `takeover_groups`、同时不在黑名单中的群会进入过滤器。默认 `pipeline_mode=filter`：不 `stop_event()`，图片/表情放行，用 `should_call_llm(True)` 禁止宿主默认 LLM（AstrBot 里 `call_llm=True` 才是关门）。完整 @/引用仍交给原管线，插件不再自己回一条。`exclusive` 才会独占拦截文本。
- **原管线交回**：仲裁放行后走 `tool_loop_agent(event=...)` 或原 LLM hook，保留人格、会话历史和工具。插件不再自己拼 `User_id` 调 `llm_generate` 当主回复。
- **环境插话**：`ambient_intervention` 默认关。打开后，弱指代仍要过 WTS；悬停、冷却、私密话题继续静默。
- **冷却持久化**：深度冷却写入插件 KV，重载后仍生效，避免重启后突然又开口。
- **UMO 隔离**：运行状态以完整 `event.unified_msg_origin` 作为 `session_key`，不是只按群号保存。同一群在不同平台、适配器或会话源下拥有独立 DAG、冷却、去重和生成任务。
- **非文本消息**：纯图片、贴纸、语音、视频、文件、转发等事件只计入媒体遥测；filter 模式抑制原生 LLM，exclusive 模式停止事件，不进入防抖、DAG 或生成。文字加媒体时保留文字进入过滤链。戳一戳按社交轻戳处理，不计入媒体附件。
- **资源边界**：单条分析文本最多 4000 字符，单轮最多 8000 字符和 32 个片段；会话总数上限 1000，空闲会话每 5 分钟清理一次。
- **观察模式**：`shadow_mode=true` 时不会改变 AstrBot 原流程，只在控制台记录预计动作和仲裁分项，适合上线前灰度。
- **内存边界**：每个 DAG 默认最多保留 500 个节点并按 1 小时 TTL 清理；上下文回溯最多 8 层、15 个节点。防抖仍有待处理消息时不会清理会话。
- **卸载语义**：卸载时丢弃尚未 flush 的防抖内容，取消并等待已跟踪的生成与氛围分类任务，然后清空会话状态。

---

## 🖥️ 独立插件页面

AstrBot 管理面板会加载 `pages/console/` 作为独立页面「群聊动态控制台」：

- 总览：插件是否启用、接管策略、活跃会话、冷却中数量、防抖积压；
- 会话列表：每个 UMO 会话的群号、氛围模式与 MPM，可搜索群 ID 或 `session_key`；
- 会话详情：平均字符数、独立发言人数、Emoji/媒体比例、场景/情绪标签、最近 WTS 分项、60 秒速率带、氛围来源、五阶段状态灯及 DAG 线程/父子近迹；
- 运维：为当前群开启深度冷却，或重置图谱 / 遥测 / 冷却；两项操作都会立即取消尚未发出的在途回复。

后端接口（由 `context.register_web_api` 注册）：

| 方法 | 路径 | 说明 |
|:---|:---|:---|
| GET | `/api/v1/plugins/extensions/astrbot_plugin_chat_dynamics/overview` | 总览 + 会话列表 |
| GET | `/api/v1/plugins/extensions/astrbot_plugin_chat_dynamics/sessions` | 仅会话列表 |
| GET | `/api/v1/plugins/extensions/astrbot_plugin_chat_dynamics/session?session_key=` | 单个 UMO 会话详情与图谱近迹 |
| POST | `/api/v1/plugins/extensions/astrbot_plugin_chat_dynamics/cool` | `{ "session_key", "minutes" }` 开启冷却 |
| POST | `/api/v1/plugins/extensions/astrbot_plugin_chat_dynamics/reset` | `{ "session_key" }` 重置该会话状态 |
| GET | `/api/v1/plugins/extensions/astrbot_plugin_chat_dynamics/presets` | 查看观察、平衡、活跃三种预设 |
| POST | `/api/v1/plugins/extensions/astrbot_plugin_chat_dynamics/preset/apply` | `{ "name", "confirm": true }` 应用预设 |

`session_key` 应使用接口列表返回的完整 UMO。仅当一个群号唯一对应一个活动会话时，后端才可兼容用群号解析。成功响应统一包装为 `{"status":"ok","ok":true,"data":...,"error":null,"message":null}`；参数错误、未知会话和卸载中的状态分别返回 `400`、`404`、`503`。

页面每 5 秒自动刷新总览与当前会话详情。正文默认脱敏；开启 `console_show_message_content` 后才显示 DAG 文本。白名单为空且未开启「对全部群聊生效」时，控制台会提示尚无信号。

六个后台页面共用日间／夜间视觉样式，侧栏可随时切换。主题按当前后台登录账号持久化到插件 KV 存储，跨页、刷新和重新打开面板后会恢复；浏览器允许时也会缓存到本机。保存状态显示在切换按钮下方，失败时可重试；主题偏好不会修改群聊或模型配置。

页面只通过 `window.AstrBotPluginPage` Dashboard Bridge 请求后端，沿用宿主管理面板的身份认证，不在插件内重复实现 JWT 或 CSRF；部署时应保持插件页处于 AstrBot 管理面板的受限 iframe 边界，详见 [AstrBot Plugin Pages](https://github.com/AstrBotDevs/AstrBot/wiki/en-dev-star-guides-plugin-pages)。接口按宿主提供的 `request.username` 限流（GET 每分钟 60 次、POST 每分钟 20 次）；旧 SDK 没有用户名时使用插件级匿名桶，超限返回 `429` 与 `Retry-After`。

---

## 💻 管理员指令

在群聊中发送管理员指令进行状态监控与运维（仅管理员）：

- `/dynamics status`：查看当前群聊的实时状态（MPM、模式、平均字符数、Emoji/媒体比例、场景/情绪标签、冷却状态、活跃 DAG 节点数）；
- `/dynamics cool [分钟]`：手动为当前群聊触发深度冷却（如 `/dynamics cool 20`）；
- `/dynamics reset`：先作废并丢弃该会话尚未 flush 的输入和在途生成，再清空 DAG、遥测、冷却与历史状态。
- `/dynamics_stop`：普通群成员可停止自己当前尚未发送的回复内容；只作用于当前 UMO 和发送者本人。

以 `/` 开头的指令不会进入防抖状态机，也不会调用 `stop_event()`，因此不会误吞 `/dynamics` 或其他插件指令。
`/dynamics cool` 只接受 `1..180` 分钟；非法参数会返回错误，不会静默触发默认冷却。
`/dynamics_stop` 不撤回已经发送的首段或后续消息；外部模型调用和工具调用已经产生的外部副作用也不会回滚。停止后新请求无需 `resume` 即可继续处理；原 `/dynamics` 命令仍仅限管理员使用。本功能不增加配置项。

---

## 🧪 测试与质量保证

本项目配备单元、并发压力和状态机测试。测试依赖列在 `requirements-dev.txt`；
多数计时测试使用虚拟时钟，另包含少量真实 `SystemClock` 并发检查：

```bash
# 在插件目录运行全部单元测试、压力测试与状态机验证
pytest tests -v

# 安装真实 AstrBot SDK 后运行独立契约 smoke（Python 3.12+）
pytest integration -v

# 安装 Chromium 后执行 Dashboard Bridge 浏览器契约
pytest tests/test_console_browser.py -v

# 运行生产代码、测试与脚本的静态检查
python -m ruff check main.py core tests integration scripts

# 检查 v1.2.0 覆盖率门槛（先生成 coverage JSON）
python -m pytest tests -q --cov=astrbot_plugin_chat_dynamics.main --cov=astrbot_plugin_chat_dynamics.core --cov-report=json:artifacts/coverage.json --cov-fail-under=90
python scripts/check_coverage.py --json artifacts/coverage.json
```

- `test_debounce.py`：滑动窗口防抖与虚拟时钟推进验证；
- `test_adversarial_incompleteness.py`：36 组金标语句未完成启发式正则边界断言；
- `test_m1_challenger_stress.py`：高并发 90 槽位与实时 SystemClock 压测；
- `test_graph.py`：DAG 多线程分支拓扑与三级指代路由测试；
- `test_semantics.py`：哈希 embedding、概念分类器与转述语义边；
- `test_embeddings.py`：AstrBot Embedding Provider 接入、缓存、失败回退与神经网络语义边；
- `test_reactions.py`：场景匹配的认可 Emoji 与跳过规则；
- `test_telemetrics.py`：MPM 滑动窗口与施密特触发器滞后模式切换；
- `test_arbiter.py`：能量不对称切断与发言欲望打分；
- `test_pacer.py`：输出碎化切分、客服词清洗与输入延迟计算；
- `test_platform_bridge.py`：AstrBot `At`/`Reply` 解析与 `event.send` 发送路径；
- `test_plugin_lifecycle.py`：接管、指令放行、回路防护与 llm_generate 生命周期；
- `test_scenarios.py`：白名单/黑名单、引用入图、冷场自动冷却与 LLM 失败静默；
- `test_dashboard.py`：插件页面快照、Web API 路由与 `pages/console` 静态文件。
- `test_config.py`：配置默认值、关系归一化、Schema 上下限与整数约束。
- `test_concurrency_guards.py`：跨 UMO 发送、reset/flush 交错、容量驱逐、持久化单写者和 shadow 副作用边界。
- `test_member_stop.py`：成员级停止命令的 UMO/发送者隔离、legacy/persona 在途生成，以及 native 首段与尾段发送边界。
- `integration/test_sdk_smoke.py`：在未注入测试桩的独立进程中核对真实 AstrBot 导入、插件初始化、Web API、消息链与卸载契约。
- `integration/test_sdk_member_stop.py`：真实 AstrBot SDK 命令注册权限与 native event.send guard 契约。

普通单元测试在本机没有安装 AstrBot 时会使用 `tests/conftest.py` 中的显式 SDK doubles；它们不能单独证明真实 SDK 兼容性。CI 应同时运行 AstrBot `4.16.0` 和受支持的当前 4.x 固定版本，并通过定时任务检查 `AstrBot>=4.16,<5`。

真实 SDK smoke 只证明导入、签名、MessageChain、Reply、Web response、Hook 和插件生命周期契约；实际平台适配器发送与 Dashboard 部署仍需在发布前完成一次人工 staging 验证。

`metadata.yaml` 的 `repo` 目前留空。仓库创建并填入真实 URL 前，本插件保持未发布状态；上架前还要在干净目录中重新检查打包清单、SDK 兼容矩阵和控制台认证边界。

发布包只包含运行时代码、`pages/console`、`i18n`（含 `.astrbot-plugin/i18n`）、Schema、metadata、requirements、README、CHANGELOG 和 LICENSE。发布前可先运行 `python scripts/check_release.py --allow-empty-repo` 做本地结构检查。创建仓库并填写真实 URL 后，运行 `python scripts/check_release.py`，再运行 `python scripts/build_release.py` 生成确定性 ZIP、文件清单和 SHA256；仓库 URL 为空时打包命令会直接失败。
