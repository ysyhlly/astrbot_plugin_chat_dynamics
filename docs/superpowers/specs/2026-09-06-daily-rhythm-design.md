# Design · 今日作息（v1.3.3 独立增量）

日期：2026-09-06  
PRD：同目录 `PRD.md`  
优先级：决策态 > 社交分寸/媒体门闩 > **作息** > 配额  
基线：交审勿破坏 page_nav_min（config 仅 `./style.css`；禁旧胖 page_nav）

## 一句话
氛围向作息：日间可早安/少量分享；夜晚 **收束中 ≠ 已睡**；被吵醒克制；睡不着默认关。

## 状态机（每 UMO 会话）
| 状态 | 含义 |
|------|------|
| `awake` | 还醒着（白天默认） |
| `winding_down` | 收束中：晚安会话已开，仍陪尾声 |
| `asleep_after_wind` | 已睡·收束后（多数人歇了） |
| `asleep_self` | 已睡·自己睡 |
| `brief_wake` | 短醒·被吵醒（冷却后回 asleep_*） |
| `insomniac` | 失眠中（稀有，默认关） |

**硬规则**：回一句晚安 **不得** 直接 → asleep_*；只能 → `winding_down`。

## 模块
新 `core/daily_rhythm.py`，由 `decision_gate` **在 useful_proactive / 配额之前**（但仍在 manners/media/occasion/deciding 之后）咨询：
1. 检测晚安触发 → 进入 `winding_down`；当晚文字晚安配额（默认 1，最多 2）
2. `winding_down`：首波可回；后续静默（可选表情）；活跃档窗口末可选一句汇总仍占配额
3. 入睡判定（保守）：收束窗 20–40min + 发言变稀/冷场；不确定保持收束；热聊推迟
4. `asleep_*`：环境主动 = 0；普通晚安/表情 ≠ 吵醒
5. 被吵醒白名单：@/点名、引用睡前、强求助/媒体向 bot、命令 → 短回 1 → `brief_wake` 冷却 → 再睡
6. 日间：醒来窗可选早安（隐身/冲突/私场/决策中不主动）；分享 0–2 槽/日，耗主动配额，优先缺口/公共记忆
7. 睡不着：默认关；若开，极低概率 + 周期硬顶，至多一句

## 与现有能力
- **不替换** manners / media / deciding / useful_proactive；作息只额外否决或缩短
- 强制入睡 / 今晚别早安：配置覆盖状态

## 配置默认（摘）
| key | default |
|-----|---------|
| `daily_rhythm_enabled` | true |
| `rhythm_morning_hi_enabled` | true（隐身逻辑关） |
| `rhythm_day_share_slots` | 1（偏少，上限 2） |
| `rhythm_goodnight_text_quota` | 1 |
| `rhythm_sleep_after_winddown` | true |
| `rhythm_allow_self_sleep` | true |
| `rhythm_allow_wake` | true |
| `rhythm_insomnia_enabled` | false |
| `rhythm_force_sleep` | false |
| `rhythm_skip_morning_hi_tonight` | false |

## UI
- 今日：作息摘要 + 状态文案；why_silent 原因码
- 分寸台：总开关、早安/分享、自己睡、被吵醒、睡不着、强制入睡、今晚别早安
- 不换皮；页内资源自洽（无 `../shared`）

## 原因码
收束中·晚安已回过/后续静默；已睡·普通晚安不吵醒；短醒冷却中；热聊中·推迟睡点；多数人已歇·入睡；决策中不早安；…

## 测试 / 交付
pytest 覆盖 PRD 验收剧本 1–6；README 短述「氛围道具非真日历」；独立包 `/workspace/reviews/chat_dynamics_daily_rhythm/`；先本机测通交 astrbot插件测试；**本手默认不上 vps2**

## 不做
真日历骗局；晚安复读；深夜陪聊引擎；催睡；绕过冲突/私场/决策
