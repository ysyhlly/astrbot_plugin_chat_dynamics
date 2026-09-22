"""Synthetic deployment smoke only: never quality evidence or a promotable model."""

import json
from pathlib import Path
import sys
import time
import urllib.request


def request(path, payload=None):
    req = urllib.request.Request(
        "http://laya-server:8900" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


QUESTIONS = {
    "join": {"type": "noul", "instructions": "Does the message ask a question?"},
    "category": {
        "type": "choice",
        "instructions": "Which topic is discussed?",
        "criteria": ["technology", "daily life", "other"],
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is the request?",
        "criteria": ["none", "low", "medium", "high", "critical"],
    },
    "action": {
        "type": "choice",
        "instructions": "Should the participant ignore or reply?",
        "criteria": ["ignore", "reply"],
    },
    "target.0": {"type": "noul", "instructions": "Should the response address the current message?"},
    "target.1": {"type": "noul", "instructions": "Should the response address an unrelated older message?"},
}

if sys.argv[1] == "predict":
    import torch

    print(
        json.dumps(
            {"cuda": torch.cuda.is_available(), "device": torch.cuda.get_device_name(0), "ready": request("/readyz")},
            ensure_ascii=False,
        )
    )
    payload = request("/prepare", {"state": {"text": "請幫我解釋這段 Python 程式為何報錯。"}, "questions": QUESTIONS})
    result = request("/predict", payload)
    assert result["metadata"]["device"] == "cuda"
    assert len(result["answers"]) == len(QUESTIONS)
    print(json.dumps(result, ensure_ascii=False))
elif sys.argv[1] == "seed":
    from services.laya_service.jobs import Jobs
    from services.laya_service.backend import task_name

    texts = [
        "電腦程式出現錯誤，請協助查看堆疊訊息。",
        "今晚吃火鍋，番茄湯底搭配豆腐。",
        "天空星座隨季節改變，冬季可以觀察獵戶座。",
        "資料庫連線失敗，應該如何設定逾時？",
        "週末去公園騎自行車，沿途欣賞花朵。",
        "音樂會演出古典交響曲，弦樂聲部非常出色。",
        "網頁按鈕點擊後沒有回應，請問如何除錯？",
        "早餐準備雞蛋麵包，搭配一杯溫牛奶。",
        "海洋潮汐受到月球引力影響，形成週期性變化。",
        "伺服器顯示磁碟空間不足，如何安全清理？",
        "雨天記得帶傘，回家後把鞋子晾乾。",
        "圖書館收藏許多歷史書籍，可供讀者借閱。",
    ]
    rows = []
    for i, text in enumerate(texts):
        prepared = request("/prepare", {"state": {"text": text}, "questions": QUESTIONS})
        labels = {
            "join": {"noul": int(i % 3 == 0)},
            "category": {"choice": list(QUESTIONS["category"]["criteria"])[i % 3]},
            "urgency": {"score": i % 5},
            "action": {"choice": "reply" if i % 3 == 0 else "ignore"},
            "target.0": {"noul": int(i % 3 == 0)},
            "target.1": {"noul": 0},
        }
        for qid, question in QUESTIONS.items():
            rows.append(
                dict(
                    id=f"synthetic-{i}-{qid}",
                    session_id=f"synthetic-session-{i}",
                    task_id=task_name(qid),
                    task_version="1",
                    state=prepared["state"],
                    candidates=question,
                    teacher_label=labels[qid],
                    teacher_model="SYNTHETIC_NOT_LLM",
                    execution_source="synthetic",
                    created_at=time.time() + i,
                    metadata={
                        "question_id": qid,
                        "request_id": f"synthetic-{i}",
                        "request_question_count": len(QUESTIONS),
                        "synthetic": True,
                    },
                )
            )
    directory = Path("/models/smoke-datasets")
    directory.mkdir(exist_ok=True)
    (directory / "synthetic.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    job = Jobs("/state/jobs").create(
        "synthetic.jsonl", "synthetic-smoke-not-promotable-" + str(int(time.time())), epochs=3, seed=73
    )
    print(json.dumps({"job_id": job["id"], "model_id": job["model_id"], "synthetic_rows": len(rows)}))
elif sys.argv[1] == "status":
    from services.laya_service.jobs import Jobs, core_module

    jobs = Jobs("/state/jobs").list()
    print(json.dumps(jobs, ensure_ascii=False))
    print(json.dumps({"registry": core_module("registry").ModelRegistry("/models/registry").status()}))
elif sys.argv[1] == "guards":
    import os
    import urllib.error
    from services.laya_service.jobs import Jobs, core_module

    job = max(Jobs("/state/jobs").list(), key=lambda row: row["created_at"])
    assert job["state"] == "completed"
    registry = core_module("registry").ModelRegistry("/models/registry")
    manifest = registry.validate(job["model_id"])
    artifact = registry._model(job["model_id"])
    training = json.loads((artifact / "training.json").read_text())
    assert training["validation_samples"] > 0
    assert training["best_epoch"] in range(1, training["epochs_completed"] + 1)
    assert training["best_validation_loss"] == min(item["loss"] for item in training["validation_history"])
    assert training["seed"] == 73 and manifest["metadata"]["seed"] == 73
    assert training["max_repeats_per_epoch"] == 3
    split = json.loads((artifact / "split_report.json").read_text())
    calibration = json.loads((artifact / "calibration.json").read_text())
    assert all(bucket["threshold"] > 1 for bucket in calibration.values())
    request_body = json.dumps({"model_id": job["model_id"]}).encode()
    req = urllib.request.Request(
        "http://laya-server:8900/admin/promote",
        data=request_body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["LAYA_ADMIN_TOKEN"]},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("synthetic model was unexpectedly promoted")
    except urllib.error.HTTPError as error:
        assert error.code == 400
    try:
        request("/admin/status")
        raise AssertionError("unauthenticated administration accepted")
    except urllib.error.HTTPError as error:
        assert error.code == 401
    assert registry.status() == {}
    print(
        json.dumps(
            {
                "artifact_files_verified": len(manifest["files"]),
                "synthetic_promotion_rejected": True,
                "unauthenticated_admin_rejected": True,
                "active_registry": registry.status(),
                "training": training,
                "partition_counts": {
                    key: {k: value[k] for k in ("samples", "groups")} for key, value in split["partitions"].items()
                },
                "calibration_groups": {key: value["effective_groups"] for key, value in calibration.items()},
            }
        )
    )
