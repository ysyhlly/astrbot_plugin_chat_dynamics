"""Use confirmed same-scope farewells without inventing a sleep transition."""
import re


def rest_announcements(events, now):
    sources = []
    for event in events:
        try:
            age = now - float(event.get("occurred_at", 0))
        except (TypeError, ValueError):
            continue
        if not 0 <= age <= 7200 or event.get("data", {}).get("message_origin") == "system_fallback":
            continue
        if re.search(r"(?:吾辈|大作家|我).{0,14}(?:准备.{0,6}(?:睡|躺|休息)|要睡|先睡|睡了|铺床|钻.{0,4}被窝|缩.{0,4}被窝|闭眼)", str(event.get("text", ""))):
            sources.append(event["event_id"])
    return sources


def rest_reply_context(snapshot, observed):
    if snapshot.get("stale") or not observed.get("rest_announcement_ids"):
        return None
    rest = snapshot.get("availability") == "asleep" or re.search(
        r"睡|就寝|铺床|被窝", str(snapshot.get("activity", "")))
    if not rest:
        return None
    return {
        "fact_kind": "observation",
        "announced_in_this_conversation": True,
        "guidance": "本会话近期已成功说明准备休息。按当前生活状态与本轮问题简短接话；无需再次宣布铺床、钻被窝或催所有人睡觉。已有告别不代表已经睡着，也不代表重新醒来；不补写这些过程。若被问及状态可如实回答，具体未答问题优先。",
    }
