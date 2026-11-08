"""Chatter 2.0 reactive reply pipeline shared by Party and whisper turns.

decide (chatter_social) -> reserve (chatter_reactive) -> context
(chatter_live) -> knowledge (chatter_knowledge) -> prompt
(chatter_dialogue) -> model or deterministic fallback -> claims ->
queue (chatter_db) -> delivery revalidation (C++).

Lookups and model calls happen only for an admitted turn, inside the
turn's own deadline (``llm_chatter_events.expires_at``). Playerbot commands
are rejected upstream before this module runs.
"""

from dataclasses import dataclass, field
import logging
import re
import time

from chatter_evidence import Evidence

logger = logging.getLogger(__name__)
_READINESS = re.compile(
    r'\b(heals?|healer|mana|oom|ready|rdy|energy|rage|runic|hp|health|dead|alive|drink(ing)?|'
    r'wait|sec|good to go|gtg|can we (go|pull)|pull)\b', re.I)


@dataclass
class ReplyResult:
    message: str = ''
    claims: str = None
    fallback: bool = False
    used: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def event_deadline(db, event_id, default_seconds=10.0):
    """Monotonic deadline from the event's server-side expiry (clock-skew safe)."""
    try:
        cursor = db.cursor()
        try:
            cursor.execute(
                'SELECT TIMESTAMPDIFF(MICROSECOND, NOW(6), expires_at) FROM llm_chatter_events WHERE id = %s',
                (int(event_id),))
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row and row[0] is not None:
            return time.monotonic() + max(0.0, float(row[0]) / 1e6)
    except Exception:
        pass
    return time.monotonic() + default_seconds


def _rare_unseen_evidence(bot_guid, now, audience='party'):
    return Evidence('rare_seen', 'observed', 'not_found', {'seen': False}, audience=audience,
                    observer=int(bot_guid), captured_at=now, ttl=8.0,
                    note='you have not seen it; that does not mean it is dead or absent')


_PUBLIC_INTENTS = {'quest_starter', 'quest_ender', 'quest_info', 'quest_eligibility', 'item_info',
                   'item_source'}


def with_public_knowledge(config, prompt, raw_message, deadline=None):
    """Append verified public server-data facts to an existing General prompt.

    General is zone-wide and public: only configured world data for linked
    quests/items may be added, never live state, party facts or anything
    requiring the speaker's position. Returns ``(prompt, knowledge_kinds)``.
    """
    from chatter_dialogue import EVIDENCE_RULES
    from chatter_evidence import render_block
    from chatter_knowledge import detect_intents, engine
    from chatter_shared import PromptParts
    knowledge = engine()
    if knowledge is None or str(config.get('LLMChatter.Knowledge.Enable', '1')) != '1':
        return prompt, []
    intents = [i for i in detect_intents(raw_message) if i.kind in _PUBLIC_INTENTS]
    if not intents:
        return prompt, []
    found, _ = knowledge.lookup(intents, deadline=deadline)
    block, used = render_block([e for e in found if e.audience == 'public'], 'public',
                               budget=int(config.get('LLMChatter.Context.BudgetChars', 900)))
    if not block:
        return prompt, []
    data = '\nDATA:\n' + block + '\n'
    if hasattr(prompt, 'system_prompt'):
        return PromptParts(prompt.user_prompt + data, prompt.system_prompt + '\n' + EVIDENCE_RULES), \
            [e.kind for e in used]
    return str(prompt) + data + EVIDENCE_RULES + '\n', [e.kind for e in used]


def generate_reactive_reply(*, client, config, speaker, channel, asker, message, transcript=(),
                            audience_note='', live=None, intents=(), deadline=None, label='reactive_reply',
                            allow_fallback=True, metadata=None):
    """Build evidence and a reply for one admitted turn. Never raises."""
    from chatter_dialogue import build_reply_prompt, derive_claims, fallback_reply, parse_reply
    from chatter_knowledge import engine
    from chatter_live import rare_question_without_sighting, speaker_evidence, world_of
    from chatter_llm import call_llm
    from chatter_personality import profile_for, profile_instruction

    started = time.monotonic()
    stats = {'intents': [i.kind for i in intents]}
    guid = int(speaker['guid'])
    now = time.time()
    evidence = []
    if str(config.get('LLMChatter.Context.LiveStateEnable', '1')) == '1':
        own_audience = 'private' if channel == 'whisper' else 'party'
        observed = speaker_evidence(live, guid, now=now,
                                    ttl=float(config.get('LLMChatter.Context.LiveTTLSeconds', 6)),
                                    rare_ttl=float(config.get('LLMChatter.Context.RareTTLSeconds', 8)),
                                    audience=own_audience)
        # Own state only when the turn is about readiness: an unrelated
        # reply must not inherit a power claim that delivery would check.
        readiness = bool(_READINESS.search(message or ''))
        evidence += [e for e in observed if e.kind != 'live_self' or readiness]
        if rare_question_without_sighting(live, guid):
            evidence.append(_rare_unseen_evidence(guid, now, own_audience))
    world = world_of(live, guid)
    knowledge = engine()
    if intents and knowledge is not None:
        found, kstats = knowledge.lookup(list(intents), world=world, deadline=deadline)
        if channel == 'general':
            found = [e for e in found if e.audience == 'public']
        evidence += found
        stats.update({'lookup_ms': kstats['ms'], 'lookup_failed': kstats['failed'],
                      'cache_hits': kstats['cache_hits'],
                      'knowledge': sorted({e.kind for e in found})})
    profile = profile_for(speaker['name'], guid)
    prompt, used = build_reply_prompt(
        speaker=speaker, profile_line=profile_instruction(speaker['name'], guid), profile=profile,
        channel=channel, audience_note=audience_note, asker=asker, message=message,
        transcript=transcript, evidence=evidence,
        budget=int(config.get('LLMChatter.Context.BudgetChars', 900)),
        mode=str(config.get('LLMChatter.ChatterMode', 'normal')), world=world, intents=intents)
    stats['evidence'] = [e.kind for e in used]
    stats['fresh'] = all(e.is_fresh() for e in used)

    model_started = time.monotonic()
    response = call_llm(client, prompt, config,
                        max_tokens_override=int(config.get('LLMChatter.Dialogue.MaxTokens', 160)),
                        context=f'{channel}:{speaker["name"]}', label=label, metadata=metadata,
                        deadline=deadline)
    stats['model_ms'] = int((time.monotonic() - model_started) * 1000)
    result = ReplyResult(used=used, stats=stats)
    if response is None:
        # Provider down, circuit open or no time left: only deterministic,
        # evidence-backed lines are allowed, and only for a direct question.
        if allow_fallback and str(config.get('LLMChatter.Dialogue.FallbackEnable', '1')) == '1':
            result.message = fallback_reply(used, profile)
            result.fallback = bool(result.message)
    else:
        result.message = parse_reply(response, speaker['name'])
    if result.message:
        result.claims = derive_claims(used, guid)
    stats['total_ms'] = int((time.monotonic() - started) * 1000)
    if deadline is not None:
        stats['deadline_ms'] = int((deadline - time.monotonic()) * 1000)
    return result
