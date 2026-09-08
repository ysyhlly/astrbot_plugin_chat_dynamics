# Useful Proactive (v1.3.2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Independent increment「读空气深化 × 有用主动」on box tree `/workspace/astrbot_plugin_chat_dynamics` per DESIGN.md; cold-memory nudge **lively-only**.

**Architecture:** Extend `OccasionKind` with `deciding`; add `core/useful_proactive.py` (gap-fill, quota, newcomer, pace); wire into `DynamicsDecisionGate` after manners/media/occasion; expose why-silent + proactive counters via dashboard/read_air; manners/today UI chips; package separate review.

**Tech stack:** Python 3 plugin, vanilla JS pages, pytest. No visual reskin. No merge into media/page_nav/skin packages.

**Spec:** `/workspace/reviews/chat_dynamics_useful_proactive/DESIGN.md` and `PRD.md`

---

## File map
- Create: `core/useful_proactive.py`, `tests/test_useful_proactive.py`
- Modify: `core/occasion_skin.py`, `core/decision_gate.py`, `core/dashboard.py`, `core/config.py` (if dataclass), `_conf_schema.json`, `main.py` (cfg defaults), `pages/manners/*`, `pages/today/*`, `README.md` / CHANGELOG
- Package: `/workspace/reviews/chat_dynamics_useful_proactive/`

---

### Task 1: Deciding occasion + unit tests
- [ ] Add `OccasionKind.DECIDING`
- [ ] Heuristics (schedule/vote/pick/分工 markers); priority conflict/cool > deciding > help > vent > banter
- [ ] Uncertain → not deciding; exit on settle/cold/topic jump
- [ ] Tests: 约时间 → deciding; banter markers lose to deciding; unclear → neutral

### Task 2: useful_proactive module + tests
- [ ] Gap kinds: hanging_question, appointment_gap, help_followup, cold_memory_nudge (lively-only)
- [ ] Hour + topic quotas (defaults tight: 2/hour, 1/topic); same gap fingerprint once
- [ ] Newcomer caution + pace slow/normal/fast (delay/length hints only)
- [ ] Reason codes zh: 决策中不插科 / 主动配额用尽 / 新人·更收 / 等待缺口闭合
- [ ] Tests covering PRD scripts 1–6 (except UI)

### Task 3: Wire decision_gate + config schema
- [ ] Gate order: manners → cool/conflict → media → occasion → useful_proactive/newcomer/quota → WTS
- [ ] Config keys + defaults from DESIGN §5
- [ ] Degrade without group_memory

### Task 4: Dashboard / today / manners UI
- [ ] read_air: deciding capsule, why_silent codes, proactive_used/cap
- [ ] Manners chips for gap_fill / cold_nudge / newcomer
- [ ] No style language change

### Task 5: README + review package + local sync
- [ ] Short README section; CHANGELOG v1.3.2 note
- [ ] Package tgz + REVIEW.md; notify astrbot插件测试; sync to `E:\项目\astrbot\astrbot_plugin_chat_dynamics`
- [ ] Do **not** deploy vps2 unless authorized after pass
