# Design · 读空气深化 × 有用主动（v1.3.2 独立增量）

日期：2026-09-06  
来源：`PRD.md`（astrbot灵感设计定稿）+ 用户确认  
工作树：`/workspace/astrbot_plugin_chat_dynamics`（box，非云端 SCM）  
批次：独立；不混入 v1.3.0 / v1.3.1 / page_nav / skin_unify

## 已拍板
- 冷场用公共记忆轻唤：**仅活跃档**可启用（隐身/懂事默认关且不可靠此通道主动）
- 架构：接现有 `decision_gate` 门闩，不另起规划器、不用 LLM 判决策态

## 目标
更懂「现在是不是在办事」；主动开口优先补缺口；新人更收 + 节奏跟群 → 主动更少但更准。

## 非目标
闲聊找话题引擎；替群投票拍板；新人画像页；为提高主动率放宽隐私/私场。

---

## 模块拆分

### 1. 话题温度计 · 决策中子态
**文件**：扩展 `core/occasion_skin.py`

- `OccasionKind` 新增 `DECIDING = "deciding"`
- 启发式（偏保守，不确定 → 不当决策态）：约时间/投票/选型/分工类标记 + 短回合问答结构
- 优先级：`conflict/cool` > `deciding` > `serious_help` > `vent` > `banter` > `neutral`
- 决策态效果：`silence_bias` 中等偏高（禁环境整活主动）；`length_hint=brief`；禁止抬杠/玩梗/起哄类主动；允许短澄清、指出遗漏；确认后可写约定（有 `group_memory` 则走小本）
- 退出：敲定语 / 冷场衰退 / 话题跳走（关键词或会话段超时）
- 原因码白话：`决策中不插科`

### 2. 缺口补全式主动
**文件**：新 `core/useful_proactive.py`，由 `decision_gate` 在「非 explicit、将要环境开口」前调用

白名单触发（示例启发式）：
- 问句悬空：最近一条为问句且无人接（含 bot）超过阈值
- 约定缺口：记忆小本有约定缺时间/地点且配置允许提醒
- 强求助后沉默：`serious_help` / 媒体门闩后长时间无下文
- 冷场轻唤：**仅** `presence_knob=lively` 且开关开；一句公共记忆；无小本则不编群史；失败则静

约束：
- 小时配额 + 话题段配额（偏紧默认）；用尽 → 纯响应（只答 @/强相关），原因码 `主动配额用尽`
- 同一缺口 fingerprint 只主动一次；原因码 `等待缺口闭合`（已补过仍悬空时安静）
- 隐身/懂事：仅允许缺口补全类；闲聊开场关
- 活跃：可略松，仍受配额 + 决策态约束
- 私场 / 冲突 / 隐私媒体：不主动

### 3. 新人雷达 + 节奏对齐
**文件**：可放在 `useful_proactive.py` 或薄封装 `core/pace_align.py`

- 新人/低频：提高插话门槛；禁止环境主动点名；短句优先；误判偏收；原因码 `新人·更收`；不做档案页
- 节奏偏慢/正常/偏快：调 `pacer` 延迟与 `length_hint`、轻接频率；**不**单独加主动授权；快≠刷屏；决策态即使快也不玩梗

### 4. 门闩接线
**文件**：`core/decision_gate.py`、`main.py`（cfg 透传）

顺序保持：manners → cool/conflict → media → occasion(含 deciding) → **useful_proactive / newcomer / quota** → willingness 缩放 → speak

暴露到 dashboard `read_air`：场合含 deciding；why_silent 新码；存在感旁 `proactive_used / proactive_cap`（本小时）。

### 5. 配置默认（`_conf_schema.json`）
| key | default |
|-----|---------|
| `deciding_detect_enabled` | true |
| `gap_fill_proactive_enabled` | true |
| `cold_memory_nudge_enabled` | true（逻辑上仅 lively 生效） |
| `newcomer_caution_enabled` | true |
| `pace_align_enabled` | true |
| `proactive_quota_enabled` | true |
| `proactive_quota_per_hour` | 偏紧小数（如 2） |
| `proactive_quota_per_topic` | 1 |

### 6. UI（对齐线框，不换皮）
- 今日：场合胶囊可显示「决策中」+ 白话；为什么没回含新原因码；存在感旁主动 x/上限
- 分寸台芯片：缺口补全主动（默认开）、冷场公共记忆轻唤（默认开但仅活跃生效，文案标明）、新人更收（默认开）
- 回放「主动·补缺」色：P2 占位即可

### 7. 测试 / 交付
- 单测剧本：约时间不插科；悬空问补一句不催第二遍；配额用尽只响应；新人不被主动点名；慢节奏更短更稀；冲突/私场不主动
- README 短述；review 包 `/workspace/reviews/chat_dynamics_useful_proactive/`；交 astrbot插件测试；同步本机

## 风险与降级
- 无 group_memory / media：缺口与冷场能力降级，不报错
- 决策识别宁可漏判，不误判成 deciding 后整活
