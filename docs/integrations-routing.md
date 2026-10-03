# 集成边界与路由证据

本轮实现 P0 + P1：集中集成发现、原生 Hook 优先、共享消息语义事实与可解释路由记录。保持现有 Topic / Parent / Recipient 权重、阈值和 Active Dialogue TTL。

| 插件 | 职责 |
| --- | --- |
| Chat Dynamics | 实时群聊、当前话题、父消息、收件人、是否参与、工作记忆 |
| Self Learning | 关系、表达风格、黑话、few-shot、人格学习 |
| LivingMemory | 长期记忆、历史事实与长期召回 |

普通 AstrBot 请求由两个搭档自己的 on_llm_request 处理；CD 不调用它们的长期记忆检索或旧 context Python 方法。CD 自有 Agent 只要走宿主请求 Hook，也遵循同一规则。

Jev 决策在正文请求 Hook 之前运行，可单独读取 Self Learning 的 `plugin.db_manager.get_user_affection(group_id, user_id)`。只使用当前群、当前发言者已有记录中的好感度与满分值，校验返回的群和账号；不新增记录、不更新分数、不重复调用正文 Hook。沿用 `selflearning_integration` 开关，并尊重上游 `enable_affection_system` / `include_affection_info`。单次读取最多等待 0.25 秒，按群与用户缓存 30 秒；缺少接口、没有记录、超时或异常时不传好感度，保持原决策。好感度只适度影响参与倾向，接话对象、参与档位、停止要求和长度约束仍优先；输入预算不足时先舍弃好感度参考。接口核对基于 Self Learning `a38bfa1f1f1ecbb9c7c44a9f827e4dcca8983681`，真实模型对好感度的响应程度需线上评估。

只有无法使用宿主请求 Hook 的内部回复请求，才可通过 Self Learning Hub v1 获取 social / jargon / few_shots。默认不配置 Hub，不影响正常聊天。`selflearning_hub_url` 设置 Hub 服务地址，`selflearning_hub_key_env` 指定保存 API Key 的环境变量名（默认 SELFLEARNING_HUB_API_KEY）。不把 Key 写入配置或面板。客户端校验 manifest 的 version=v1 与响应 envelope；关闭集成或变更配置会使在途请求失效。Hub 不请求 v2 长期记忆。

core/integrations/registry.py 是能力发现入口，legacy/ 隔离历史 Python 兼容。面板分别显示 detected、ready、selected；发现兼容方法不代表会在聊天中调用。LivingMemory search 是管理命令，不作为共享检索 API；没有验证共享 embedding 合约时显示不可用。CD 保留现有宿主 embedding provider 选择，本轮未实现 P2 的 SessionSemanticCache 或跨插件 embedding 共享。

MessageFeatures 是不可变输入事实，文本分析缓存有界，身份事实使用当前别名、提及和引用。保留旧模块不同语义视图：短话题片段不等于对 Bot 的追问，问句结尾不等于一般问句。Topic 相似度不直接决定 Recipient。

Evidence Ledger 分为 topic / parent / recipient / participation。数值是启发式证据，未经概率校准。因子记录原始值与实际加权贡献；结构规则只记录命中，不伪造贡献。回放分开显示话题、父消息和收件人；重排或补归属后刷新最终结论。仅导出允许的诊断代码和有限数值，不复制消息正文或任意 metadata。

本轮没有调整算法准确率，也未训练校准模型。后续以匿名真实 topic / parent / recipient 标注衡量误差，再进行动态对话连续性、短期语义缓存和模糊案例 LLM 判断。

接口核对基于 Self Learning 3c947dd7e0b170bb25d7f18f0e63937aa695917a 与 LivingMemory 9e1c2d717c65ad433d8f7a8ff652fc7e7cda268c 的公开源码。HTTP 回归使用本机测试服务，不能替代用户部署的三插件端到端联调。
