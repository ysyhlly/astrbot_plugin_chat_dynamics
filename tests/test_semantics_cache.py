"""Cache boundaries and caller isolation for semantic matching."""

import pytest

from astrbot_plugin_chat_dynamics.core import semantics


def test_mutable_results_do_not_poison_later_matches():
    text = "Python 报错了，为什么不开心？"
    expected = semantics.semantic_match(text, "代码异常，难受")
    tokens = semantics.lexical_tokens(text)
    scores = semantics.concept_scores(text)
    tokens.clear()
    scores.clear()
    scores["positive"] = 999
    assert semantics.lexical_tokens(text)
    assert semantics.concept_scores(text).get("positive", 0) == 0
    assert semantics.semantic_match(text, "代码异常，难受") == expected


@pytest.mark.parametrize("text", ["", "不开心", "开心但不开心", "没有那么紧张", "Python API 报错？", "哈哈笑死"])
def test_cached_and_uncached_results_agree(text):
    assert semantics.lexical_tokens(text) == set(semantics._lexical_tokens.__wrapped__(text))
    assert semantics.concept_scores(text) == dict(semantics._concept_scores.__wrapped__(text))
    assert semantics.hashed_embedding(text) == semantics._hashed_embedding.__wrapped__(text, 64)


def test_cache_retention_is_bounded_and_long_inputs_bypass():
    caches = (semantics._lexical_tokens, semantics._concept_scores, semantics._hashed_embedding)
    for cache in caches:
        cache.cache_clear()
    for index in range(semantics._CACHE_SIZE + 10):
        semantics.lexical_tokens(str(index))
        semantics.concept_scores(str(index))
        semantics.hashed_embedding(str(index))
    assert all(cache.cache_info().currsize == semantics._CACHE_SIZE for cache in caches)
    before = [cache.cache_info() for cache in caches]
    long_text = "开心 Python " * semantics._CACHE_TEXT_LIMIT
    semantics.lexical_tokens(long_text)
    semantics.concept_scores(long_text)
    semantics.hashed_embedding(long_text)
    assert [cache.cache_info() for cache in caches] == before
    semantics.hashed_embedding("large vector", 256)
    assert semantics._hashed_embedding.cache_info() == before[-1]


def test_embedding_dimension_is_part_of_cache_key():
    assert len(semantics.hashed_embedding("Python API", 32)) == 32
    assert len(semantics.hashed_embedding("Python API", 64)) == 64
