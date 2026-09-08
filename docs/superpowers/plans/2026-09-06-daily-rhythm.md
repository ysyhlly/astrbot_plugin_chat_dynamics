# Daily Rhythm (v1.3.3) Implementation Plan

**Goal:** Independent「今日作息」per DESIGN.md / PRD.md on `/workspace/astrbot_plugin_chat_dynamics`.

**Constraints:** Priority deciding > manners/media > rhythm > quota. No CSS reskin. Config pages keep page-local assets (no `../shared`). Do not ship fat page_nav. Do not deploy vps2 in handoff. Package separately from useful_proactive.

**Architecture:** New `core/daily_rhythm.py` state machine; wire into `decision_gate` after manners/media/occasion/deciding, before useful_proactive quotas where relevant; expose status via dashboard/read_air; manners/today chips.

## Tasks
1. State machine + goodnight/wind-down/sleep/wake heuristics + unit tests
2. Config keys + schema defaults from DESIGN
3. Wire decision_gate + main/cfg passthrough
4. Dashboard / today / manners UI (status + chips + reason codes)
5. README/CHANGELOG + review package + sync local; notify 插件测试
