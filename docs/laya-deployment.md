# Laya 决策学习容器部署

在 Windows Docker Desktop 的 WSL2 Linux 容器后端运行，安装支持 WSL2 的 NVIDIA 驱动。模型与训练库独立于 AstrBot Python 环境。部署前先确认 Docker 的 GPU 测试可用；本仓库的 CPU 契约测试不证明目标显卡可用。

## 启动

在仓库根目录执行，先复制 `deploy/laya/.env.example` 为同目录 `.env`，填写已验证的 AstrBot 镜像版本及随机管理员令牌。若已有 AstrBot 数据，应先将 Compose 的 `astrbot-data` 指向现有卷，避免启动空实例。

```powershell
docker compose --env-file deploy/laya/.env -f deploy/laya/compose.yaml build
docker compose --env-file deploy/laya/.env -f deploy/laya/compose.yaml run --rm --no-deps laya-server python -m services.laya_service.training download-base --destination /models/base
docker compose --env-file deploy/laya/.env -f deploy/laya/compose.yaml up -d astrbot laya-server
docker compose --env-file deploy/laya/.env -f deploy/laya/compose.yaml --profile training up -d laya-trainer
```

上游代码固定为 `573e5b62696ba441230cd6be71d593331b5d23af`，Hugging Face `convaiinnovations/laya` 固定为 `1c5edc17a7acd8701df6fc341c0d179f1c62c982` 的 `multilingual` 子目录，包含 tokenizer。重复下载不会覆盖已有 base 目录。依赖、CUDA 镜像及模型首次下载需要联网。

插件服务地址设置为 `http://laya-server:8900`，内部 HTTP 允许主机仅添加 `laya-server`。服务不发布宿主机端口，AstrBot 通过 Compose 网络访问。管理令牌必须与插件管理配置一致。AstrBot 面板仅映射本机 `127.0.0.1:6185`。

## 数据与训练

插件导出的 JSONL 写入 AstrBot 挂载的 `/decision-datasets/`，训练服务中同一卷路径为 `/datasets/`。每行包含 `session_id, task_id, task_version, state, candidates, teacher_label, teacher_model, created_at`。`candidates` 为上游题目定义；标签必须来自教师。人工复核放入 `metadata.human_label`，与教师答案使用相同类型。未收集够不同会话或近重复去重后无法划分三组时拒绝训练。

管理员 API 全部需要 `Authorization: Bearer <令牌>`：

| 方法与路径 | 请求／结果 |
|---|---|
| `POST /admin/jobs` | `{"dataset":"export.jsonl","model_id":"candidate-001","epochs":3,"seed":42}`；seed 为 1–2³¹ 的整数 |
| `GET /admin/jobs` | 作业列表，含 queued/running/completed/failed/cancelled/interrupted 状态 |
| `GET /admin/jobs/{id}` | 训练进度、损失、评估结果及失败原因 |
| `POST /admin/jobs/{id}/cancel` | 持久化取消请求，批次边界生效 |
| `POST /admin/evaluate` | `{"model_id":"candidate-001"}`，读取训练后自动生成的不可变测试报告 |
| `POST /admin/promote` | 同上；仅通过验收的任务进入 10% 接管 |
| `POST /admin/rollback` | 切回上一模型与接管阶段 |
| `POST /admin/rollout` | `{"dataset":"comparisons.jsonl"}`，从真实教师／学生比较中统计当前阶段样本；达到 24 小时和 500 个不同请求后推进至 50%、100% |
| `GET /admin/status` | 作业、模型、实际设备及错误 |

工作进程自动完成分组划分、加权采样监督训练、独立校准、独立测试及校验和注册。choice 为交叉熵；noul 为两项 logit 差的 BCE；score 为相邻等级分布交叉熵与累积分布距离。act_head 不参与训练或接管。使用混合精度、梯度累积、梯度检查点与实际前反向批量探测；若目标主机仍显存不足，作业明确失败，不会假装完成。

训练分区还会按相同会话／近重复规则独立划出较新的 validation 组，用于逐 epoch 验证和保存最低验证损失的 checkpoint。默认连续 2 轮未改善至少 0.0001 则早停；calibration 和 test 不参与模型选择。独立组不足时在加载 GPU 模型前拒绝训练。加权采样保持任务／类别平衡，同时限制每条样本每 epoch 最多出现 3 次。训练产物记录 seed、四分区统计、训练与验证指纹、逐轮验证损失和 best_epoch；seed 同时进入模型 manifest。

校准阈值按独立请求聚合：同请求最小置信度作为整组置信度，任一候选错误则整组计错。只有独立请求至少 20 且错误率 Wilson 95% 上界不超过 5% 才启用阈值；因此即使零错误，20 组也不足以通过。缺少 request_id 的样本不增加接管证据，并单独记录数量。测试报告另评估完整请求的目标集合、错选／漏选、无目标却回复的无效计划；不完整请求单独计数，不伪装成完整集合。

训练和推理共用进程级 GPU 文件锁。排队训练会让服务卸载模型并返回 503，此期间插件回退教师；训练完成后重新加载。只启动一个推理 worker；不要绕开卷或复制独立锁目录。容器中断释放锁，重启训练 worker 将旧 running 作业标记 interrupted，重新提交新的 model_id，不覆盖候选模型。

## 模型与接管

模型存储于 `/models/registry/<model_id>`，包含权重、encoder 配置、tokenizer、训练来源、任务版本、校准、评估及 manifest。注册后不可变。晋升不会修改权重；服务发现版本变化后校验并加载。尚未晋升的基座可用于 shadow，但 approved_tasks 为空，禁止直接接管。

灰度推进还要求每个已启用任务在当前阶段至少 20 条实际由 Laya 执行且有教师标签的比较，错误率不超过 5%；choice 按选项一致、noul 按 0.5 阈值、score 按差值不超过 0.5 判断。首次晋升还没有上一稳定模型，紧急恢复时将插件模式切回 `shadow` 或 `off`；已有上一版本时使用 rollback。

`/healthz` 仅表示进程存在；`/readyz` 表示模型已加载，并报告实际 CUDA/CPU 设备。`/prepare` 用实际 tokenizer 生成教师和学生共享的文本快照；`/predict` 接收该快照与 input_id、expected_model_version，版本变化返回 409。服务输出任务／候选数校准阈值和已验收任务列表。

最终上线前必须在目标主机运行真实训练与容器重启、取消、模型切换、中文长上下文验证，收集完整请求延迟。训练报告通过真实 loopback HTTP 测量 `/prepare` 和 `/predict`，包含同一请求的全部已采样题目；目标环境还需验证 AstrBot 跨容器调用的端到端 p95。关键任务缺人工复核、样本不足或指标不合格不会通过晋升。首次交付代码不附带虚构的合格模型或训练效果。

本地 CPU 契约检查：`python -m pytest tests/test_laya_service.py -q`。安装服务依赖后将 `LAYA_TEST_CHECKPOINT` 设置为下载的 multilingual 目录，可额外验证真实权重的三类输出；该项没有权重时明确跳过。CI 检查 CPU 损失反传、固定上游 scorer 契约、HTTP 与 Compose 配置，不声称验证目标 GPU。

## 2026-09-22 目标机验证

已在 win-desktop 的 WSL2／Docker Desktop、RTX 3060 12GB 上构建固定依赖镜像并加载 multilingual 权重。真实 HTTP 验证 choice、noul、score 三类输出，设备报告为 CUDA；服务重启后仍能加载。

单独使用 36 条明确标记的合成样本验证训练管线，未读取实际聊天数据。完成 1 个 epoch、6 个微批次，微批量为 4，训练峰值 CUDA 分配约 6.10GiB；随后完成校准、HTTP 评估与 9 个模型文件的校验和注册。该样本量不足，所有任务均拒绝晋升；未启用任何模型接管。管理员认证拒绝未授权请求，合成模型晋升请求返回 400。

训练优化后另用 72 条隔离合成样本、seed 73 完成 3 个 epoch／27 个微批次。四分区分别为 train 36 条／6 组、validation 12 条／2 组、calibration 12 条／2 组、test 12 条／2 组。验证损失逐轮为 0.548936、0.512938、0.479095，保存第 3 轮最佳权重。目标候选的 4 行校准数据正确计为 2 个独立请求，未虚增证据；所有阈值禁止接管，完整目标集合评估也识别出无目标却回复的无效计划。10 个产物文件校验通过，仍没有晋升任何模型。

三题示例暖启动推理约 179–233ms，重启后的首个请求约 526ms。上述为部署冒烟证据，不代表真实会话任务的质量或完整延迟验收；正式接管仍须积累规定的教师数据并通过门槛。目标部署位于 `/opt/astrbot/laya-decision`，其验证报告独立保存在该目录，服务不发布宿主机端口。
