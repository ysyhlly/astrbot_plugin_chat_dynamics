"""Lightweight semantic matching for Chat Dynamics.

No third-party models. Combines:
- hashed character n-gram embeddings (hashing trick)
- a concept classifier over synonym groups (scene / emotion / intent)
- surface lexical overlap

Semantic DAG edges use the combined score. Addressivity score boosts stay
conservative and still rely on surface overlap so hover tests do not flip.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Iterable, List, Sequence, Set, Tuple


EMBED_DIM = 64
_CONCEPT_ORDER = (
    "technical_help",
    "banter",
    "support",
    "private_topic",
    "question",
    "positive",
    "negative",
    "tense",
)

CONCEPT_LEXICONS: Dict[str, Tuple[str, ...]] = {
    "technical_help": (
        "代码", "程序", "报错", "出错", "挂了", "崩了", "异常", "失败", "bug",
        "接口", "api", "函数", "日志", "部署", "配置", "编译", "架构", "算法",
        "python", "java", "sql", "docker", "asyncio", "怎么实现", "为什么失败",
    ),
    "banter": ("哈哈", "笑死", "草", "绷不住", "233", "梗", "绝了", "hhh", "lol"),
    "support": ("难受", "烦死", "累死", "崩溃", "委屈", "吐槽", "抱抱", "安慰"),
    "private_topic": ("私聊", "不方便在群里说", "别告诉别人", "只跟你说", "这是秘密", "保密"),
    "question": ("为什么", "怎么", "如何", "为啥", "吗", "呢", "？", "?"),
    "positive": ("哈哈", "开心", "好耶", "太棒", "喜欢", "绝了", "赞", "谢谢"),
    "negative": ("难受", "烦", "累", "崩溃", "委屈", "生气", "无语"),
    "tense": ("救命", "急", "赶紧", "马上", "严重", "完了", "紧张"),
}

# Canonical synonym groups expand paraphrases into shared features.
SYNONYM_GROUPS: Tuple[Tuple[str, ...], ...] = (
    ("报错", "出错", "挂了", "崩了", "异常", "失败", "bug", "error"),
    ("代码", "程序", "接口", "api", "函数", "方法"),
    ("为什么", "为啥", "怎么", "如何", "怎样"),
    ("哈哈", "hhh", "233", "笑死", "lol"),
    ("方案", "计划", "设计", "思路"),
)

# Bound retained chat text as well as entry count. Longer inputs are still
# evaluated in full, but are not retained process-wide.
_CACHE_SIZE = 512
_CACHE_TEXT_LIMIT = 2048
_ASCII_WORDS_RE = re.compile(r"[a-zA-Z0-9_]{2,}")
_CJK_RUNS_RE = re.compile(r"[\u4e00-\u9fff]+")
_WHITESPACE_RE = re.compile(r"\s+")
_NEGATION_RE = re.compile(r"(?:不|没|没有|并非|不是|别)(?:太|很|那么)?$")
_CONCEPT_PATTERNS = tuple(
    (name, tuple(re.compile(re.escape(term)) for term in lexicon))
    for name, lexicon in CONCEPT_LEXICONS.items()
)


def lexical_tokens(text: str) -> Set[str]:
    """ASCII words plus adjacent Chinese character bigrams for topical overlap."""
    text = text or ""
    compute = _lexical_tokens if len(text) <= _CACHE_TEXT_LIMIT else _lexical_tokens.__wrapped__
    return set(compute(text))


@lru_cache(maxsize=_CACHE_SIZE)
def _lexical_tokens(text: str) -> frozenset[str]:
    ascii_words = set(_ASCII_WORDS_RE.findall(text.lower()))
    cjk_runs = _CJK_RUNS_RE.findall(text)
    bigrams = {run[i : i + 2] for run in cjk_runs for i in range(len(run) - 1)}
    return frozenset(ascii_words | bigrams)


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub("", (text or "").strip().lower())


def _char_ngrams(text: str) -> List[str]:
    compact = _normalize(text)
    grams: List[str] = []
    for size in (2, 3):
        if len(compact) < size:
            continue
        grams.extend(compact[i : i + size] for i in range(len(compact) - size + 1))
    return grams


def _signed_index(token: str, dim: int = EMBED_DIM) -> Tuple[int, float]:
    digest = hashlib.md5(token.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:4], "little") % dim
    sign = 1.0 if digest[4] & 1 == 0 else -1.0
    return idx, sign


def hashed_embedding(text: str, dim: int = EMBED_DIM) -> Tuple[float, ...]:
    """Deterministic hashing-trick embedding over tokens and character n-grams."""
    text = text or ""
    compute = _hashed_embedding if len(text) <= _CACHE_TEXT_LIMIT and 0 < dim <= 128 else _hashed_embedding.__wrapped__
    return compute(text, dim)


@lru_cache(maxsize=_CACHE_SIZE)
def _hashed_embedding(text: str, dim: int) -> Tuple[float, ...]:
    vec = [0.0] * dim
    features: List[str] = list(lexical_tokens(text))
    features.extend(_char_ngrams(text))
    lowered = text.lower()
    for group in SYNONYM_GROUPS:
        if any(term in lowered for term in group):
            features.append("syn:" + group[0])
    if not features:
        return tuple(vec)
    for token in features:
        idx, sign = _signed_index(token, dim)
        weight = 1.0 + min(2.0, max(0, len(token) - 1) * 0.2)
        vec[idx] += sign * weight
    norm = math.sqrt(sum(value * value for value in vec))
    if norm <= 0.0:
        return tuple(vec)
    return tuple(value / norm for value in vec)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return max(0.0, min(1.0, float(sum(a * b for a, b in zip(left, right)))))


def concept_scores(text: str) -> Dict[str, float]:
    text = text or ""
    compute = _concept_scores if len(text) <= _CACHE_TEXT_LIMIT else _concept_scores.__wrapped__
    return dict(compute(text))


@lru_cache(maxsize=_CACHE_SIZE)
def _concept_scores(text: str) -> Tuple[Tuple[str, float], ...]:
    lowered = (text or "").lower()
    scores: Dict[str, float] = {}
    for name, patterns in _CONCEPT_PATTERNS:
        hits = 0
        for pattern in patterns:
            for match in pattern.finditer(lowered):
                prefix = lowered[max(0, match.start() - 4):match.start()]
                if name in ("positive", "negative", "tense") and _NEGATION_RE.search(prefix):
                    if name == "positive":
                        scores["negative"] = max(scores.get("negative", 0), 0.5)
                    continue
                hits += 1
                break
        if hits:
            scores[name] = max(scores.get(name, 0), round(min(1.0, hits / 2.0), 4))
    return tuple(scores.items())


def classify_message(text: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Per-message scene and emotion labels from the concept classifier."""
    scores = concept_scores(text)
    scenes = tuple(
        name
        for name in ("technical_help", "banter", "support", "private_topic")
        if scores.get(name, 0.0) > 0.0
    )
    emotions = tuple(
        name for name in ("positive", "negative", "tense") if scores.get(name, 0.0) > 0.0
    )
    return scenes, emotions


def classify_window(texts: Iterable[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    joined = "\n".join(texts)
    return classify_message(joined)


@dataclass(frozen=True)
class SemanticMatch:
    lexical_ratio: float
    overlap_count: int
    embedding_cosine: float
    concept_affinity: float
    score: float
    shared_scenes: Tuple[str, ...]
    backend: str = "hashed"

    def should_link(self, threshold: float = 0.42) -> bool:
        if self.score >= threshold:
            return True
        if self.overlap_count >= 2 and self.lexical_ratio >= 0.25:
            return True
        if self.shared_scenes and self.concept_affinity >= 0.65 and self.overlap_count >= 1:
            return True
        if self.embedding_cosine >= 0.62 and self.overlap_count >= 1:
            return True
        return False


def _expanded_tokens(text: str) -> Set[str]:
    tokens = set(lexical_tokens(text)) - {"今天", "现在", "这个", "那个", "了吗", "了呢", "什么", "怎么", "如何", "为啥", "为什么"}
    lowered = (text or "").lower()
    for group in SYNONYM_GROUPS:
        if group[0] == "为什么":
            continue
        if any(term in lowered or term in tokens for term in group):
            tokens.add("syn:" + group[0])
    return tokens


def _concept_affinity(left: Dict[str, float], right: Dict[str, float]) -> float:
    left = {k: v for k, v in left.items() if k != "question"}
    right = {k: v for k, v in right.items() if k != "question"}
    keys = set(left) | set(right)
    if not keys:
        return 0.0
    dot = sum(left.get(key, 0.0) * right.get(key, 0.0) for key in keys)
    n1 = math.sqrt(sum(value * value for value in left.values()))
    n2 = math.sqrt(sum(value * value for value in right.values()))
    if n1 <= 0.0 or n2 <= 0.0:
        return 0.0
    return round(max(0.0, min(1.0, dot / (n1 * n2))), 4)


def semantic_match(left_text: str, right_text: str) -> SemanticMatch:
    """Compare two utterances with surface overlap, embedding cosine, and concepts."""
    left_tokens = _expanded_tokens(left_text)
    right_tokens = _expanded_tokens(right_text)
    overlap = left_tokens.intersection(right_tokens)
    overlap_count = len(overlap)
    lexical_ratio = 0.0
    if left_tokens and right_tokens:
        lexical_ratio = overlap_count / min(len(left_tokens), len(right_tokens))
    embedding = cosine(hashed_embedding(left_text), hashed_embedding(right_text))
    left_concepts = concept_scores(left_text)
    right_concepts = concept_scores(right_text)
    affinity = _concept_affinity(left_concepts, right_concepts)
    left_scenes, _ = classify_message(left_text)
    right_scenes, _ = classify_message(right_text)
    shared = tuple(scene for scene in left_scenes if scene in right_scenes)
    score = round(
        0.40 * embedding + 0.30 * min(1.0, lexical_ratio) + 0.30 * affinity,
        4,
    )
    return SemanticMatch(
        lexical_ratio=round(lexical_ratio, 4),
        overlap_count=overlap_count,
        embedding_cosine=round(embedding, 4),
        concept_affinity=affinity,
        score=score,
        shared_scenes=shared,
    )
