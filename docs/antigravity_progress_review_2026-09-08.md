# Antigravity 进度复核与后续优化

核查日期：2026-09-08。依据当前源代码、`.agents/` 交接与最终审计记录、实际测试；当前目录没有 Git 元数据，因此不推断提交归属或未提交差异。

## 已完成进度

- M1：DAG 入库与语义推断解耦，推断边由路由层提升。
- M2：话题评分、父消息重排、受话人优先级和诊断日志已实现。
- M3/M4：会话隔离、成功发送后的机器人话题记录、下游语义/决策和配置接线已存在。
- M5：复核前实际执行 `python -m pytest tests -q --disable-warnings --maxfail=5`，878 项通过。`PROJECT.md` 的 M2–M5 状态滞后，现已同步。

生产神经路径在 `main.py` 中使用缓存匹配、后台预热和限时重路由，具备 epoch/实例/锁检查；独立 `route_async` 不是生产入口，要求 DAG matcher 使用同一个 adapter 的缓存。

## 本轮修复

- 共享 embedding 的重复等待者使用 shield，取消一个等待者不会连带取消共享请求；完成回调释放任务索引。
- embedding 配置切换清空旧缓存、取消旧请求，并以 generation 防止旧向量写回；关闭功能后同步匹配使用 hashed 回退。
- `route_async` 等待后检查 revision、epoch、DAG/状态身份、节点存在与路由快照，避免恢复已失效状态。
- 真实 SDK 冒烟接口清单补入已注册的 `replay`；原来仅跑单元测试无法发现这处过期断言。
- 浏览器展开/收起测试等待异步 toggle 更新，保留文本与 ARIA 验证要求。

## 验证记录

- 修改后的全量回归：880 项通过、2 项浏览器即时断言失败；已定位为 toggle 更新时序，并改用 Playwright 等待断言后定向复验。
- 最终定向复验：`tests/test_console_redesign_verification.py tests/test_embeddings.py tests/test_thread_router.py` 共 57 项全部通过，覆盖上述两个失败及新增回归；没有将这次定向结果冒充最终全量复跑。
- AstrBot 4.16 与 4.27 本地真实 SDK 环境：`integration/test_sdk_smoke.py` 各 2 项通过。
- 发布结构检查：`python scripts/check_release.py --allow-empty-repo` 通过，v1.3.3，117 个文件。
- 修改的路由、embedding 与 SDK 测试文件 Ruff 检查通过。

这些结果证明本地测试与接口契约；未在真实群聊执行端到端发送，也未部署或重新打包。目录内同名嵌套副本没有同步修改。
