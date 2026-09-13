# SelfLearning 兼容接口覆盖

当前正常聊天遵循 [原生 Hook 优先规则](integrations-routing.md)。以下 Python 别名保留在 legacy adapter，用于兼容管理，不参与普通请求的背景注入。

核对日期：2026-09-07。核对工作区代码与公开上游，不代表已连接用户运行中的实例。

| 能力 | 兼容方法名 | 消费位置 |
| --- | --- | --- |
| 批准记忆 | get_approved_memories、list_approved_memories、fetch_memories、query_memories | 开启情绪记忆后按用户读取批准短标签 |
| 关系提示 | get_relationship_hints、list_relationships、relationship_snapshot | 旧版兼容读取，普通请求停用 |
| 黑话 | get_slang_candidates、list_slang、approved_slang、list_approved_slang | 本地黑话条目写入前核对批准状态 |

上表是 3 类能力的 11 个兼容名称，同一搭档每类优先使用一个可用方法，并非 11 次调用，也不代表公开主线实现了这些方法。

[SelfLearning 主入口](https://github.com/NickCharlie/astrbot_plugin_self_learning/blob/main/main.py) 的已核对正式衔接点：

- `inject_diversity_to_llm_request(event, req)`：原生请求注入，由宿主分发。上游内部可组合社交上下文、记忆、表达多样性、黑话、few-shot 与 shadow 内容，是否启用由上游控制；不将这些内部函数冒充独立公共 API。
- `on_bot_message_sent(event, *_args)`：本轮新增自管送达通知。只对成功片段调用，复制事件并让 `get_result()` 返回实际片段，保持原事件结果不变；原生发送继续由宿主负责。

显式兼容读取保留 UMO、用户过滤及两秒预算。普通请求不调用关系读取；搭档缺少原生 Hook 时也不会自动退回猜测 Python 方法。

发送后通知不阻塞送达状态提交；限制 32 个在途任务，每个回调使用独立的 60 秒预算，不再复用前台读取的两秒预算，避免正常的数据库或模型处理被过早取消。无写入重试，停用或热重载后未开始的旧任务不再通知，旧回调结果不回写当前诊断状态，卸载取消并等待。真正超时仍报告异常，后续回调成功后恢复。上游采集方法可能自行过滤或吞掉数据库错误，因此诊断只能报告回调状态，不能认证数据库写入。

历史 adapter 的有界正文过滤与 11 个兼容别名测试保留，但 IntegrationRegistry.model_context 返回空结果。只有绕过宿主 Hook 的内部回复可使用 Hub v1，且只读取 social / jargon / few_shots，不读取长期记忆。

### Hub v1 响应契约：作用域回显是必须的

`POST /api/hub/v1/context` 的响应必须回显请求中的 `group_id` 与 `user_id`。

- 插件按「回显必须存在且一致」校验：字段缺失、为空或与请求不一致，一律判为 `scope_mismatch`，状态降级为 `degraded`，本次背景数据整包丢弃并走 fallback，不写入提示词。
- 比较按文本进行，Hub 回显数字 ID（`{"group_id": 123}`）同样匹配。
- 理由：没有回显就无法把一个会话的背景数据与另一个会话区分开。注入内容按「不可信背景」处理，但**作用域隔离不能依赖对端自觉**，因此缺失回显与不匹配同等处理。
- 失败只影响该次请求；下一次请求会重新发现能力并重试，不缓存失败结论。

默认 filter、persona_model、legacy + exclusive 均分发宿主请求钩子；独占模式使用事件副本，兼容 4.16 与 4.27 的请求字段，原始停止状态不变。送达通知覆盖插件的 `_send_owned` 路径。

上游 `on_message` 在其原生高优先级平台事件处理器收集输入，继续由宿主执行，插件不重复录入。7 个可发现的管理命令入口（状态、启动、停止、强制学习、记住、好感度状态、设置心情）保留原生命令路由与权限检查，不在每次聊天中自动执行。诊断列出 `input_hooks` 与 `native_commands`；这些入口的存在不代表动作已经执行。
