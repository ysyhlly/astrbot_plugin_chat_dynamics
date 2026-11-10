# OpenAI Decisions 抉择模型

Chat Dynamics 支持独立的 OpenAI Decisions API。它调用 `POST /v1/decisions`，用于判断是否参与、接谁的话、回复方向和上下文是否足够。它不生成 QQ 回复正文。

需要 AstrBot **4.29.4 或更新版本**。Jev / System One 和 OpenAI Decisions 均属于原生 `DecisionProvider`。

1. 在 AstrBot「模型服务 → 抉择模型」新增 **OpenAI Decisions** 服务。
2. 官方服务地址填 `https://api.openai.com/v1`，密钥在该模型服务中配置；也支持沿用 AstrBot 的密钥环境变量写法。
3. 获取模型列表并添加 `gpt-6-luna`。依据 2026-11-04 的官方文档，这是官方 Decisions 当前支持的模型。兼容网关可以填写自己的地址和模型；网关必须实际支持 Decisions 协议，仅支持 Chat Completions 不够。
4. 在 Chat Dynamics「参数配置」页的「决策模型」下拉选择刚添加的模型，再保存。该下拉只读取专用抉择模型列表，回复模型仍选择普通聊天模型。更新会将原有 Jev 服务迁到抉择分类，保留地址、密钥、模型 ID 和已有选择。

默认模型列表路径为 `/v1/models`，默认决策路径为 `/v1/decisions`，兼容服务可以修改。不要把 API 密钥填入提示词或插件的普通文本设置。

请求复用现有会话快照和闭集问题；不发送其他会话、聊天工具或 Agent 调用。predicate 映射为原来的 noul；choice 保留原始选项和模型给出的 choice，不用概率 argmax 代替；score 保留模型给出的连续加权评分。

OpenAI 的 choice 要求至少两个候选。原契约只有一个候选时，将其转换为“这个候选是否适合本问题”的 predicate，使用返回概率作为置信度。不会添加虚构消息选项或将置信度设为 1。

超时和置信度继续使用现有设置。任一问题拒绝、答案缺失/错序、候选不匹配、非法概率时，该轮按现有不可用规则保守处理。没有自动切换为聊天生成接口，也没有隐式重试。模型服务更新、切换或卸载后，旧请求结果不能生效。

模型连接状态返回 `protocol=openai_decisions` 及最后一次请求的已报告用量。`cached_tokens` 和 `cache_write_tokens` 已包含在 `input_tokens` 中，不应再次相加；未报告用量与 0 不等价。是否节省费用应按实际服务账单及同类流量比较，不能直接套用 Jev 的置信度或费用结论。

官方依据：[Decisions 指南](https://developers.openai.com/api/docs/guides/decisions)、[创建决策 API](https://developers.openai.com/api/reference/resources/decisions/methods/create)。账号权限、兼容网关支持情况和真实 QQ 对话效果需要在配置实际服务后验证。
