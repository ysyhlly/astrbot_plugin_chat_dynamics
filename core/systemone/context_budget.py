"""Reduce prose after exact provider token counting, preserving IDs and options."""
import copy
from ..semantic_excerpt import excerpt, OMITTED


def validate_counts(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('invalid context count schema')
    counts, limits = value.get('counts', {}), value.get('limits', {})
    for key in ('state', 'branch', 'packed'):
        for obj, name in ((counts, key), (limits, 'max_' + key)):
            if type(obj.get(name)) is not int or not 0 < obj[name] <= 2**22:
                raise ValueError('invalid token count or limit')
    return counts, limits


def fits(counts, limits):
    return all(counts[k] <= limits['max_' + k] for k in ('state', 'branch', 'packed'))


def shrink_prose(state, counts, limits):
    """Never alter candidates, target IDs, quotes, commitment states or questions."""
    data = copy.deepcopy(state)
    if not isinstance(data, dict):
        return data
    ratio = max(.25, min(.8, min(limits['max_' + k] / counts[k] for k in counts) * .9))
    def clip(obj, key, floor, preserve_if_indivisible=False):
        value = obj.get(key)
        if isinstance(value, str) and len(value) > floor:
            shortened = excerpt(value, max(floor, int(len(value) * ratio)))
            if not (preserve_if_indivisible and shortened == OMITTED):
                obj[key] = shortened
    conversation = data.get('conversation', {})
    if conversation.get('nearby_context'):
        conversation.pop('nearby_context')
        conversation.pop('nearby_context_policy', None)
        return data  # recount before trimming authoritative/current evidence
    for row in conversation.get('background', []):
        clip(row, 'text', 80)
    # Use the established attribution-preserving clipping function: fragmented
    # input spans must continue to point at the right author's retained text.
    from ..turn_decision import clip_conversation_text
    text = conversation.get('text', '')
    if isinstance(text, str) and len(text) > 256:
        clip_conversation_text(conversation, max(256, int(len(text) * ratio)))
    clip(data, 'persona', 400, True)
    clip(data, 'decision_prompt', 600, True)
    character = data.get('character_state', {})
    clip(character, 'summary', 60)
    for row in character.get('context_items', []):
        clip(row, 'text', 100 if row.get('subject_id') else 60, bool(row.get('subject_id')))
    return data
