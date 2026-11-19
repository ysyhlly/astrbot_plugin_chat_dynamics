"""Reconcile optional visual expression with the owned Agent's tool boundary."""


_CONCISE_STATIC_MEDIA_SCOPE = (
    "\n[视觉工具权限范围]\n"
    "本轮已通过回复参与判断；允许在当前回复会话内按视觉策略实际调用工具（仅限本轮实际提供且权限允许的生图工具），"
    "不扩大其他外部工具权限，不更换接收人，也不越过停止/隐私/会话边界。\n"
)

_FULL_FALLBACK_MEDIA_POLICY = (
    "\n[已开启的视觉表达权限]\n"
    "本轮已经通过回复参与判断。统一视觉表达策略允许你根据当前聊天内容，主动选择文字、现成表情或创作新图；"
    "生图无需等用户输入命令，也不要求额外出现‘请生图’字样，但画面必须直接服务于本轮交流，不能机械地每轮配图。"
    "此许可仅限本轮实际提供且权限允许的生图工具，以及将其结果交付给当前回复会话；不扩大其他外部工具的权限，"
    "也不允许更换接收人、越过停止/隐私/会话边界或自主开辟新的对话。"
    "需要图片时实际调用工具；工具被禁用或失败时不能假装已生成或已发送。"
    "视觉发送计划是交付给调用方的内部格式，最终由调用方解析，不属于向用户展示的分析；"
    "有候选时遵循视觉策略的计划格式，无候选时直接回复普通文字。\n"
)


def media_reply_instructions(event, *, internal=False) -> str:
    """Permit only the enabled visual policy in an already admitted chat reply."""
    getter = getattr(event, "get_extra", None)
    if internal or not callable(getter) or getter("visual_media_policy_v1", False) is not True:
        return ""
    if getter("_visual_media_static_policy_v1", False) is True:
        return _CONCISE_STATIC_MEDIA_SCOPE
    return _FULL_FALLBACK_MEDIA_POLICY
