"""Safe expected-failure signals; never turn refusal into a retry instruction."""
from .llm_adapter import LLMErrorResponse


def provider_failure_code(value):
    reason=getattr(value, 'block_reason', None)
    if isinstance(reason, str) and reason and reason not in {'NONE', 'UNSPECIFIED', 'BLOCK_REASON_UNSPECIFIED'}:
        return 'provider_content_blocked'
    text=str(value).lower()
    if any(marker in text for marker in ('gemini prompt blocked:', 'prohibited_content','content_filter','prompt was blocked','blockedreason.','safety policy')):
        return 'provider_content_blocked'
    if 'candidates is empty' in text:
        # AstrBot 4.28 discards Gemini prompt_feedback when raising this error.
        # Empty output cannot safely be assumed transient, so do not switch models.
        return 'provider_empty_or_blocked'
    if text in {'provider_content_blocked','provider_empty_or_blocked'}:
        return text
    return ''


class ModelReplyFailure(LLMErrorResponse):
    def __init__(self, code='provider_reply_failed'):
        self.reason_code=code
        super().__init__(code)


def unavailable_reply(reason):
    if reason in {'provider_content_blocked','provider_empty_or_blocked'}:
        return '收到你的@了，这条内容我暂时没法回应。'
    return '收到你的@，但这次暂时没能生成完整回复。'
