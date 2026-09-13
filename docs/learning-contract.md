# Learning Contract：与 Dynamics Learning 的跨插件契约

本插件与 [Dynamics Learning](https://github.com/ysyhlly/astrbot_plugin_dynamics_learning)
之间只有两条通道，方向相反，协议版本也**互相独立**。

```text
ChatDynamics  ──trace_schema_version──▶  Dynamics Learning     （决策轨迹）
ChatDynamics  ◀──policy_contract_version──  Dynamics Learning   （发布的策略）
```

两个数字互不推导：本插件改 reader、改 API、改页面都**不会**动 trace schema；
学习层改自己的 UI 也**不会**动 policy contract。把它们混成一个 `contract_version`
曾经是本仓库的一处命名事故，v1.7.0 拆开并写死在这里。

---

## 一、本插件写什么（trace schema）

决策轨迹由 `core/routing_trace.py` 生成，`core/topic_annotations.py` 在人工标注时
把它冻结进标注记录。当前 **v1.7.0 写 schema 3**。

| 段 | 内容 |
| --- | --- |
| `recipient` | 收件人 id / bot_is_addressee / 置信度 / 阈值 / 是否含糊 |
| `topic` | 话题 id / 置信度 / 阈值 / 领先间隔 / 是否含糊 |
| `participation` | 加性分数、level、证据列表、族贡献、**未裁剪的 contribution_total** |
| `state` | 待定 hover、当前对话者、中间消息数、等答复、上一条是不是问句 |
| `routing`（schema 3 起） | `selected_topic` 与结构化 `topic_candidates`，每条带 `evidence` |
| `outcome`（schema 3 起） | `final_outcome` / `delivered` / `suppression_reason` / `stage` |

`contribution_total` 是学习层阈值回放的**唯一依据**（它是宿主裁剪前的分数），
不要停止写入它。

### `outcome` 为什么是事后写入的

轨迹在决策时冻结，那时最终结果还不存在。结果由 `core/outcome_recorder.py` 在五个
检查点写入节点元数据，并在标注快照重建时重新挂回：

```text
not_attempted     默认值：回复流程从未进入
suppressed        门禁 / 仲裁判定不说话（reason 是它的 reason_code）
generation_failed 进入了流程，生成没产出可用回复
delivery_failed   生成了，平台发送失败
delivered         至少一个分片发出去了（终态）
```

**投递是终态**：一个分片发出去了，这一轮就是 delivered，后续分片失败不会翻案。

### 未知的抑制原因会被读成未分类

学习层的抑制原因是**开放词表**。本插件新增一个 `reason_code` 时，学习层不会把它归进
「门禁压制」，而是记成 `unknown` 并计数 —— 加一个原因应当表现为一个未分类的码，
而不是一次静默的门禁压制。所以新增 reason 时，学习层那边的统计会先变「难看」，
这是设计，不是故障。

---

## 二、学习层写什么（policy contract）

学习层把发布契约写在**它自己的**插件作用域的一个键里：

```text
scope    plugin
scope_id ysyhlly/astrbot_plugin_dynamics_learning
key      learning_published_v1
```

本插件只读这个键，**从不写入**学习层的任何数据。契约形态（`policy_contract_version = 1`）：

```json
{
  "policy_contract_version": 1,
  "generated_at": 1760000000.0,
  "policies": [{
    "policy_id": "policy_v3",
    "state": "promoted",
    "source": { "trace_schema_version": 3, "dataset_fingerprint": "9f2c…",
                "learning_version": "0.9.0" },
    "target": { "chat_dynamics_version": "v1.7.0",
                "baseline_config_hash": "3ab41f0c9d2e7b85",
                "validated_host_versions": ["v1.7.0"] },
    "params": { "strong_addressivity_threshold": 0.67, "...": "全部六个参数" },
    "shadow_observed": false
  }]
}
```

### 本插件消费侧的规则（`core/learning_policy.py`）

| 模式 | 行为 |
| --- | --- |
| `off`（默认） | 不读发布文件，一切照旧 |
| `shadow` | 读取、解析、算出「会改成什么」，**不应用**；版本/基线不匹配时打标 |
| `active` | 应用，且必须通过三项检查 |

三项检查各自挡住一种过期方式：

1. **`policy_contract_version`** —— 不认识就直接拒绝，而不是半读；
2. **`validated_host_versions`** —— **成员判定，不是 SemVer 比较**。`1.7.0 -> 1.7.1`
   可能改掉参与度计算或门禁顺序，而策略里每个阈值都是对着旧分布校准的。列表为空表示
   「无法验证」，按不匹配处理；
3. **`baseline_config_hash`** —— 策略假定的基线配置摘要。摘要覆盖六个参数，
   键排序、四位小数、无空白：

   ```text
   sha256('{"parent_accept_threshold":"0.7200","safe_hover_threshold":"0.4000",...}')[:16]
   ```

   `topic_commit_threshold` 在配置里是 `0.0`（表示「由 topic_join_threshold 推导」），
   所以摘要里用的是**推导后**的值，规则与 `ThreadRouter.configure_topics` 一致。
   两边规则必须一致；学习层的跨仓库测试会盯着这一条。

**白名单**：只应用 `ALLOWED_PARAMS` 里的键。未知键或超范围取值会让整份策略不可用，
而不是部分应用 —— 策略是一个被验证过的**集合**，离线结果描述的是整个集合。

---

## 三、为什么把 `core/learning/` 删掉（v1.7.0）

旧 `core/learning/` 是本插件内部的第一版学习层，与独立的 Dynamics Learning 插件职责重叠：
两边都在把标注转成样本、都在统计错误分布、都在给方向性建议。两份实现意味着两个答案，
而运营看到的是其中一个。

删除后的去向：

| 旧位置 | 新位置 |
| --- | --- |
| `core/learning/candidates.py` | `core/candidate_metrics.py`（仍在用：离线评测与标注控制台） |
| `core/learning/{sample,stats,store,recipient_learner,builder}.py` | 删除；样本、统计与学习在 Dynamics Learning 侧 |
| `scripts/learning_report.py` | 删除；报告由学习层控制台出 |
| `core/learning/` 里没有的东西 | 保留：`core/integrations/selflearning*.py` 是 Self Learning 联动，与学习层无关 |

`core/candidate_metrics.py` 留下来是因为它不是学习层的一部分：它是离线路由评测与标注
控制台读的实时指标，不依赖样本格式、学习器或存储。

### `main.py` 的分阶段拆分

v1.7.0 只做第一阶段：把学习策略的运行期部分抽到
`core/learning_policy_runtime.py`（配置基线计算、策略折叠、刷新节流、状态查询）。
边界刻意收得很窄 —— 解析与兼容性规则留在 `learning_policy.py`，运行期模块不知道消息
怎么被路由 —— 这样后续两阶段（回合管线、控制台面）可以各自移动，不必重新谈判这一层。

## 四、本插件仍然不做什么

- 不写入学习层的任何共享首选项；
- 不因为读不到发布文件而报错或改变行为（默认 `off`，找不到伙伴插件就是什么都不做）；
- 不在策略不兼容时降级应用 —— 拒绝就是拒绝。
