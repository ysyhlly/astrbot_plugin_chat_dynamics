"""Conservative, program-owned hints; no extra model call or permission grant."""

import re
import json

_SOCIAL = frozenset(
    (
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "hello",
        "hi",
        "hey",
        "在吗",
        "在不在",
        "早",
        "早安",
        "早上好",
        "中午好",
        "下午好",
        "晚上好",
        "晚安",
        "晚安啦",
        "谢谢",
        "多谢",
        "谢谢你",
        "收到",
        "明白了",
        "好的",
        "好呀",
        "好哒",
        "嗯",
        "嗯嗯",
        "哈哈",
        "哈哈哈",
        "辛苦了",
        "拜拜",
        "再见",
        "回头见",
        "你在干嘛",
        "在干嘛",
        "忙吗",
        "吃了吗",
        "吃饭了吗",
        "还没睡吗",
        "早啊",
        "早呀",
    )
)


def simple_social_text(text):
    if not isinstance(text, str) or len(text) > 32:
        return False
    normalized = re.sub(r"[\s，。！？!?~～…]+$", "", text.strip()).casefold()
    return normalized in _SOCIAL


def tool_free_social_turn(text, history):
    """Do not interpret confirmations of a pending action as tool-free chat."""
    greetings = {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "hello",
        "hi",
        "hey",
        "在吗",
        "在不在",
        "早",
        "早安",
        "早上好",
        "中午好",
        "下午好",
        "晚上好",
        "晚安",
        "晚安啦",
        "早啊",
        "早呀",
        "拜拜",
        "再见",
    }
    if not isinstance(text, str) or re.sub(r"[\s，。！？!?~～…]+$", "", text.strip()).casefold() not in greetings:
        return False
    try:
        rows = json.loads(history or "[]") if isinstance(history, str) else history
    except (ValueError, TypeError):
        return False
    if not isinstance(rows, list):
        return False
    for row in rows[-4:]:
        if not isinstance(row, dict) or row.get("tool_calls") or row.get("role") == "tool":
            return False
        if row.get("role") == "assistant" and re.search(
            r"确认|是否|要不要|可以吗|需要我|帮你|提醒|安排|查询|搜索|发送|执行|记住|记下|工具|\?",
            str(row.get("content", "")),
        ):
            return False
    return True
