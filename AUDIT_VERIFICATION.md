# 审计结论核查报告（AUDIT_REPORT.md 逐条复核）

> **核查对象**：`AUDIT_REPORT.md`（v1.9.0 深度审计报告，25 项条目）
> **核查方式**：逐条回到生产源码 + 宿主 AstrBot 4.x 框架实现 + 可执行回归测试
> **核查环境**：Python 3.12.3 / pytest 9.0.3 / AstrBot 4.x（`astrbot_version: ">=4.16,<5"`）
> **核查结论**：**25 项中 6 项属实并已修复，19 项不成立或无法证实**（含 1 项报告自己已排除）
> **回归状态**：`2146 passed`（基线 2140 + 新增 6 条针对性回归测试），零失败

---

## 1. 一句话结论

原报告把大量「防御性代码没被触发」当成「缺陷」，并且有若干条目的**文件位置、行号、符号名与真实代码对不上**。
真正需要修的是 6 处；其中 3 项 Critical 全部不成立——包括那条被判为「永久死锁」的信号量缺陷和两条「未鉴权」的安全缺陷。

| 结论 | 数量 | 条目 |
|---|---|---|
| ✅ **属实，已修复** | **6** | DEF-03、DEF-05、DEF-11、DEF-10（加固）、AUDIT-MAIN-02、AUDIT-MAIN-08、AUDIT-MAIN-12 |
| ❌ **不成立（有反证）** | **15** | DEF-01、DEF-02、DEF-06、DEF-08、DEF-09、AUDIT-MAIN-01、03、04、05、06、07、09、10、11、14 |
| ⚠️ **无法证实 / 无行为影响** | **3** | DEF-04、AUDIT-MAIN-13、DEF-11 的 slider 子项 |
| ⚪ 报告已自行排除 | 1 | DEF-07 |

> 注：上表按条目拆分统计，DEF-11 的主体（缺失枚举）属实已修，其 slider 子项不成立，故同时出现在两栏。

---

## 2. 核查方法（为什么这次的结论可信）

1. **只认生产代码路径**：所有判断都回到 `main.py` / `core/*.py` 的真实函数体，而不是报告里的代码片段。
2. **核对宿主框架语义**：权限、Web API 鉴权、钩子异常处理、事件基类方法，全部到 `.venv/Lib/site-packages/astrbot/` 里查实现，不靠推测。
3. **反证式核查**：对每个「不成立」的条目给出**为什么触发不了**的具体理由（同步函数无 await 点、框架层已拦截、上游已过滤等）。
4. **修复必须可证伪**：为 6 处修复各写一条回归测试，并**先把修复回退、确认 6 条测试全部失败**（`6 failed`），再恢复修复、确认全部通过。测试不是走过场，去掉修复就会红。

原报告 11 个 PoC 的主要问题：**多数没有调用生产代码**，而是把生产逻辑抄进脚本再断言它出错，因此证明的是「作者写下的那段逻辑会出错」，不是「插件会出错」。例如 `poc_task_leak.py` 从未调用 `PersonaEngine.submit/run`，只是手动 `acquire()` 9 次再断言信号量归零。

---

## 3. 已修复的 6 项（含修复位置与验证）

### 3.1 DEF-03 图结构悬空指针 —— 属实
- **位置**：`core/graph.py` `ConversationDAG.prune()`
- **事实**：淘汰节点时只断开 `parent_ids/child_ids/edge_kinds`，子节点 `metadata["inferred_parent_id"]`、`edge_metadata`、`routing.parent_message_id` 仍指向已不存在的父节点。`core/message_semantics.py:105` 会继续把这个悬空 ID 当作父消息输出。
- **修复**：淘汰父节点时，对仍存活的子节点执行与 `unlink_inferred_reply()` 相同的清理（`core/graph.py:568-591`）。

### 3.2 DEF-05 LLM 返回 Markdown 围栏导致标题解析失败 —— 属实
- **位置**：`core/topic_reranker.py` `TopicReranker.title()`
- **事实**：`json.loads(output)` 直接吃模型输出；模型把 JSON 包进 ```json 围栏时抛异常，被 `except Exception` 静默吞掉，话题标题永远为空。
- **修复**：新增 `_unwrap_fenced_json()`（`core/topic_reranker.py:13-21`），仅容忍**一个完整展示围栏**，绝不从散文里抠 JSON——与既有 `TurnDecision.parse` 的既定规则保持一致（`core/turn_decision.py:70-74`）。

### 3.3 DEF-11 配置枚举缺失 —— 属实（仅枚举部分）
- **位置**：`_conf_schema.json:264-272`
- **事实**：`learning_policy_mode` 只接受 `off/shadow/active`，但缺 `options`，配置页把它渲染成自由文本框（`pages/config/app.js:571-577`），手输错值会静默失效。
- **修复**：补 `options: ["off","shadow","active"]`（与 `decision_mode`、`pipeline_mode` 一致）。
- **子项不成立**：报告的「5 个数值项缺少 slider」无实际影响——当前 AstrBot 4.x 的 dashboard 代码中 **slider 出现 0 次**，本插件配置页也只渲染 `type="number"`；该字段是无人消费的死元数据，不加。

### 3.4 DEF-10 信号量许可泄漏 —— 报告结论不成立，但按「结构性加固」处理
- **报告结论不成立的理由**：报告称「取消 generation_task 时队列中的许可永久泄漏 → 死锁」，但真实取消路径都会先清空队列：
  - `main.py:4005` `_invalidate_pending_generation()` → 先 `runtime.clear_model_queue()` 再 `task.cancel()`（`stop`/`cool`/`reset` 全走这里）；
  - `main.py:3906` `_reset_session_state()` → `runtime.reset_conversation_state()` → `clear_model_queue()`（`core/session_runtime.py:446`）；
  - 唯一未清队列的是 `terminate()`，而那时整个插件实例正在被丢弃，信号量随之消失。
  也就是说：**没有一条真实路径能留下无主许可**。`poc_task_leak.py` 全程未调用生产代码，属于自证。
- **仍然加固**：把「队列里的轮次 == 已持有的许可」从「依赖每个取消点自觉清理」变成**结构上恒真**——worker 因取消而退出、且自己仍是 owner 时，释放并丢弃剩余队列（`core/persona_engine.py:488-503`）。正常收尾路径（队列自然清空、已有新 owner 接管）一律不介入，因此不会误伤新轮次。

### 3.5 AUDIT-MAIN-02 已投递尾段未登记 sent_id —— 属实
- **位置**：`main.py` 原生跟进尾段投递循环
- **事实**：尾段**已经发送成功**之后，才检查批次是否已被作废；一旦作废就 `return`，跳过了 `_remember_sent_id()`。平台回显这条已投递消息时，`main.py:2190` 的自答防护查不到它。
- **修复**：投递成功后立刻登记（`main.py:4648-4653`），再做作废判定——消息已经发出去了，它的身份与判定结果无关。

### 3.6 AUDIT-MAIN-08 `/dynamics cool` 容量上限抛异常 —— 属实
- **位置**：`main.py` `cmd_dynamics` 的 `cool` 分支
- **事实**：`_get_or_create_runtime()` 在容量耗尽且无可淘汰会话时抛 `RuntimeError("session capacity reached")`（`main.py:716-717`），而该调用没有守卫，异常直接冲出指令处理器。（回退验证时实测复现：`main.py:717: RuntimeError`。）
- **修复**：先做容量判定，不可调度时回一条明确提示（`main.py:4812-4817`），与 `on_group_message` 的既有写法一致。

### 3.7 AUDIT-MAIN-12 卸载清理可能被取消跳过 —— 属实
- **位置**：`main.py` `terminate()`
- **事实**：会话/仲裁器/分析器的释放写在两级 `try` 之后；任一 `asyncio.shield` 保存被取消就会 `raise`，后续清理整段跳过。
- **修复**：把释放动作收进最外层 `finally`，并提取为 `_release_session_state()`（`main.py:2045-2083`），保证取消、异常、正常三条路径都归还状态。

---

## 4. 判为「不成立」的条目及反证

| 条目 | 报告定性 | 反证（可复核） |
|---|---|---|
| **DEF-01** 防抖槽位竞态 | High | `prune_idle_slots()` 是**同步函数**（`core/debounce.py:586-616`），函数体内无 `await`。单线程事件循环下不可能被 `ingest()` 打断。PoC 自己手抄了扫描与 pop 两步再伪造交错。 |
| **DEF-02** 同秒时间戳丢消息 | Medium | `SystemClock.time()` = `time.monotonic()`（`core/time_service.py:46`），浮点亚毫秒精度；`delta_t == 0.0` 要求两条消息时间戳**完全相同**。且防抖层已把同窗口碎发合并为一次路由。 |
| **DEF-06** 画像浅拷贝污染 | High | `_profile_cache` 是**按输入签名索引的备忘**（`core/topic_resolution.py:102-152`），命中即同输入同输出；`score_topic` 先对真实 topic 重建完整画像，再 `copy()` 到副本上做历史视图（`:158-160`），公共画像从未被改写。共享列表最多导致缓存条目被挤出、重新计算，不产生错误分数。 |
| **DEF-08** 空序列 `max()` 崩溃 | High | `get_recent_nodes()` 只返回**存在于 `self.nodes`** 的节点（`core/graph.py:521-527`），`RoutingState.prune` 又用该集合过滤 `message_ids`（`core/session_runtime.py:116`），中间无 await；因此第 128 行的生成器在真实 DAG 上恒非空。PoC 用的是「`nodes={}` 却让 `get_recent_nodes` 返回节点」的自相矛盾 mock。 |
| **DEF-09** Py3.10 取消防御反转 | High | 报告给的「修复」`if cancelling is not None and cancelling(): raise` 在 Python 3.10 上（`Task.cancelling()` 为 3.11 新增）会让 `cancelling` 为 `None` → **不重抛**，等于把真正的取消（stop/reset/卸载）吞掉，让已作废的回复继续生成。现网代码 `if not callable(cancelling) or cancelling(): raise` 是**保守失败**，是 3.10 上唯一安全的选择（该版本无法区分「自己被取消」和「内部 await 被取消」）。照报告改会引 bug，故不改。 |
| **AUDIT-MAIN-06** 管理指令 Fail-Open | Critical | 双重事实错误：① `is_admin()` 定义在**基类** `AstrMessageEvent`（`.venv/.../astr_message_event.py:263`），任何适配器都有，`hasattr` 恒为真；② `cmd_dynamics` 带 `@filter.permission_type(PermissionType.ADMIN)`（`main.py:4731`），`PermissionTypeFilter.filter` 对非管理员返回 False（`.../star/filter/permission.py:23-29`），**处理器根本不会被调用**。报告建议的补丁反而新增了「按 `sender.role` 字符串放行」的旁路。 |
| **AUDIT-MAIN-09/10/11** Web API 裸奔 | Critical/Medium/Low | 插件 Web API 由宿主挂载在仪表盘鉴权之后：新路由 `Depends(require_plugin_scope)`（`dashboard/api/plugins.py:379-394`），兼容路由 `Depends(require_dashboard_user)`（`:1506-1512`），未登录直接 401（`dashboard/api/auth.py:107-128`）。匿名请求到不了这些 handler。 |
| **AUDIT-MAIN-01** 容量检查与创建脱钩 | High | 检查（`main.py:2154`）与创建（`:2156`）之间**无 await**，同一事件循环线程内不可能被插入；`_ensure_runtime_capacity` 内部用 `threading.Lock` 且淘汰后必然腾出名额。`RuntimeError` 无法由此产生，「崩溃事件循环」更无从谈起（宿主钩子/定时器均有兜底）。 |
| **AUDIT-MAIN-03** 自学习刷新阻断入站 | Medium | `Consumer.refresh()` 文档明确 **Never raises**，内部 `try/except` 包住宿主 IO（`core/learning_policy.py:492-515`）。刷新还受 `due()` 节流。 |
| **AUDIT-MAIN-04** 未尽早校验 revision | Medium | `on_llm_response` 在改写文本**之前**已做 `_event_epoch_is_current()` 校验（`main.py:4180-4186`）。报告所指行号落在另一个函数上。 |
| **AUDIT-MAIN-05** StyleShaper 在 try 之外 | Medium | 宿主对每个钩子处理器都包了 `try/except BaseException` 并记日志（`pipeline/context_utils.py:95-103`），异常不会外溢；加了本地 try 也只是日志形态不同，行为等价。 |
| **AUDIT-MAIN-07** `/dynamics_stop` 无频控 | Low | 「无条件回执」是被测试钉住的**既有契约**：`tests/test_member_stop.py:404-408` 明确断言空停也回执一行。改它等于改产品行为并弄红既有测试，故保留。 |
| **AUDIT-MAIN-14** 会话清理协程无看门狗 | Low | `_session_sweeper`（`main.py:2003-2020`）每轮 `try/except Exception` 兜底、只让 `CancelledError` 逃逸，单轮失败不会让循环死亡。 |
| **DEF-04** 未成型话题死代码 | Low | 报告给的 `core/thread_router.py:540-547` 位置与符号都对不上：`seed_confirmed` 实际位于 `core/pending_topics.py:37`。其「必为死代码」的论证未建立（重复路由会重新 `defer` 并带空候选），且即便成立也仅是死分支、零行为影响，不动路由语义。 |
| **AUDIT-MAIN-13** 孤儿会话映射 | Low | 触发前提（会话在 `_umo_by_session` 等映射中、却既无 runtime 又无 DAG）在现有代码里没有可达路径：`_drop_session` 会同步清掉全部映射，`self._sessions`/`self.dags` 就是 registry 的两个字典本身（`main.py:329-330`）。改动会波及会话生命周期语义，收益无法证明，故不动。 |
| **DEF-07** 质心维度降级 | — | 报告自己已排除，核查同意：哈希兜底是冷启动防致盲的有意设计。 |

---

## 5. 原报告本身的可靠性问题（供后续审计流程改进）

1. **行号/符号错位**：AUDIT-MAIN-06 报 `4709-4720`，真实鉴权在 `4731-4747`；DEF-04 的 `seed_confirmed` 不在所报文件；AUDIT-MAIN-05/04 的行号落在别的函数上。说明部分结论来自**片段推测而非逐行阅读**。
2. **PoC 自证**：`poc_task_leak.py`、`poc_def01.py`、`poc_def08.py`、`poc_def09.py` 均未驱动生产路径（分别手写信号量、手抄 prune 循环、喂自相矛盾 mock、复制逻辑片段）。判据应是「调用真实入口并观察真实后果」。
3. **未核对宿主框架**：两条 Critical 安全结论（管理指令、Web API）都建立在「适配器可能没有 is_admin / 路由无鉴权」的假设上，而这两点都能在 `.venv` 里直接读实现证伪。
4. **给出的补丁有反效果**：DEF-09 的建议补丁会在 Python 3.10 上吞掉真实取消（见上表）。安全类建议必须带上「在支持版本矩阵上分别会怎样」的推演。

---

## 6. 本次改动的验证方式

```powershell
# 1) 全量回归（基线 2140 → 现在 2146）
.venv\Scripts\python.exe -m pytest tests -q

# 2) 针对性回归：6 条测试各锁定一处修复
.venv\Scripts\python.exe -m pytest tests/test_audit_fixes_verified.py -q

# 3) 可证伪性：把修复回退后再跑，应得 6 failed（已实测）
```

| 修复 | 锁定测试 | 回退后 |
|---|---|---|
| 图结构悬空指针 | `test_prune_clears_inferred_parent_pointers` | FAILED |
| 围栏 JSON 标题 | `test_topic_title_accepts_one_presentation_fence` | FAILED |
| 取消时释放许可 | `test_cancelled_persona_worker_releases_queued_admission` | FAILED |
| 尾段 sent_id | `test_invalidated_native_tail_still_records_delivered_id` | FAILED |
| 卸载清理 | `test_terminate_releases_sessions_when_final_save_is_cancelled` | FAILED |
| cool 容量上限 | `test_dynamics_cool_reports_capacity_instead_of_raising` | FAILED（并复现 `main.py:717 RuntimeError`） |

**未改动**：`AUDIT_REPORT.md` 正文（保留原始审计记录），仅在其顶部加了一行指向本报告的更正说明；所有生产业务语义（路由、仲裁、防抖、冷却阈值）保持不变。
