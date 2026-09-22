"""Exercise the plugin-generated synthetic turn envelope without logging user data."""

import json
import os
import urllib.error
import urllib.request

from laya import Agent
from laya.common import render_options
from transformers import AutoTokenizer

payload = json.load(open("/tmp/turn-smoke.json", encoding="utf-8"))
tok = AutoTokenizer.from_pretrained("/models/base/tokenizer")
heads = {}
for qid, question in payload["questions"].items():
    q = Agent._to_internal(question)
    options = [len(tok(" " + value, add_special_tokens=False)["input_ids"]) for value in render_options(q)]
    instruction = len(tok(f"{q['t']} question: {q['ins']}", add_special_tokens=False)["input_ids"])
    heads[qid] = {
        "instructions": instruction,
        "options_with_markers": sum(length + 1 for length in options),
        "max_option": max(options),
    }
print(json.dumps({"head_token_counts": heads}))


def request(path, body=None, admin=False):
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["Authorization"] = "Bearer " + os.environ["LAYA_ADMIN_TOKEN"]
    req = urllib.request.Request(
        "http://127.0.0.1:8900" + path, data=json.dumps(body).encode() if body is not None else None, headers=headers
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


try:
    prepared = request("/prepare", payload)
    result = request("/predict", prepared)
    assert set(result["answers"]) == set(payload["questions"])
    print(json.dumps({"turn_question_keys": list(result["answers"]), "metadata": result["metadata"]}))
except urllib.error.HTTPError as error:
    print(json.dumps({"synthetic_turn_status": error.code, "detail": json.load(error)}))
    raise

try:
    request("/prepare", {"state": {"text": "超長測試" * 10000}, "questions": payload["questions"]})
    raise AssertionError("over-budget critical text unexpectedly accepted")
except urllib.error.HTTPError as error:
    assert error.code == 400
    print(json.dumps({"synthetic_oversize_rejected": json.load(error)}))
print(json.dumps({"prepare_rejections": request("/admin/status", admin=True).get("prepare_rejections")}))
