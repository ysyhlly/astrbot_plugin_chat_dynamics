# 记忆搭档互联修复记录

本次修复针对 Self Learning、LivingMemory 的发现、调用、状态诊断和临时注入持久化。修改位于当前工作区；没有连接或重载用户实际运行的 AstrBot。

## 已核对的上游

- [Self Learning 主入口](https://github.com/NickCharlie/astrbot_plugin_self_learning/blob/main/main.py)：通过 `inject_diversity_to_llm_request` 和 `_hook_handler` 注入学习内容。
- [LivingMemory 主入口](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/blob/master/main.py)：通过 `handle_memory_recall` 注入记忆，初始化状态由 `initializer.is_initialized` 提供。
- [Group Chat Plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus)：另一套回复调度器；其普通回复支持平台钩子，但不能把它当作记忆 API，也没有修改该仓库。

以上是本轮读取的公开主线接口，并非对用户已安装版本或远程运行状态的认证。

## 已完成的修改

1. 按 AstrBot `StarMetadata.star_cls` 读取实例，忽略未激活插件；兼容常见名称和字典注册表，分别发现两个搭档。
2. 识别原生请求钩子、初始化未完成、直连能力缺失、调用失败和主动关闭。检测到原生钩子不等同于本轮召回成功；面板显示接入方式与逐插件诊断。
3. 直连方法按签名绑定 UMO 和用户参数；支持 await、总等待预算、取消及卸载等待，不再因内部 TypeError 删除用户参数后重复调用同一方法。
4. 保留调用失败状态，避免读取快照就重新亮绿灯；热重载、停用后的迟到结果不再消费。错误诊断不包含上游异常原文。
5. 通用记忆和黑话查询必须明确提供审批状态；过滤与请求会话/用户不一致的返回行，拒绝无会话范围的接口。
6. 没有原生搭档时，已开启的情绪记忆可在请求前异步读取批准短标签，遵守忘记、静音和会话重置。黑话小本写入前异步核对审批。原生搭档存在时避免重复直接召回。
7. 自管 Agent 保存历史时过滤内容片段上的 `_no_save`，防止临时记忆提示被再次保存；正常正文和工具记录保留。
8. 保留现有配置名称和默认值，更新 README、控制台文案及变更日志。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| `python -m pytest tests -q` | 715 passed，包含浏览器测试 |
| 真实 AstrBot 4.16.0：`pytest integration -q` | 12 passed |
| 真实 AstrBot 4.27.5：`pytest integration -q` | 12 passed |
| 本轮修改文件的 Ruff 检查 | 通过 |
| Python 编译、JavaScript 语法检查 | 通过 |
| 修改的中文源码与说明编码扫描 | 无乱码命中 |
| 发布结构检查 `check_release.py --allow-empty-repo` | 通过，版本保持 v1.3.3 |

真实 SDK 测试使用真实元数据、注册表、钩子分发器和 Agent runner；搭档读取及模型提供方使用离线替身。两路提示各到达模型一次、发送前不提交历史、临时提示不写入持久历史。未启动两个完整上游插件或访问用户的记忆数据库。

全仓 Ruff 另有 8 处现存未使用导入，位于未修改的 `daily_rhythm.py`、`media_gate.py`、`occasion_skin.py`、`social_manners.py`、`useful_proactive.py`；本轮没有把全仓 lint 或完整 CI 宣称为通过。

## 部署核对

- 部署当前工作区版本并重载插件。默认 filter 或 persona_model 路径下，搭档初始化完成后应显示“原生钩子”；无需为两个公开主线填写一个不存在的直连 API。
- 若显示初始化中，检查对应搭档的初始化、模型配置和日志；如果状态正常但没有记忆，检查搭档的会话启用范围和召回配置。
- `legacy + exclusive` 简化回复路径没有平台请求钩子保障，面板会明确提示。要使用原生记忆互联，应选择 filter 或 persona_model。
- Group Chat Plus 和 dynamics 同群接管回复的调度竞争没有自动裁决；应由一套插件负责该群回复。
