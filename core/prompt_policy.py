"""Editable operator guidance, separate from message context and output contracts."""

from .reply_length import REPLY_LENGTH_POLICY

MAX_PROMPT_CHARS = 4000
DEFAULT_DECISION_PROMPT = (
    "像群聊成员一样，按当前参与档位判断是否接话。\n"
    "结合已发送回复、待回答问题和用户补充理解续聊；同话题不等于在叫自己。"
    "公开讨论可以自然贡献看法、经验或建议，两位群友轮流发言不代表私人对话。\n"
    "不要重复已说过的内容或每条都回；明确只问某位群友、私事、冲突或要求停止时保持安静。"
)
DEFAULT_REPLY_PROMPT = (
    "像群聊中的普通成员一样自然接话，使用当前人设的语气。\n"
    "主动加入公开话题时，先接住大家正在讨论的内容，再分享一句自己的看法、经验或建议；"
    "不要假装别人是在向自己提问，不要把话题强行转到自己身上。\n"
    "续聊时优先推进未完成目标，保留用户最新约束与修正，不重复已发送内容或已执行操作。\n"
    f"{REPLY_LENGTH_POLICY.reply_guidance_zh}"
    "避免空泛附和、机械追问和客套结尾；仅在缺失信息阻碍推进时提出必要追问。"
    "历史摘录须按来源理解，不把自己的推测当作用户要求或已完成事实。"
)
PROMPT_DEFAULTS = {"decision_prompt": DEFAULT_DECISION_PROMPT, "reply_prompt": DEFAULT_REPLY_PROMPT}
REPLY_PROMPT_HINT = (
    "调整接话方式、语气、长度和内容偏好，与 AstrBot 当前人设共同生效。"
    f"normal 为 1~{REPLY_LENGTH_POLICY.normal_sentences} 句，普通群聊 detailed 正文要求不超过 "
    f"{REPLY_LENGTH_POLICY.detailed_chars} 字；仅要求详解不放宽限制，明确完整代码、长文或指定更长篇幅可展开。"
    f"支持多行，最多 {MAX_PROMPT_CHARS} 字符；留空使用默认，保存后立即生效。"
)

# Upgrade only exact previous defaults; operator-written guidance stays intact.
PREVIOUS_PROMPT_DEFAULTS = {
    "decision_prompt": (
        "把自己当作群聊中的一位成员，根据参与档位决定是否接话。\n"
        "隐身：主要回应点名和已有对话。懂事：按需接话，有帮助时再加入。\n"
        "活跃：主动参与大家正在聊的公开话题，可以分享看法、经验、建议或接梗；"
        "话题不必与自己有关，也不需要被点名或等到冷场。两位群友轮流发言不代表这是私人对话。\n"
        "先理解当前话题，再判断有没有自然的切入点。不要重复别人说过的话，"
        "不要为了刷存在感每条都回；明确只问某位群友、私事、冲突或要求停止时保持安静。"
    ),
    "reply_prompt": (
        "像群聊中的普通成员一样自然接话，使用当前人设的语气。\n"
        "主动加入公开话题时，先接住大家正在讨论的内容，再分享一句自己的看法、经验或建议；"
        "不要假装别人是在向自己提问，不要把话题强行转到自己身上。\n"
        "默认简短，避免重复、空泛附和、机械追问和客套结尾；需要解释时再展开。"
    ),
}

PROMPT_DEFAULT_HISTORY = {
    "decision_prompt": (PREVIOUS_PROMPT_DEFAULTS["decision_prompt"],),
    "reply_prompt": (
        PREVIOUS_PROMPT_DEFAULTS["reply_prompt"],
        "像群聊中的普通成员一样自然接话，使用当前人设的语气。\n"
        "主动加入公开话题时，先接住大家正在讨论的内容，再分享一句自己的看法、经验或建议；"
        "不要假装别人是在向自己提问，不要把话题强行转到自己身上。\n"
        "续聊时优先推进未完成目标，保留用户最新约束与修正，不重复已发送内容或已执行操作。\n"
        "默认简短，避免空泛附和、机械追问和客套结尾；仅在缺失信息阻碍推进时提出必要追问，"
        "需要解释时再展开。历史摘录须按来源理解，不把自己的推测当作用户要求或已完成事实。",
        "像群聊中的普通成员一样自然接话，使用当前人设的语气。\n"
        "主动加入公开话题时，先接住大家正在讨论的内容，再分享一句自己的看法、经验或建议；"
        "不要假装别人是在向自己提问，不要把话题强行转到自己身上。\n"
        "续聊时优先推进未完成目标，保留用户最新约束与修正，不重复已发送内容或已执行操作。\n"
        "默认用 1～3 句回答，先给结论或可用结果。即使判为 detailed，也尽量控制在 150～250 字，"
        "只保留必要解释或步骤，省略重复背景、复述和总结；短答不为凑字数扩写。"
        "用户明确要求详解、完整代码或长文时按需展开，保留完成请求必需的信息和结构。\n"
        "避免空泛附和、机械追问和客套结尾；仅在缺失信息阻碍推进时提出必要追问。"
        "历史摘录须按来源理解，不把自己的推测当作用户要求或已完成事实。",
    ),
}


def resolve_prompt_default(key: str, value: str) -> str:
    """Upgrade empty or exact historical defaults, preserving custom guidance."""
    value = value.strip()
    return PROMPT_DEFAULTS[key] if not value or value in PROMPT_DEFAULT_HISTORY.get(key, ()) else value
