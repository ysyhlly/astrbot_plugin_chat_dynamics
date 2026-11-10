from types import SimpleNamespace
import pytest
from astrbot_plugin_chat_dynamics.core.reply_failure import provider_failure_code

@pytest.mark.parametrize('reason',['SAFETY','PROHIBITED_CONTENT','BLOCKLIST','OTHER'])
def test_native_refusal_does_not_become_retryable(reason):
    assert provider_failure_code(SimpleNamespace(block_reason=reason))=='provider_content_blocked'
    assert provider_failure_code(RuntimeError('Gemini prompt blocked: '+reason))=='provider_content_blocked'

def test_empty_output_is_not_assumed_transient():
    assert provider_failure_code('candidates is empty')=='provider_empty_or_blocked'
    assert provider_failure_code(TimeoutError('request timeout'))==''
