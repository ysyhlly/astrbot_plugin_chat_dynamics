"""Causal, bounded recipient rules; no guessed parent messages or model calls."""
from __future__ import annotations

from .bot_identity import BotIdentityMatcher


def resolve_recipients(runtime, node, bot_names=(), *, gap=45.0, lifetime=120.0):
    dag, bot = runtime.dag, str(getattr(runtime, 'bot_id', '') or '')
    # Replaying the bounded causal prefix makes retries idempotent and reset-safe.
    # Equal timestamps use DAG insertion order, not lexical message IDs.
    prefix = []
    ordered = dag.get_recent_nodes(0)
    positions = {item.msg_id: index for index, item in enumerate(ordered)}
    for item in ordered:
        if item.timestamp <= node.timestamp and node.timestamp - item.timestamp <= lifetime:
            prefix.append(item)
        if item.msg_id == node.msg_id:
            break
    if not prefix or prefix[-1].msg_id != node.msg_id:
        prefix = [node]
    states = {}
    outcome = dict(ids=[], confidence=0.0, reason='recipient_unknown', anchor_id='',
                   explicit=False, quoted_author_id='', subject_is_bot=False)
    revisions = getattr(runtime, 'topic_stop_revisions', {})
    for current in prefix:
        now, actor = current.timestamp, current.user_id
        revision_owner = str(current.metadata.get('trigger_user_id') or actor) if actor == bot else actor
        states = {owner: state for owner, state in states.items()
                  if actor in {owner, state['target']}
                  and 0 <= now - state['last'] <= gap
                  and 0 <= now - state['start'] <= lifetime}
        if revisions.get(revision_owner, 0) != current.metadata.get('dialogue_stop_revision', revisions.get(revision_owner, 0)):
            states.clear()
            if current is node:
                return dict(ids=[], confidence=0.0, reason='recipient_stopped', anchor_id='',
                            explicit=False, quoted_author_id='', subject_is_bot=False)
            continue
        ids = list(dict.fromkeys(str(uid) for uid in current.metadata.get('actual_mentions', current.mentioned_users) if uid))
        quoted = dag.get_node(current.reply_to_id) if current.reply_to_id else None
        # A future quote target can be stored as a platform ID but not read.
        if quoted is not None and (quoted.timestamp > now or
                positions.get(quoted.msg_id, len(ordered)) >= positions.get(current.msg_id, -1)):
            quoted = None
        quoted_author = str((quoted.user_id if quoted else current.metadata.get('quoted_author_id')) or '')
        reference = BotIdentityMatcher.match(current.text, bot_names)
        if bot and (current.metadata.get('is_wake') or reference.vocative) and bot not in ids:
            ids.append(bot)
        reason, anchor, start = 'recipient_unknown', '', now
        explicit = bool(ids or current.reply_to_id)
        can_anchor = True
        if explicit:
            states.clear()
            if ids:
                reason = 'explicit_mention'
                if current.reply_to_id and quoted_author and quoted_author not in ids:
                    can_anchor = False
            elif quoted_author:
                ids, reason = [quoted_author], 'explicit_reply'
            else:
                can_anchor = False
            anchor = current.msg_id
        elif current.metadata.get('explicit_unresolved') or '[提及]' in current.text:
            states.clear()
            reason, can_anchor = 'recipient_unresolved', False
        else:
            state = states.get(actor)
            incoming = [(owner, value) for owner, value in states.items() if value['target'] == actor]
            if state:
                ids, reason = [state['target']], 'rule_recipient_continuation'
                anchor, start = state['anchor'], state['start']
            elif len(incoming) == 1:
                other, state = incoming[0]
                ids, reason = [other], 'rule_recipient_reciprocal'
                anchor, start = state['anchor'], state['start']
        if len(ids) == 1 and ids[0] not in {actor, 'all'} and can_anchor:
            states[actor] = dict(target=ids[0], anchor=anchor, start=start, last=now)
        else:
            states.clear()
        outcome = dict(ids=ids, confidence=(1.0 if explicit else .80) if ids else 0.0,
                       reason=reason, anchor_id=anchor, explicit=explicit,
                       quoted_author_id=quoted_author, subject_is_bot=bool(reference.subject))
    return outcome
