"""Real tokenizer and GPU contract check for the synthetic v2 rubric fixture."""

import json
from pathlib import Path
import urllib.request

from laya import Agent
from laya.common import render_options
from transformers import AutoTokenizer

payload = json.loads(Path("/tmp/rubric-v2-smoke.json").read_text(encoding="utf-8"))
tok = AutoTokenizer.from_pretrained("/models/base/tokenizer")
heads = {}
for qid, definition in payload["questions"].items():
    question = Agent._to_internal(definition)
    options = [len(tok(" " + text, add_special_tokens=False)["input_ids"]) for text in render_options(question)]
    instruction = len(tok(f"{question['t']} question: {question['ins']}", add_special_tokens=False)["input_ids"])
    total = instruction + sum(length + 1 for length in options)
    assert max(options) <= 48 and total <= 256
    heads[qid] = total


def post(path, body):
    request = urllib.request.Request(
        "http://127.0.0.1:8900" + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


prepared = post("/prepare", payload)
assert prepared["questions"] == payload["questions"]
result = post("/predict", prepared)
assert set(result["answers"]) == set(payload["questions"])
assert result["metadata"]["device"] == "cuda"
assert all(answer["type"] == "score" and 0 <= answer["score"] <= 4 for answer in result["answers"].values())
report = {
    "rubric_version": "zh-rubric-v2",
    "head_tokens": heads,
    "question_count": len(heads),
    "gpu_contract_passed": True,
    "prepared_questions_unchanged": True,
    "metadata": result["metadata"],
}
Path("/tmp/rubric-v2-contract.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
