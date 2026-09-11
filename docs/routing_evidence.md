# 路由证据一致性

`routing.evidence` 记录的是**当前结论成立的理由**，不是推理过程的历史。两者混用会让
回放、人工标注、根因统计和后续权重拟合读到互相矛盾的 flag 与 reason。

## 问题

v1.4.2 及之前，话题回填只追加新证据，不清理旧证据：

```text
消息先判为待定/未成题
  topic_ambiguous = True
  evidence = ["topic_profile", "topic_ambiguous"]

后续被 burst、pending 后续或 LLM 重排确认为已有话题
  topic_ambiguous = False
  evidence = ["topic_profile", "topic_ambiguous", "topic_llm_rerank"]
                                                 ^^^^^^^^^^^^^^^ 已被推翻的理由仍然保留
```

这不直接改变是否回复，但会让诊断、标注快照和未来的权重学习把“曾经历过的中间状态”
当成最终原因。

## 契约

`core/routing_contract.py`：

```python
TOPIC_AMBIGUITY_EVIDENCE = frozenset({"topic_ambiguous", "topic_not_formed"})
UNRESOLVED_TOPIC_STATUS = frozenset({"unformed", "pending"})

commit_topic_evidence(routing, code=None) -> list[str]
topic_evidence_is_consistent(routing) -> bool
```

- 当最终结论是**已解决**（`topic_ambiguous` 为假，且 `topic_status` 不属于
  `unformed`/`pending`）时，丢弃 `topic_ambiguous` 与 `topic_not_formed`。
- 结论仍是待定或未成题时两者都保留。
- 顺序保留、去重；传入的新 code 追加在末尾；再次提交已存在的 code 不改变结果。
- 未解决状态优先于陈旧的布尔值：`topic_status="pending"` 时即使
  `topic_ambiguous` 为假也保留未解决证据。

## 写入点

| 位置 | 触发 |
| --- | --- |
| `ThreadRouter.route` 收尾 | 每条消息存储结论前的统一归一化 |
| burst 回填（合并碎发的先前消息） | `topic_burst_confirmed` |
| `pending_topics.reconcile` | `pending_followup` |
| `ThreadRouter.rerank_pending` | `topic_llm_rerank` |

后三处会就地改写**更早消息**的路由快照，它们不会再次经过 `route`，因此必须各自归一化。
重排路径同时补齐 `addressee_ambiguous`：话题提交不改变收件人结论。

## 不变量

`topic_evidence_is_consistent(routing)` 为真，即“已解决结论不得保留未解决理由”。
`tests/test_routing_evidence.py` 覆盖：

- 三个回填路径的端到端一致性（burst、pending 后续、LLM 重排）；
- 未解决状态保留原有理由；
- 三个固定夹具在真实路由器上逐条回放后，所有节点快照都满足不变量。

该不变量只约束话题结论与话题理由，不重写收件人证据：收件人推断在路由时一次成型，
后续话题回填不再改写入参。
