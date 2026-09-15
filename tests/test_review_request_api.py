"""Review request receipts are queryable and remain isolated by session."""
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core import web_api
from tests.test_review_request_integration import runtime


@pytest.mark.asyncio
async def test_review_post_receipt_is_queryable_and_session_isolated(monkeypatch, offline_web_responses):
    host, review, body = await runtime()
    host._shutting_down = False
    host.annotation_drafts_apply = review.annotation_drafts_apply
    api = web_api.ConsoleWebAPI(host)
    monkeypatch.setattr(api, '_rate_limit', lambda *args: None)
    monkeypatch.setattr(web_api, '_json_body', AsyncMock(return_value=body))
    posted = await api.annotation_drafts_post()
    assert posted['data']['saved_ids'] == ['m']
    params = {'session_key': 's', 'request_id': 'r'}
    monkeypatch.setattr(web_api, '_query_param', lambda key: params.get(key, ''))
    queried = await api.annotation_drafts_get()
    assert queried['data'] == {'state': 'complete', 'request_id': 'r', 'result': posted['data']}
    params['session_key'] = 'other'
    assert (await api.annotation_drafts_get())['data'] == {'state': 'pending', 'request_id': 'r'}
    params['session_key'] = ''
    assert (await api.annotation_drafts_get())['status_code'] == 400
    params.update(session_key='s', request_id='x' * 129)
    assert (await api.annotation_drafts_get())['status_code'] == 400


@pytest.mark.asyncio
async def test_review_receipt_failure_is_not_reported_as_pending(monkeypatch, offline_web_responses):
    host, _, _ = await runtime()
    host._shutting_down = False
    api = web_api.ConsoleWebAPI(host)
    monkeypatch.setattr(api, '_rate_limit', lambda *args: None)
    monkeypatch.setattr(web_api, '_query_param', lambda key: {'session_key': 's', 'request_id': 'r'}.get(key, ''))
    monkeypatch.setattr(host.topic_annotations, 'read_request', AsyncMock(side_effect=OSError('offline')))
    response = await api.annotation_drafts_get()
    assert response['status_code'] == 503
    assert response['ok'] is False
