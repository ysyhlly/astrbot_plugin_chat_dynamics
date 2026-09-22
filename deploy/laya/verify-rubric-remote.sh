#!/usr/bin/env bash
set -euo pipefail
cd /opt/astrbot/laya-decision
cp /mnt/c/Users/ysyhly/Codex/laya-decision/rubric-v2-smoke.json ./rubric-v2-smoke.json
cp /mnt/c/Users/ysyhly/Codex/laya-decision/rubric_smoke.py ./rubric_smoke.py
docker cp ./rubric-v2-smoke.json chat-dynamics-laya-laya-server-1:/tmp/rubric-v2-smoke.json
docker compose exec -T laya-server python - < rubric_smoke.py
docker cp chat-dynamics-laya-laya-server-1:/tmp/rubric-v2-contract.json ./rubric-v2-contract.json
