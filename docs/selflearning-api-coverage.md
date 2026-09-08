# SelfLearning 接口覆盖

核对日期：2026-09-07。核对工作区代码与公开上游，不代表已连接用户运行中的实例。

| 能力 | 兼容方法名 | 消费位置 |
| --- | --- | --- |
| 批准记忆 | get_approved_memories、list_approved_memories、fetch_memories、query_memories | 开启情绪记忆后按用户读取批准短标签 |
| 关系提示 | get_relationship_hints、list_relationships、relationship_snapshot | 本轮新增主模型请求前的关系背景注入 |
| 黑话 | get_slang_candidates、list_slang、approved_slang、list_approved_slang | 本地黑话条目写入前核对批准状态 |

上表是 3 类能力的 11 个兼容名称，同一搭档每类优先使用一个可用方法，并非 11 次调用，也不代表公开主线实现了这些方法。

[SelfLearning 主入口](https://github.com/NickCharlie/astrbot_plugin_self_learning/blob/main/main.py) 的已核对正式衔接点：

- `inject_diversity_to_llm_request(event, req)`：原生请求注入，由宿主分发。上游内部可组合社交上下文、记忆、表达多样性、黑话、few-shot 与 shadow 内容，是否启用由上游控制；不将这些内部函数冒充独立公共 API。
- `on_bot_message_sent(event, *_args)`：本轮新增自管送达通知。只对成功片段调用，复制事件并让 `get_result()` 返回实际片段，保持原事件结果不变；原生发送继续由宿主负责。

关系读取保持 UMO、用户过滤及两秒读取预算。单个原生搭档的存在不会阻断另一个仅提供直连接口的搭档。返回值仅保留有界的关系说明字段，不将分数作为发言阈值，也不作为系统指令。

发送后通知不阻塞送达状态提交；限制 32 个在途任务，每个回调两秒，无写入重试，停用或热重载后未开始的旧任务不再通知，卸载取消并等待。上游采集方法可能自行过滤或吞掉数据库错误，因此诊断只能报告回调状态，不能认证数据库写入。

三类直连能力现均作为主模型背景消费，包括完整的有界记忆正文和黑话释义；每类最多四条，每字段 400 字，泛用查询必须带批准状态。11 个兼容方法名逐一通过模型上下文测试。不会为了遍历别名而重复请求同一能力。

默认 filter、persona_model、legacy + exclusive 均分发宿主请求钩子；独占模式使用事件副本，兼容 4.16 与 4.27 的请求字段，原始停止状态不变。送达通知覆盖插件的 `_send_owned` 路径。

上游 `on_message` 在其原生高优先级平台事件处理器收集输入，继续由宿主执行，插件不重复录入。7 个可发现的管理命令入口（状态、启动、停止、强制学习、记住、好感度状态、设置心情）保留原生命令路由与权限检查，不在每次聊天中自动执行。诊断列出 `input_hooks` 与 `native_commands`；这些入口的存在不代表动作已经执行。
