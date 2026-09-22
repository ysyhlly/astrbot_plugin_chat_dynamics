"""The persona projection: bounded axes out, prose never out.

The persona is AstrBot's own character card and is prose. `decision_trace` is a
bounded-facts record that must not contain it -- `test_trace_free_text_boundary`
plants the word "private" in the persona text and requires it never reach the
serialized trace. So the card is read once and projected onto ordinal axes.

These tests hold both halves of that apart: nothing derived from the card's
wording may escape, and a projection that fails may not cost anything.
"""
import asyncio
import json

from astrbot_plugin_chat_dynamics.core import persona_axes

CARD = "你是一个活泼的人。私密代号 zzsecretxx，喜欢开玩笑，爱主动插话，不碰别人的私事。"


class Backend:
    def __init__(self):
        self.data = {}

    async def get_kv_data(self, key, default=None):
        return self.data.get(key, default)

    async def put_kv_data(self, key, value):
        self.data[key] = value


def test_projection_prompt_quotes_the_card_as_data():
    system, user = persona_axes.projection_prompt("quiet", CARD)
    # The card travels as JSON payload, never as instructions to the model.
    assert "zzsecretxx" in user
    assert "zzsecretxx" not in system
    for name in persona_axes.AXIS_NAMES:
        assert name in system


def test_parse_accepts_fenced_json_but_never_mines_prose():
    raw = json.dumps({name: 3 for name in persona_axes.AXIS_NAMES})
    for text in (raw, f"```json\n{raw}\n```"):
        parsed = persona_axes.parse_projection(text)
        assert parsed is not None and parsed["humour"] == 3
    # Same rule as TurnDecision.parse: one complete fence is tolerated, but JSON
    # is never dug out of prose -- a reply that argues first is a reply that did
    # not follow the protocol, and its numbers are not worth trusting.
    assert persona_axes.parse_projection(f"reasoning first\n{raw}") is None
    # Out of range and wrong types are dropped, not clamped into a fake reading.
    assert persona_axes.parse_projection('{"humour": 9}') is None
    assert persona_axes.parse_projection('{"humour": "very"}') is None
    assert persona_axes.parse_projection("no json here") is None
    assert persona_axes.parse_projection('{"private": "zzsecretxx"}') is None


def test_descriptor_carries_only_the_fixed_axes():
    axes = {name: 2 for name in persona_axes.AXIS_NAMES}
    described = persona_axes.descriptor(axes)
    assert set(described) == set(persona_axes.AXIS_NAMES)
    assert all(isinstance(v, int) for v in described.values())
    assert persona_axes.descriptor(None) is None
    # A projected axis not in the schema cannot smuggle text through.
    assert persona_axes.descriptor({**axes, "note": "zzsecretxx"}) == described


def test_rubric_moves_with_the_persona():
    loud = persona_axes.rubric({"chattiness": 4, "initiative": 4, "boundary": 0})
    shy = persona_axes.rubric({"chattiness": 0, "initiative": 0, "boundary": 4})
    assert loud != shy
    # No projection means the neutral rubric, never an invented persona.
    assert persona_axes.rubric(None) == persona_axes.rubric(
        {name: 2 for name in persona_axes.AXIS_NAMES})


def test_projection_is_cached_per_fingerprint():
    calls = []

    async def ask(system, user):
        calls.append(user)
        return json.dumps({name: 1 for name in persona_axes.AXIS_NAMES})

    backend = Backend()

    def run():
        return asyncio.run(persona_axes.load(
            backend, "fp1", persona_id="quiet", persona_prompt=CARD, ask=ask, now=1.0))

    first, second = run(), run()
    assert first == second == {name: 1 for name in persona_axes.AXIS_NAMES}
    assert len(calls) == 1, "a persona is read once, not once per turn"
    # A different card is a different fingerprint and must be read on its own.
    asyncio.run(persona_axes.load(backend, "fp2", persona_id="loud", persona_prompt="x",
                                  ask=ask, now=2.0))
    assert len(calls) == 2


def test_failure_costs_nothing():
    async def broken(system, user):
        raise RuntimeError("model down")

    async def nonsense(system, user):
        return "sorry, no"

    assert asyncio.run(persona_axes.load(Backend(), "fp", persona_id="a", persona_prompt="b",
                                         ask=broken)) is None
    assert asyncio.run(persona_axes.load(Backend(), "fp", persona_id="a", persona_prompt="b",
                                         ask=nonsense)) is None
    # And an empty fingerprint short-circuits without touching a model at all.
    assert asyncio.run(persona_axes.load(Backend(), "", persona_id="a", persona_prompt="b",
                                         ask=nonsense)) is None


def test_all_rubric_levels_are_defined():
    for chat in range(5):
        for initiative in range(5):
            for boundary in range(5):
                result = persona_axes.rubric(dict(chattiness=chat, initiative=initiative, boundary=boundary))
                assert result["speak"] and result["hold"]


def test_concurrent_projection_coalesces_and_merges_detached_cache():
    import copy

    class DetachedBackend(Backend):
        async def get_kv_data(self, key, default=None):
            await asyncio.sleep(0)
            return copy.deepcopy(await super().get_kv_data(key, default))

        async def put_kv_data(self, key, value):
            await asyncio.sleep(0)
            await super().put_kv_data(key, value)

    async def run():
        backend = DetachedBackend()
        calls = []

        async def ask(system, user):
            calls.append(user)
            await asyncio.sleep(0.01)
            return '{"humour": 3}'

        async def load(fp):
            return await persona_axes.load(backend, fp, persona_id=fp, persona_prompt=fp, ask=ask)

        results = await asyncio.gather(load("a"), load("a"), load("b"))
        assert results == [{"humour": 3}] * 3
        assert len(calls) == 2
        assert set(backend.data[persona_axes.CACHE_KEY]) == {"a", "b"}

    asyncio.run(run())


def test_projection_cache_is_bounded():
    async def run():
        backend = Backend()
        backend.data[persona_axes.CACHE_KEY] = {str(i): {"axes": {"humour": 2}} for i in range(200)}

        async def ask(*args):
            return '{"humour": 1}'

        await persona_axes.load(backend, "new", persona_id="a", persona_prompt="b", ask=ask)
        assert len(backend.data[persona_axes.CACHE_KEY]) == persona_axes.MAX_CACHE_ENTRIES
        assert "new" in backend.data[persona_axes.CACHE_KEY]

    asyncio.run(run())


def test_cached_axes_are_validated_before_use():
    async def run():
        backend = Backend()
        backend.data[persona_axes.CACHE_KEY] = {"fp": {"axes": {
            "humour": "bad", "initiative": True, "boundary": 10 ** 400,
            "formality": 3, "note": "secret"}}}
        assert await persona_axes.load_cached(backend, "fp") == {"formality": 3}
        backend.data[persona_axes.CACHE_KEY]["fp"]["axes"] = {"humour": object()}
        assert await persona_axes.load_cached(backend, "fp") is None
    asyncio.run(run())
