# v1.16.0 配套兼容包

这个包与 Chat Dynamics 主安装包分开：基础聊天不要求全部安装。补丁把已验证的本地融合接口整理成可核对的源码差异，不代表对应上游正式发行了这些融合版本，也不适用于任意同名版本。

## 基线

精确提交、每个变更文件的前后 SHA-256 和补丁校验值均在 `manifest.json`。下表的版本号只帮助定位，以文件哈希判定是否可应用。

| ID | 基线 | 融合目标 | 内容 |
| --- | --- | --- | --- |
| livingmemory | 上游 2.10.0，提交 `1a216d7` | 2.10.0+local.3 | 原生来源与叙事证据、私聊作用域、拒绝摘要清理、未归纳原文保留、并行召回、有界队列领取 |
| self_learning | 上游 4.9.0，提交 `204ee8d` | 4.9.0 融合版 | 原生注入与社交状态接口、现有学习和关系修复；版本号相同不等于字节相同 |
| dynamic_life_state | 上游 v0.3.1，提交 `f98022b` | v0.4.11 融合版 | 生活快照、事件接入、意图、成员状态及角色边界 |
| stealer | 上游 3.1.12，提交 `30f8a80` | 3.1.13+local.2 | 实际送达约定、联合文字/表情计划、普通文本解析及独立事件预取 |
| worldbook | 仓库 v1.4.0，提交见 manifest | v1.5.0 融合版 | 可核对的角色资料接口 |
| portrayal | 已存档的本地 v1.1.8 T2I 修复包 | v1.2.0-rc.2 | 当前成员画像与事件接口 |

画像基线是本地存档包，**不是宣称与上游同版本逐字相同**；其原仓库在本次整理时不可访问。manifest 记录了存档包 SHA-256。没有该基线或哈希不匹配时请人工合并，不自动覆盖。

表情插件上游已经出现 3.2.x；本补丁明确针对 3.1.12 基线，不能用于回退或覆盖 3.2.x。它也不等于上游的 3.1.13 发布标签。

这些是融合源码差异，包含为保留现有行为所需的相邻修复，不只是几个接口声明。`licenses/` 保留各来源许可证；第三方补丁遵循对应许可，不以主仓库许可重新授权。世界书为同一作者维护的配套仓库；新增自有代码及选装模块遵循主仓库许可证。

## 检查和应用

先在离线源码副本操作。准备插件源码、配置和 SQLite/索引的可用备份，单独安排目标插件重载；不要把运行中的数据库复制进源码目录。

```powershell
python check_compat.py --plugin livingmemory --target C:/staging/astrbot_plugin_livingmemory
git -C C:/staging/astrbot_plugin_livingmemory apply --check C:/downloads/compat/patches/livingmemory.patch
git -C C:/staging/astrbot_plugin_livingmemory apply C:/downloads/compat/patches/livingmemory.patch
python check_compat.py --plugin livingmemory --target C:/staging/astrbot_plugin_livingmemory
```

检查器只读，既不应用补丁，也不修改配置、数据库、模型、插件激活状态或运行服务。它输出：

- `base_matches`：所有待变更文件匹配基线，可继续离线 `git apply --check`。
- `already_applied`：目标文件已全部匹配融合版本，不再重复应用。
- `mismatch`：有本地修改、其他版本或只应用了一部分。停止覆盖，按清单人工合并；不要加 `--reject` 或强制忽略差异。

通过文件检查后，仍应验证依赖、数据迁移和相关测试，再将离线结果按目标插件原有部署方式上线。源码检查不会评估未修改的本地文件，不能代替业务测试。源码回滚只恢复被替换的文件，数据库和新消息不要随旧快照自动回退。

## 单独选装的本地模块

`addons/astrbot_plugin_memory_guard/` 归还 glibc 已空闲页，按 RSS 增长触发，定时检查，不删除业务缓存。`addons/astrbot_plugin_autonomous_blocking/` 提供成员策略、过滤及发送租约；策略范围与管理命令见其 README。

它们不由主安装包自动安装。把所需模块文件夹单独打包后，通过 AstrBot 插件安装流程安装。只有在服务已就绪且确实需要这一依赖时，才开启 Chat Dynamics 的 `autonomous_blocking_required`。关闭此开关不会删除伙伴的持久策略。

## 验证范围

六份补丁均在精确基线上离线 `git apply --check`、应用并核对全部目标哈希；Python 变更文件通过语法检查。验证过程中未调用第三方插件的模型、消息发送或数据库迁移。当前融合版本的运行验证与本次主插件的原版 SDK/浏览器测试分别记录，不将离线应用成功当作所有安装环境的真实 QQ 验收。
