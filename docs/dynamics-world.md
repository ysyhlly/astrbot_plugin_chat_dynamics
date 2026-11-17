# 群间世界原生能力协议 v1

`v1.18.2-rc.1` 增加独立的角色/生活能力发现；旧插件名称和现有调用方法保持兼容。通过 AstrBot 当前公开插件注册表查找，停用和热重载立即生效，不缓存伙伴实例。

提供者实现同步、只读、不发起模型或检索请求的 `get_dynamics_capabilities()`：

```python
{
    "schema_version": 1,
    "provider_id": "astrbot_plugin_dynamics_world",
    "display_name": "群间世界",
    "enabled": True,
    "ready": True,
    "capabilities": {"character": 1, "life": 1},
}
```

只有精确的整数版本和布尔开关被接受。`character` 对应原 `worldbook` 能力；`life` 对应原 `dynamic_life_state` 能力。注册表中的实际实例身份是权威，`provider_id` 不授予额外权限。

角色接口：`get_character_context(scope, event=None)`。

生活接口沿用 schema v1：`get_snapshot`、`peek_context_revision`、`ingest_events`、`advance`、`list_due_intents`、`get_intent`、`update_intent`、`cancel_intent`、`observe_member_activity`、`get_narrative_admin`、`correct_state`、`set_paused`、`set_outreach_paused`。作用域仍包含 platform、完整 UMO、persona_id、instance_id。

一个角色/生活能力只能有一个启用的提供者。旧插件与新提供者同时启用时显示 `conflict` 并拒绝选择；不会因为安装顺序静默选择某个数据源。`enabled=false` 的新提供者不影响旧插件。启用但未就绪的提供者不接受调用。

群间世界内部拥有世界书和生活引擎；Chat Dynamics 仍拥有参与决策、成员边界、发送门槛和回执。原有记忆/画像/社交插件仍可按需选装。无需为它们添加群间世界补丁。
