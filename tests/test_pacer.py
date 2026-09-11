"""Unit tests for Milestone M5: Pacing & Style Shaper.

Validates:
1. Removal of robotic assistant sign-offs and boilerplate clichés.
2. Markdown format degradation into casual text in fast banter mode.
3. Code block preservation in serious inquiry mode.
4. Message fragmentation into 2-3 bursts under FAST_BANTER.
5. Cohesive non-fragmented delivery under SERIOUS_INQUIRY.
6. Typing latency calculations proportional to text length and thinking pause.
"""

from __future__ import annotations

import pytest

from astrbot_plugin_chat_dynamics.core.pacer import (
    PacingShaper,
    is_rhythm_short_act,
    scale_delay,
)
from astrbot_plugin_chat_dynamics.core.style_shaper import StyleShaper
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode


@pytest.fixture
def style_shaper():
    return StyleShaper()


@pytest.fixture
def pacer(style_shaper):
    return PacingShaper(style_shaper=style_shaper)


def test_unpunctuated_long_text_is_split_into_at_most_three_bursts(pacer):
    fragments = pacer.shape_and_fragment(
        "长" * 250,
        mode=GroupChatMode.FAST_BANTER,
        max_fragments=10,
        max_fragment_chars=120,
    )
    assert len(fragments) == 3
    assert "".join(fragments) == "长" * 250
    assert max(map(len, fragments)) <= 120


@pytest.mark.parametrize("separator", ["，", "；", ",", ";"])
@pytest.mark.parametrize("max_fragments", [1, 2, 3])
def test_fragmentation_preserves_clause_punctuation(pacer, separator, max_fragments):
    text = separator.join(["这是一段需要完整保留的文字"] * 6)
    fragments = pacer.shape_and_fragment(
        text, GroupChatMode.CHILL_FADE, max_fragments=max_fragments,
    )
    assert 1 <= len(fragments) <= max_fragments
    assert "".join(fragments) == text


def test_merging_fragments_preserves_english_spaces(pacer):
    text = "First comes preparation, then comes execution; finally check every result carefully"
    assert pacer.shape_and_fragment(
        text, GroupChatMode.CHILL_FADE, max_fragments=1,
    ) == [text]


def test_merging_sentence_chunks_does_not_insert_chinese_spaces(pacer):
    text = "第一步先做好准备。第二步开始执行任务！第三步检查结果。第四步保存所有修改。"
    fragments = pacer.shape_and_fragment(text, GroupChatMode.CHILL_FADE, max_fragments=2)
    assert len(fragments) == 2
    assert "".join(fragments) == text


def test_size_boundary_keeps_colon(pacer):
    text = "长" * 55 + "：" + "续" * 100
    fragments = pacer.shape_and_fragment(
        text, GroupChatMode.CHILL_FADE, max_fragment_chars=60,
    )
    assert len(fragments) == 3
    assert fragments[0].endswith("：")
    assert "".join(fragments) == text


def test_optional_casual_emoji_is_deterministic_and_never_used_for_serious_mode():
    shaper = StyleShaper(casual_emoji_enabled=True)
    pacer = PacingShaper(style_shaper=shaper)
    first = pacer.shape_and_fragment("这个可以", GroupChatMode.FAST_BANTER, trigger_text="哈哈这个梗")
    second = pacer.shape_and_fragment("这个可以", GroupChatMode.FAST_BANTER, trigger_text="哈哈这个梗")
    serious = pacer.shape_and_fragment("这个可以", GroupChatMode.SERIOUS_INQUIRY, trigger_text="哈哈这个梗")
    assert first == second
    assert first != ["这个可以"]
    assert "😂" in first[-1]
    assert serious == ["这个可以"]


def test_rhythm_short_acts_are_wake_goodnight_morning_insomnia():
    assert is_rhythm_short_act("wake_reply")
    assert is_rhythm_short_act("goodnight_reply")
    assert not is_rhythm_short_act("")
    assert not is_rhythm_short_act("ok")


def test_scale_delay_slows_and_clamps():
    assert scale_delay(1.0, 1.35) == 1.35
    assert scale_delay(2.0, 0.85) == 1.7
    assert scale_delay(1.0, 0.1) == 0.5
    assert scale_delay(1.0, 9.0) == 2.5
    assert scale_delay(1.0, "bad") == 1.0
    assert scale_delay(0.0, 1.5) == 0.0


def test_typing_delay_honors_gate_delay_scale(pacer):
    base = pacer.calculate_typing_delay("一段回复", GroupChatMode.CHILL_FADE, delay_scale=1.0)
    slow = pacer.calculate_typing_delay("一段回复", GroupChatMode.CHILL_FADE, delay_scale=1.35)
    assert slow == pytest.approx(base * 1.35)


def test_inter_burst_delay_grows_with_fragment_length(pacer):
    short = pacer.calculate_inter_burst_delay(GroupChatMode.CHILL_FADE, "短")
    long = pacer.calculate_inter_burst_delay(GroupChatMode.CHILL_FADE, "长" * 120)
    assert 0.6 <= short < long <= 2.0


def test_inter_burst_delay_tracks_group_rate(pacer):
    slow = pacer.calculate_inter_burst_delay(GroupChatMode.CHILL_FADE, "短", mpm=2.0)
    fast = pacer.calculate_inter_burst_delay(GroupChatMode.CHILL_FADE, "短", mpm=16.0)
    assert 0.6 <= fast < slow <= 2.0


# ---------------------------------------------------------------------------
# Style Shaper Tests
# ---------------------------------------------------------------------------

def test_style_shaper_strips_robotic_signoffs(style_shaper):
    """Robotic customer-service sign-offs must be eliminated."""
    raw = (
        "你可以尝试重启一下路由器试试。\n"
        "如果您还有其他任何问题，请随时告诉我！希望能对您有所帮助。祝您生活愉快！"
    )
    cleaned = style_shaper.clean_robotic_signoffs(raw)
    assert "随时告诉我" not in cleaned
    assert "有所帮助" not in cleaned
    assert "生活愉快" not in cleaned
    assert cleaned == "你可以尝试重启一下路由器试试。"


def test_style_shaper_markdown_degradation_in_banter(style_shaper):
    """In FAST_BANTER mode, markdown headers, bold, and list bullets are stripped."""
    raw = (
        "### 推荐方案\n"
        "**核心重点**：\n"
        "- 使用 `uv` 代替 pip\n"
        "- 开启缓存加速"
    )
    adapted = style_shaper.adapt_style(raw, mode=GroupChatMode.FAST_BANTER)
    assert "###" not in adapted
    assert "**" not in adapted
    assert "- " not in adapted
    assert "核心重点" in adapted
    assert "开启缓存加速" in adapted


def test_style_shaper_preserves_code_in_serious_mode(style_shaper):
    """In SERIOUS_INQUIRY mode, code blocks and structure are preserved."""
    raw = (
        "这里是实现方案：\n"
        "```python\n"
        "def hello():\n"
        "    return 'world'\n"
        "```\n"
        "希望对您有所帮助。"
    )
    adapted = style_shaper.adapt_style(raw, mode=GroupChatMode.SERIOUS_INQUIRY)
    # Robotic ending stripped
    assert "希望对您有所帮助" not in adapted
    # Code block preserved intact
    assert "```python" in adapted
    assert "def hello():" in adapted


# ---------------------------------------------------------------------------
# Pacer Fragmentation & Latency Tests
# ---------------------------------------------------------------------------

def test_pacer_fragmentation_in_fast_banter(pacer):
    """In FAST_BANTER, long generated text is split into 2-3 sequential fragments."""
    long_text = (
        "哈哈哈哈这个真的绝了！\n"
        "我之前也遇到过完全一模一样的坑。\n"
        "最后发现居然是因为配置文件里少写了一个冒号。"
    )
    fragments = pacer.shape_and_fragment(long_text, mode=GroupChatMode.FAST_BANTER, max_fragments=3)

    assert 2 <= len(fragments) <= 3
    assert "这个真的绝了" in fragments[0]
    assert "一模一样的坑" in fragments[1]


def test_pacer_no_fragmentation_in_serious_mode(pacer):
    """In SERIOUS_INQUIRY, text is not fragmented into fragmented bursts."""
    long_text = (
        "在处理并发请求时，建议使用单实例事件循环搭配内存锁进行同步控制。\n"
        "首先，初始化互斥锁；其次，在每次读取或写入缓存前显式获取锁；最后在finally中释放。"
    )
    fragments = pacer.shape_and_fragment(long_text, mode=GroupChatMode.SERIOUS_INQUIRY)

    assert len(fragments) == 1
    assert "单实例事件循环" in fragments[0]


def test_pacer_typing_delay_bounds_and_proportionality(pacer):
    """Typing delay is strictly positive, grows with length, and respects bounds."""
    short_delay = pacer.calculate_typing_delay("好", mode=GroupChatMode.FAST_BANTER)
    long_delay = pacer.calculate_typing_delay(
        "这是一段相对比较长的回复内容，用来测试打字耗时模拟算法是否会随着字数线性增加。",
        mode=GroupChatMode.FAST_BANTER,
    )

    assert short_delay >= pacer.min_typing_delay
    assert long_delay <= pacer.max_typing_delay
    assert long_delay > short_delay


def test_strip_markdown_does_not_eat_snake_case():
    shaper = StyleShaper()
    assert shaper.strip_markdown("use my_var_name please") == "use my_var_name please"


def test_markdown_code_placeholder_text_is_not_replaced():
    shaper = StyleShaper()
    text = "正文 __CODE_BLOCK_0__\n```python\nprint(1)\n```"
    shaped = shaper.strip_markdown(text, preserve_code_blocks=True)
    assert "CODEBLOCK0" in shaped.replace("_", "")
    assert shaped.count("```") == 2
    assert "print(1)" in shaped
