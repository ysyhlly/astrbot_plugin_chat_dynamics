"""Adversarial stress testing for IncompletenessDetector (M1 Challenger 2).

Systematically probes linguistic heuristics across:
1. False positive resistance (normal acknowledgments and conversational completes)
2. Truncation stress (hanging conjunctions, hanging punctuation)
3. Unclosed syntax (fences, quotes, brackets)
4. Mixed code and natural language
5. Edge cases, contractions, and compound inputs
"""

from __future__ import annotations

import pytest
from astrbot_plugin_chat_dynamics.core.incompleteness import IncompletenessDetector


@pytest.fixture
def detector():
    return IncompletenessDetector(threshold=0.50)


# ===========================================================================
# 1. False Positive Stress: Normal Acknowledgments & Casual Completes
# ===========================================================================

@pytest.mark.parametrize(
    "text",
    [
        # Core prompt specified acknowledgments (MANDATORY)
        "好的",
        "收到",
        "ok",
        "666",
        "好的~",
        "谢谢你啊",
        # Case variations
        "OK",
        "Ok",
        "oK",
        "OKK",
        "Okk",
        # Punctuated acknowledgments
        "好的！",
        "好的。",
        "收到！",
        "收到。",
        "ok!",
        "ok.",
        "666！",
        "666.",
        # Chinese modal particles on acknowledgments
        "好的呀",
        "收到啦",
        "好滴",
        "好哒",
        "好的呢",
        "好嘞",
        "好啊",
        "行啊",
        "可以啊",
        "收到哈",
        "好的哈",
        # Number repetitions
        "6666",
        "666666",
        "6",
        # English & slang
        "thx",
        "thanks",
        "ty",
        "np",
        "cool",
        "sure",
        "fine",
        "yes",
        "yep",
        "nope",
        "kk",
        "k",
        "nb",
        "牛逼",
        "确实",
        "赞",
        "早",
        "晚安",
        "拜拜",
        "再见",
        "哈哈",
        "哈哈哈",
        # Compound polite acknowledgments
        "好的，收到",
        "好的，谢谢",
        "收到，谢谢老板",
        "多谢多谢",
        "行，没问题",
        "没问题！",
        "谢谢你",
        "非常感谢",
        # Whitespace padded
        "  好的  ",
        "\tok\n",
        " 收到 \t",
    ],
)
def test_false_positive_acknowledgments(detector, text):
    """Normal acknowledgments must NEVER trigger incompleteness."""
    result = detector.evaluate(text)
    assert not result.is_incomplete, (
        f"False positive triggered for '{text}': score={result.score}, rules={result.matched_rules}"
    )


# ===========================================================================
# 2. Truncation Stress: Hanging Conjunctions
# ===========================================================================

@pytest.mark.parametrize(
    "text,expected_rule",
    [
        # Chinese conjunctions alone
        ("因为", "zh_trailing_conjunction"),
        ("但是", "zh_trailing_conjunction"),
        ("如果", "zh_trailing_conjunction"),
        ("而且", "zh_trailing_conjunction"),
        ("不过", "zh_trailing_conjunction"),
        ("然后", "zh_trailing_conjunction"),
        ("还有", "zh_trailing_conjunction"),
        ("以及", "zh_trailing_conjunction"),
        ("或者", "zh_trailing_conjunction"),
        ("也就是", "zh_trailing_conjunction"),
        # English conjunctions alone
        ("and", "en_trailing_conjunction"),
        ("but", "en_trailing_conjunction"),
        ("so", "en_trailing_conjunction"),
        ("because", "en_trailing_conjunction"),
        ("if", "en_trailing_conjunction"),
        ("then", "en_trailing_conjunction"),
        ("although", "en_trailing_conjunction"),
        ("cuz", "en_trailing_conjunction"),
        ("unless", "en_trailing_conjunction"),
        # Sentences ending in conjunctions
        ("我之所以没有去，因为", "zh_trailing_conjunction"),
        ("这个方案可行，但是", "zh_trailing_conjunction"),
        ("明天有空的话，如果", "zh_trailing_conjunction"),
        ("We discussed the issue, and", "en_trailing_conjunction"),
        ("The implementation is solid, but", "en_trailing_conjunction"),
        ("The server crashed, so", "en_trailing_conjunction"),
        # Conjunctions with trailing punctuation
        ("因为...", "zh_trailing_conjunction"),
        ("但是……", "zh_trailing_conjunction"),
        ("如果，", "zh_trailing_conjunction"),
        ("and...", "en_trailing_conjunction"),
        ("but,", "en_trailing_conjunction"),
        ("so...", "en_trailing_conjunction"),
        ("因为~", "zh_trailing_conjunction"),
    ],
)
def test_truncation_hanging_conjunctions(detector, text, expected_rule):
    """Trailing conjunctions MUST trigger incompleteness."""
    result = detector.evaluate(text)
    assert result.is_incomplete, f"Expected incomplete for '{text}', got score={result.score}"
    assert expected_rule in result.matched_rules


@pytest.mark.parametrize(
    "text",
    [
        # Conjunction exemptions must NOT be incomplete
        "然后呢",
        "所以呢？",
        "理所当然",
        "知其所以然",
        "不过如此",
        "I think so.",
        "See you then!",
        "until then",
        "by then",
        "since then",
        "It's okay then",
    ],
)
def test_conjunction_exemptions(detector, text):
    """Exempted conjunction phrases must NOT trigger incompleteness."""
    result = detector.evaluate(text)
    assert not result.is_incomplete, (
        f"False positive on exempt phrase '{text}': score={result.score}, rules={result.matched_rules}"
    )


# ===========================================================================
# 3. Truncation Stress: Hanging Punctuation
# ===========================================================================

@pytest.mark.parametrize(
    "text,expected_rule",
    [
        # Ellipsis
        ("...", "trailing_ellipsis"),
        ("....", "trailing_ellipsis"),
        ("……", "trailing_ellipsis"),
        ("…", "trailing_ellipsis"),
        ("..", "trailing_ellipsis"),
        ("我不确定...", "trailing_ellipsis"),
        ("这个事情嘛……", "trailing_ellipsis"),
        # Dashes
        ("——", "trailing_dash"),
        ("—", "trailing_dash"),
        ("事情其实是这样的——", "trailing_dash"),
        ("Wait for it --", "trailing_dash"),
        # Commas
        ("，", "trailing_comma"),
        (",", "trailing_comma"),
        ("首先，", "trailing_comma"),
        ("关于这个问题，", "trailing_comma"),
        ("apples, bananas,", "trailing_comma"),
        ("苹果、香蕉、", "trailing_comma"),
        # Colons
        ("：", "trailing_colon"),
        (":", "trailing_colon"),
        ("配置如下：", "trailing_colon"),
        ("Step 1:", "trailing_colon"),
        # Semicolons
        ("；", "trailing_semicolon"),
        (";", "trailing_semicolon"),
        ("第一步完成；", "trailing_semicolon"),
    ],
)
def test_truncation_hanging_punctuation(detector, text, expected_rule):
    """Hanging punctuation MUST trigger incompleteness."""
    result = detector.evaluate(text)
    assert result.is_incomplete, f"Expected incomplete for '{text}', got score={result.score}"
    assert expected_rule in result.matched_rules


@pytest.mark.parametrize(
    "text",
    [
        # Emoticons with punctuation must NOT trigger hanging semicolon/colon
        "hello :)",
        "good job ;-)",
        "nice :-)",
        ";)",
    ],
)
def test_emoticons_not_hanging_punctuation(detector, text):
    """Emoticons ending with parentheses must not be flagged as hanging semicolon/colon."""
    result = detector.evaluate(text)
    assert not result.is_incomplete, (
        f"False positive on emoticon '{text}': score={result.score}, rules={result.matched_rules}"
    )


# ===========================================================================
# 4. Truncation Stress: Unclosed Syntax (Fences, Quotes, Brackets)
# ===========================================================================

@pytest.mark.parametrize(
    "text,expected_unclosed",
    [
        # Code fence unclosed
        ("```python\ndef foo():", "code_block"),
        ("```", "code_block"),
        ("Here is the code:\n```json\n{\"k\": 1}", "code_block"),
        # Inline code unclosed
        ("`x = 1", "inline_code"),
        ("Use `variable_name to configure", "inline_code"),
        ("See `config.py` and `database.py", "inline_code"),
        # ASCII quotes unclosed
        ('"hello world', "ascii_double_quote"),
        ('He said: "Wait for me', "ascii_double_quote"),
        ("He said 'Wait for me", "ascii_single_quote"),
        # Chinese quotes unclosed
        ("“这是未闭合的双引号", "chinese_double_quote"),
        ("‘这是未闭合的单引号", "chinese_single_quote"),
        # Brackets unclosed (ASCII)
        ("def foo(x, y", "bracket_("),
        ("data = [1, 2, 3", "bracket_["),
        ("config = {'port': 8080", "bracket_{"),
        # Brackets unclosed (Chinese/Fullwidth)
        ("（请参考附件", "bracket_（"),
        ("【紧急通知", "bracket_【"),
        ("《三体第一部", "bracket_《"),
        ("「精选台词", "bracket_「"),
        ("『古籍善本", "bracket_『"),
    ],
)
def test_unclosed_syntax_detected(detector, text, expected_unclosed):
    """Unclosed syntax constructs MUST be detected and trigger incompleteness."""
    result = detector.evaluate(text)
    assert result.is_incomplete, f"Expected incomplete for '{text}', got score={result.score}"
    assert "unclosed_syntax" in result.matched_rules
    assert expected_unclosed in result.unclosed_items


@pytest.mark.parametrize(
    "text",
    [
        # Balanced code blocks
        "```python\nprint(1)\n```",
        "```\nhello\n```",
        "Here is code:\n```json\n{}\n```\nAll done.",
        # Balanced inline code
        "Use `var_name` here",
        "Run `npm test` and `npm build`",
        # Balanced quotes
        '"Hello world"',
        'He said "good morning" to everyone.',
        "“这是完整的双引号”",
        "‘这是完整的单引号’",
        # English contractions and apostrophes
        "It's fine, don't worry",
        "I'm ready, we'll go together",
        "They aren't coming, wouldn't you say?",
        "That's a user's choice",
        # Balanced brackets
        "foo(x, y)",
        "[1, 2, 3]",
        "{'port': 8080}",
        "（请参考附件）",
        "【紧急通知】服务已恢复正常运行。",
        "《三体》是一部优秀的科幻小说。",
        "「台词」非常精彩。",
        "『古籍善本』整理完毕。",
    ],
)
def test_balanced_syntax_not_incomplete(detector, text):
    """Properly closed syntax constructs must NOT trigger incompleteness."""
    result = detector.evaluate(text)
    assert not result.is_incomplete, (
        f"False positive on balanced syntax '{text}': score={result.score}, unclosed={result.unclosed_items}"
    )


# ===========================================================================
# 5. Mixed Code and Natural Language
# ===========================================================================

@pytest.mark.parametrize(
    "text,should_be_incomplete,reason",
    [
        # Incomplete mixed code + text
        (
            "我们可以这样写：\n```python\nimport sys\n# missing closing fence",
            True,
            "Unclosed code fence in explanation",
        ),
        (
            "在 `main.py` 里面修改 `foo 函数",
            True,
            "Unclosed inline backtick in Chinese sentence",
        ),
        (
            "JSON格式如下：\n{\n  \"name\": \"test\",\n  \"value\": 100\n",
            True,
            "Unclosed curly brace in JSON snippet",
        ),
        (
            "代码运行失败，报错是: IndexError: list index out of range，因为",
            True,
            "Code error message ending with hanging Chinese conjunction",
        ),
        # Complete mixed code + text
        (
            "我们可以这样写：\n```python\nimport sys\n```\n你觉得怎么样？",
            False,
            "Closed code block with terminal question",
        ),
        (
            "在 `main.py` 里面修改 `foo()` 函数就可以了。",
            False,
            "Multiple closed inline code snippets in complete Chinese sentence",
        ),
        (
            "运行 `pytest tests/` 即可通过所有用例。",
            False,
            "Single inline code in complete Chinese sentence",
        ),
        (
            "执行命令：git commit -m 'fix bug' 然后 push 到远端仓库。",
            False,
            "Non-trailing conjunction in middle of sentence",
        ),
    ],
)
def test_mixed_code_and_natural_language(detector, text, should_be_incomplete, reason):
    """Tests realistic developer group chat messages combining code and natural language."""
    result = detector.evaluate(text)
    assert result.is_incomplete == should_be_incomplete, (
        f"Failed on [{reason}] '{text}': expected incomplete={should_be_incomplete}, "
        f"got incomplete={result.is_incomplete}, score={result.score}, rules={result.matched_rules}"
    )


# ===========================================================================
# 6. Suspension Turn-Holders and Numbered Lists
# ===========================================================================

@pytest.mark.parametrize(
    "text",
    [
        "等一下",
        "稍等",
        "等等",
        "稍等片刻",
        "等我一下",
        "等下",
        "等会儿",
        "慢着",
        "别急",
        "马上来",
        "wait",
        "hold on",
        "one sec",
        "sec",
        "gimme a sec",
        "brb",
        "moment",
    ],
)
def test_suspension_turn_holders(detector, text):
    """Explicit turn-holding suspension phrases MUST trigger incompleteness."""
    result = detector.evaluate(text)
    assert result.is_incomplete, f"Expected incomplete for suspension phrase '{text}'"
    assert "suspension_turn_holder" in result.matched_rules


# ===========================================================================
# 7. Empirical Failure Mode Probes: Multi-Tilde & Casual Sentences
# ===========================================================================

@pytest.mark.parametrize(
    "text",
    [
        "好的~~",
        "收到~~",
        "谢谢~~",
        "拜拜~~",
        "晚安~~",
    ],
)
def test_multi_tilde_polite_signoffs_are_complete(detector, text):
    """Polite ~ / ~~ tails on autonomous replies are finished turns, not hanging speech."""
    result = detector.evaluate(text)
    assert result.is_incomplete is False
    assert result.details.get("reason") == "autonomous_complete"


@pytest.mark.parametrize(
    "text",
    [
        "今天周五",
        "今天天气真好",
        "昨天很好玩",
        "请问现在几点",
        "请问有人在吗",
        "注意安全",
    ],
)
def test_complete_zh_heads_are_not_marked_incomplete(detector, text):
    """Complete short statements must not be delayed solely by their first word."""
    result = detector.evaluate(text)
    assert result.is_incomplete is False
    assert "short_fragment_hanging_syntax" not in result.matched_rules


def test_quotes_inside_fenced_code_are_ignored(detector):
    text = "看这段：\n```python\nprint(\"hello\nprint('world\n```\n写完了。"
    result = detector.evaluate(text)
    assert result.is_incomplete is False
    assert "ascii_double_quote" not in result.unclosed_items
    assert "ascii_single_quote" not in result.unclosed_items


def test_unclosed_quote_outside_code_is_still_detected(detector):
    text = "他说 \"先看这段\n```python\nprint(1)\n```"
    result = detector.evaluate(text)
    assert result.is_incomplete is True
    assert "ascii_double_quote" in result.unclosed_items


@pytest.mark.parametrize(
    "text",
    [
        "真好看",
        "很好看",
        "真好听",
        "好用",
        "没用",
        "不知道",
        "算我一个",
        "就这",
    ],
)
def test_re_zh_hanging_tail_overmatching(detector, text):
    """Complete colloquial reactions must not incur the extended waiting window."""
    result = detector.evaluate(text)
    assert result.is_incomplete is False
    assert "short_fragment_hanging_syntax" not in result.matched_rules


@pytest.mark.parametrize(
    "text",
    [
        "Check the students' homework.",
        "It depends on users' choice.",
    ],
)
def test_english_plural_possessive_apostrophe(detector, text):
    """Plural possessive apostrophes are punctuation, not opening quotes."""
    result = detector.evaluate(text)
    assert result.is_incomplete is False
    assert "ascii_single_quote" not in result.unclosed_items


@pytest.mark.parametrize(
    "text",
    [
        "这就是最终结果。",
        "实验结果很好。",
        "He was born in the 90's.",
        "She is 5'10\" tall.",
    ],
)
def test_complete_jieguo_and_numeric_apostrophes_are_complete(detector, text):
    result = detector.evaluate(text)
    assert result.is_incomplete is False
    assert "zh_trailing_conjunction" not in result.matched_rules
    assert "ascii_single_quote" not in result.unclosed_items
    assert "ascii_double_quote" not in result.unclosed_items


# ===========================================================================
# 8. Boundary Conditions & Null / Empty Stress
# ===========================================================================

@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\t\n\r",
        None,
    ],
)
def test_empty_and_null_inputs(detector, text):
    """Empty and null inputs must cleanly return non-incomplete with score 0.0."""
    result = detector.evaluate(text)
    assert not result.is_incomplete
    assert result.score == 0.0
