"""Chatter 2.0 shared dialogue engine: one registry of surface policies.

Every model request in the bridge passes ``call_llm``. Before the
relationship enrichment, ``apply`` resolves the request's *surface* (from
its label and the event being dispatched) and adds what that surface's
policy allows, from the same shared infrastructure used by reactive replies:

- a short surface rule block (audience, length, social stance);
- server-data knowledge for the player's own words (intent detection) or
  for the exact quest an event names, filtered by audience and rendered
  through ``chatter_evidence`` with the uncertainty rules;
- RSS colour for social small talk when the speaker's news interests and
  the RSS service's own pacing allow it (never forced);
- lifecycle decisions (player precedence, active conversation, reply
  chains, repeats) through ``chatter_lifecycle``.

Specialised handlers keep their own prompts. Surfaces whose builders
already apply the 2.0 context pipeline (reactive Party/General/whisper) are
marked ``handled`` so nothing is added twice. System labels (memory
summaries, tone, backstory, addressed-bot detection, vision) are explicit
exceptions: they are not dialogue.

Rollback: with ``LLMChatter.Dialogue.Pipeline = 1`` or
``LLMChatter.Surfaces.Enable = 0`` this module is inert and every prompt is
exactly what the legacy builders produced.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import inspect
import json
import logging
import re
import time

import chatter_lifecycle

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SurfacePolicy:
    name: str
    channel: str               # party | raid | general | guild | say | whisper | battleground | system
    audience: str = 'public'   # evidence audience: public | party | private
    knowledge: str = 'none'    # none | intent | quest_id | handled
    rss: str = 'none'          # none | builder | engine
    lifecycle: str = 'event'   # reactive | initiation | reply_chain | event | none
    dedup: bool = True         # drop an opener that repeats this scope's recent lines
    max_speakers: int = 1
    budget: int = 700
    rules: str = ''
    handled: bool = False      # builder already applies the 2.0 pipeline
    exempt: bool = False       # not dialogue


_SHORT = 'Keep it to one short natural chat line; players do not narrate.'
_PUBLIC = ('This is a public channel: strangers can read it. Never reveal party plans, private '
           'conversations, whispers or anything only your group knows.')
_PARTY = 'Only your group hears this. Talk like a groupmate, not a guide or an assistant.'
_SOCIAL = ('Bots in this exchange are other players with their own moods. Let it end naturally after a few '
           'lines; do not ask a new question in the final line, and never restart a finished topic.')
_COMBAT = ('Mid-fight: terse, practical, no lore, no questions that need an answer. Only mention your own '
           'health, mana or target if the prompt states it.')

SURFACES = {s.name: s for s in (
    # Reactive turns owned by the 2.0 reply pipeline.
    SurfacePolicy('party_reactive', 'party', 'party', 'handled', lifecycle='reactive', dedup=False,
                  handled=True),
    SurfacePolicy('whisper', 'whisper', 'private', 'handled', lifecycle='reactive', dedup=False, handled=True),
    SurfacePolicy('general_reactive', 'general', 'public', 'handled', lifecycle='reactive', dedup=False,
                  handled=True),
    # Legacy-builder player turns that the 2.0 pipeline did not own.
    SurfacePolicy('party_player_conv', 'party', 'party', 'intent', lifecycle='reactive', dedup=False,
                  max_speakers=2, rules=_PARTY + ' ' + _SOCIAL),
    SurfacePolicy('guild_player', 'guild', 'public', 'intent', lifecycle='reactive', dedup=False,
                  max_speakers=2, rules='Guild chat: guildmates, familiar but not your party. ' + _SHORT),
    SurfacePolicy('proximity_player', 'say', 'public', 'intent', lifecycle='reactive', dedup=False,
                  max_speakers=2, rules='/say: only people standing nearby hear you. ' + _PUBLIC),
    # Bot-initiated social talk.
    SurfacePolicy('general_ambient', 'general', 'public', rss='builder', lifecycle='initiation',
                  max_speakers=4, rules=_PUBLIC + ' ' + _SOCIAL),
    SurfacePolicy('general_followup', 'general', 'public', lifecycle='reply_chain', max_speakers=2,
                  rules=_PUBLIC + ' ' + _SOCIAL),
    SurfacePolicy('party_idle', 'party', 'party', rss='engine', lifecycle='initiation', max_speakers=4,
                  rules=_PARTY + ' ' + _SOCIAL),
    SurfacePolicy('bot_question', 'party', 'party', lifecycle='initiation', max_speakers=2,
                  rules=_PARTY + ' Ask something a player would actually wonder about; one question only.'),
    SurfacePolicy('guild_idle', 'guild', 'public', rss='builder', lifecycle='initiation', max_speakers=3,
                  rules=_SOCIAL),
    SurfacePolicy('guild_login', 'guild', 'public', lifecycle='event', max_speakers=2, rules=_SHORT),
    SurfacePolicy('proximity', 'say', 'public', rss='engine', lifecycle='initiation', max_speakers=3,
                  rules='/say: only people standing nearby hear you. ' + _SOCIAL),
    SurfacePolicy('proximity_reply', 'say', 'public', lifecycle='reply_chain', max_speakers=2,
                  rules=_SOCIAL),
    # Game events.
    SurfacePolicy('combat', 'party', 'party', lifecycle='event', rules=_COMBAT),
    SurfacePolicy('boss', 'party', 'party', lifecycle='event', rules=_COMBAT),
    SurfacePolicy('dungeon', 'party', 'party', lifecycle='event', rules=_PARTY + ' ' + _SHORT),
    SurfacePolicy('group_event', 'party', 'party', lifecycle='event', max_speakers=2,
                  rules=_PARTY + ' ' + _SHORT),
    SurfacePolicy('quest', 'party', 'party', 'quest_id', lifecycle='event', max_speakers=2,
                  rules=_PARTY + ' Do not invent quest objectives, givers or rewards beyond the prompt and DATA.'),
    SurfacePolicy('group_lifecycle', 'party', 'party', lifecycle='event', max_speakers=2,
                  rules=_PARTY + ' ' + _SHORT),
    SurfacePolicy('raid', 'raid', 'party', lifecycle='event', max_speakers=2, rules=_COMBAT),
    SurfacePolicy('battleground', 'battleground', 'party', lifecycle='event', max_speakers=2,
                  rules='Battleground chat: your team only. ' + _SHORT),
    SurfacePolicy('world_event', 'general', 'public', lifecycle='initiation', max_speakers=3,
                  rules=_PUBLIC + ' ' + _SOCIAL),
    SurfacePolicy('general_loot', 'general', 'public', lifecycle='event', rules=_PUBLIC + ' ' + _SHORT),
    SurfacePolicy('zone', 'general', 'public', lifecycle='event', rules=_PUBLIC + ' ' + _SHORT),
    SurfacePolicy('emote', 'party', 'party', lifecycle='event', rules=_SHORT),
    SurfacePolicy('screenshot', 'party', 'party', lifecycle='event', max_speakers=2,
                  rules=_PARTY + ' Only describe what the description says is visible.'),
    SurfacePolicy('precache', 'general', 'public', lifecycle='none', dedup=False, rules=_PUBLIC),
    SurfacePolicy('system', 'system', 'private', lifecycle='none', dedup=False, exempt=True),
)}

# call_llm label -> surface. Labels not listed resolve by prefix, then by
# the event being dispatched, and finally to 'system' (nothing added).
LABEL_SURFACE = {
    'reactive_reply': 'party_reactive', 'bot_player_whisper': 'whisper',
    'general_player_msg': 'general_reactive', 'general_followup': 'general_followup',
    'group_player_msg': 'party_player_conv', 'group_player_msg_conv': 'party_player_conv',
    'player_msg_conv': 'party_player_conv',
    'guild_player_message': 'guild_player', 'guild_idle_chatter': 'guild_idle',
    'guild_login_greeting': 'guild_login',
    'proximity_player_conversation': 'proximity_player', 'proximity_player_conversation_brief': 'proximity_player',
    'proximity_player_emote': 'proximity_player', 'proximity_player_emote_fallback': 'proximity_player',
    'proximity_say': 'proximity', 'proximity_conversation': 'proximity',
    'proximity_conversation_fallback': 'proximity', 'proximity_reply': 'proximity_reply',
    'ambient_statement': 'general_ambient', 'ambient_conv': 'general_ambient', 'general_conv': 'general_ambient',
    'group_idle_conv': 'party_idle', 'group_idle': 'party_idle', 'group_bot_question': 'bot_question',
    'group_welcome': 'group_lifecycle', 'group_greeting': 'group_lifecycle', 'group_farewell': 'group_lifecycle', 'group_composition': 'group_lifecycle',
    'group_quest_conv': 'quest', 'quest_conv': 'quest', 'reaction_quest_accept': 'quest',
    'reaction_quest_complete': 'quest',
    'reaction_combat': 'combat', 'reaction_low_health': 'combat', 'reaction_oom': 'combat',
    'reaction_aggro_loss': 'combat', 'reaction_spell': 'combat', 'bot_group_combat': 'combat',
    'bot_group_low_health': 'combat', 'bot_group_oom': 'combat', 'bot_group_aggro_loss': 'combat',
    'bot_group_spell_cast': 'combat',
    'reaction_kill': 'boss', 'reaction_wipe': 'boss', 'bot_group_kill': 'boss', 'bot_group_wipe': 'boss',
    'reaction_dungeon_entry': 'dungeon', 'bot_group_dungeon_entry': 'dungeon',
    'reaction_death': 'group_event', 'reaction_rez': 'group_event', 'reaction_corpse_run': 'group_event',
    'reaction_levelup': 'group_event', 'reaction_achievement': 'group_event', 'reaction_loot': 'group_event',
    'reaction_nearby_obj': 'group_event', 'reaction_zone_transition': 'group_event',
    'group_nearby_obj': 'group_event', 'nearby_obj_conv': 'group_event', 'single_reaction': 'group_event',
    'bot_group_death': 'group_event', 'bot_group_resurrect': 'group_event', 'bot_group_corpse_run': 'group_event',
    'bot_group_levelup': 'group_event', 'bot_group_achievement': 'group_event', 'bot_group_loot': 'group_event',
    'reaction_emote': 'emote', 'reaction_emote_obs': 'emote',
    'reaction_zone_intrusion': 'zone', 'general_loot': 'general_loot',
    'reaction_world_event': 'world_event', 'event_conv': 'world_event',
    'raid_battle_cry': 'raid', 'screenshot_conv': 'screenshot', 'precache': 'precache',
    'memory_generation': 'system', 'bot_tone': 'system', 'bot_backstory': 'system',
    'guild_session_summary': 'system', 'find_addressed_bot': 'system', 'screenshot_vision': 'system',
}
_PREFIX_SURFACE = (('reaction_bg_', 'battleground'), ('bg_', 'battleground'), ('raid_', 'raid'),
                   ('proximity_', 'proximity'), ('guild_', 'guild_idle'))

# Event type -> surface, for labels the table does not name.
EVENT_SURFACE = {
    'bot_group_player_msg': 'party_player_conv', 'player_general_msg': 'general_reactive',
    'bot_player_whisper': 'whisper', 'guild_player_message': 'guild_player',
    'guild_idle_chatter': 'guild_idle', 'guild_login_greeting': 'guild_login',
    'proximity_player_say': 'proximity_player', 'proximity_player_conversation': 'proximity_player',
    'proximity_player_emote': 'proximity_player', 'proximity_say': 'proximity',
    'proximity_conversation': 'proximity', 'proximity_reply': 'proximity_reply',
    'proximity_boss_approach': 'boss', 'proximity_boss_player_say': 'boss',
    'bot_group_quest_accept': 'quest', 'bot_group_quest_accept_batch': 'quest',
    'bot_group_quest_complete': 'quest', 'bot_group_quest_objectives': 'quest',
    'bot_group_join': 'group_lifecycle', 'bot_group_join_batch': 'group_lifecycle',
    'bot_group_farewell': 'group_lifecycle', 'bot_group_emote_reaction': 'emote',
    'bot_group_emote_observer': 'emote', 'bot_group_screenshot_observation': 'screenshot',
    'bot_group_general_reaction': 'general_loot', 'bot_loot_item': 'general_loot',
    'bot_group_zone_transition': 'group_event', 'bot_group_subzone_change': 'group_event',
    'bot_group_nearby_object': 'group_event', 'player_enters_zone': 'zone',
    'raid_boss_pull': 'raid', 'raid_boss_kill': 'raid', 'raid_boss_wipe': 'raid', 'raid_idle_morale': 'raid',
    'weather_change': 'world_event', 'weather_ambient': 'world_event', 'day_night_transition': 'world_event',
    'holiday_start': 'world_event', 'holiday_end': 'world_event', 'transport_arrives': 'world_event',
    'minor_event': 'world_event',
    'bot_backstory_regen': 'system', 'bot_tone_regen': 'system',
}

PLAYER_EVENTS = {'player_general_msg', 'bot_group_player_msg', 'guild_player_message',
                 'proximity_player_say', 'proximity_player_conversation', 'proximity_player_emote',
                 'proximity_boss_player_say', 'bot_player_whisper'}


def surface_for(label='', event_type=''):
    label = label or ''
    if label.endswith('_memory'):
        label = label[:-len('_memory')]  # same surface, memory-enriched prompt variant
    name = LABEL_SURFACE.get(label) or EVENT_SURFACE.get(label)
    if name is None and event_type:
        name = EVENT_SURFACE.get(event_type)
        if name is None and event_type.startswith('bot_group_'):
            name = 'group_event'
    if name is None and label:
        name = next((s for prefix, s in _PREFIX_SURFACE if label.startswith(prefix)), None)
    if name is None and event_type:
        name = next((s for prefix, s in _PREFIX_SURFACE if event_type.startswith(prefix)), None)
    return SURFACES[name or 'system']


@dataclass
class Dispatch:
    event_type: str
    extra: dict = field(default_factory=dict)
    db: object = None
    scope: str = ''
    suppressed: bool = False
    applied: list = field(default_factory=list)


_dispatch = ContextVar('chatter_surface_dispatch', default=None)
_enabled = False  # until the bridge calls configure(); inert in isolated imports
_stats = {}


def _count(key):
    _stats[key] = _stats.get(key, 0) + 1


def stats():
    return dict(_stats, lifecycle=chatter_lifecycle.registry().snapshot())


def configure(config):
    """Read switches once (bridge config is static until restart)."""
    global _enabled
    _enabled = (str(config.get('LLMChatter.Surfaces.Enable', '1')).strip() == '1'
                and str(config.get('LLMChatter.Dialogue.Pipeline', '2')).strip() != '1'
                and str(config.get('LLMChatter.ChatterMode', 'normal')).strip().lower() == 'normal')
    chatter_lifecycle.configure(config)
    _stats.clear()
    return _enabled


def enabled():
    return _enabled


def _extra_of(event):
    extra = event.get('extra_data') or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (ValueError, TypeError):
            extra = {}
    return extra if isinstance(extra, dict) else {}


def scope_key(channel, extra, event=None):
    """The audience a line lands in; lifecycle and dedup are per scope."""
    event = event or {}
    zone = int(extra.get('zone_id') or event.get('zone_id') or 0)
    if channel in ('party', 'raid'):
        gid = int(extra.get('group_id') or event.get('_group_id') or 0)
        return f'party:{gid}' if gid else ''
    if channel == 'guild':
        gid = int(extra.get('guild_id') or 0)
        return f'guild:{gid}' if gid else ''
    if channel == 'say':
        area = int(extra.get('area_id') or zone)
        return f'say:{zone}:{area}' if zone else ''
    if channel == 'general':
        return f'general:{zone}' if zone else ''
    if channel == 'battleground':
        instance = int(extra.get('instance_id') or extra.get('map_id') or 0)
        return f'bg:{instance}' if instance else ''
    if channel == 'whisper':
        return f"whisper:{int(extra.get('player_guid') or 0)}:{int(extra.get('bot_guid') or 0)}"
    return ''


def _talkativeness(name, guid):
    try:
        from chatter_personality import profile_for
        return int(profile_for(name or str(guid), guid).get('talkativeness', 50))
    except Exception:
        return 50


def admit_event(event):
    """Lifecycle decision for one claimed event before its handler runs.

    Returns ``(ok, reason)``. Player events record precedence and are always
    admitted; initiation and reply-chain events honour the scope's state.
    """
    if not _enabled:
        return True, 'disabled'
    kind = event.get('event_type', '')
    extra = _extra_of(event)
    policy = surface_for('', kind)
    scope = scope_key(policy.channel, extra, event)
    life = chatter_lifecycle.registry()
    if kind in PLAYER_EVENTS:
        life.note_player(scope)
        return True, 'player'
    if policy.lifecycle == 'initiation':
        ok, reason = life.admit_initiation(
            scope, conversation=policy.max_speakers > 1,
            talkativeness=_talkativeness(extra.get('bot_name', ''), int(extra.get('bot_guid') or 0)))
    elif policy.lifecycle == 'reply_chain':
        ok, reason = life.admit_reply(scope)
    else:
        return True, 'event'
    _count(f'{policy.name}.{reason}')
    return ok, reason


@contextmanager
def dispatch_context(db, event):
    if not _enabled:
        yield None
        return
    kind = event.get('event_type', '')
    extra = _extra_of(event)
    policy = surface_for('', kind)
    current = Dispatch(kind, extra, db, scope_key(policy.channel, extra, event))
    token = _dispatch.set(current)
    try:
        yield current
    finally:
        _dispatch.reset(token)


def initiation_gate(kind):
    """Decorator for queue/idle builders: lifecycle gate plus dispatch scope.

    ``kind`` is 'general' (ambient queue: request.zone_id) or 'party'
    (idle: group_id). A gated call returns ``silent`` without any model
    request: True for queue rows (completed, intentionally quiet), False for
    idle (nothing generated), matching each builder's own quiet paths.
    """
    def decorate(builder):
        signature = inspect.signature(builder)
        silent = kind == 'general'

        @wraps(builder)
        def wrapped(*args, **kwargs):
            if not _enabled:
                return builder(*args, **kwargs)
            values = signature.bind_partial(*args, **kwargs).arguments
            if kind == 'general':
                request = values.get('request') or {}
                extra = {'zone_id': request.get('zone_id') or 0}
                speakers = [values['bot']] if values.get('bot') else list(values.get('bots') or ())
                conversation = len(speakers) > 1
                event_type = 'ambient_conv' if conversation else 'ambient_statement'
                channel = 'general'
            else:
                extra = {'group_id': values.get('group_id') or 0, 'zone_id': values.get('zone_id') or 0}
                speakers = list(values.get('bot_rows') or values.get('all_bots') or ())
                conversation = builder.__name__ == '_idle_conversation'
                event_type = 'group_idle_conv' if conversation else 'group_idle'
                channel = 'party'
            scope = scope_key(channel, extra)
            first = speakers[0] if speakers else {}
            talk = _talkativeness(first.get('name') or first.get('bot_name', ''),
                                  int(first.get('guid') or first.get('bot_guid') or 0))
            ok, reason = chatter_lifecycle.registry().admit_initiation(
                scope, conversation=conversation, talkativeness=talk)
            _count(f'{event_type}.{reason}')
            if not ok:
                LOG.debug('surface gate: %s in %s quiet (%s)', event_type, scope, reason)
                return silent
            token = _dispatch.set(Dispatch(event_type, extra, values.get('db'), scope))
            try:
                return builder(*args, **kwargs)
            finally:
                _dispatch.reset(token)
        return wrapped
    return decorate


def admit_line(message, sequence=0, channel=''):
    """Central insert check: suppress a repeated opener in the same scope.

    Only the first line of a dispatch is judged, so a conversation is either
    kept whole or dropped whole; every line is remembered for later checks.
    """
    current = _dispatch.get()
    if not _enabled or current is None or not current.scope:
        return True
    if current.suppressed:
        return False
    policy = surface_for(current.event_type, current.event_type)
    repeat = chatter_lifecycle.registry().is_repeat(current.scope, message)
    if policy.dedup and repeat and int(sequence or 0) == 0:
        current.suppressed = True
        _count(f'{policy.name}.repeat_dropped')
        return False
    return True


# -- prompt augmentation ----------------------------------------------------

_PUBLIC_INTENTS = {'quest_starter', 'quest_ender', 'quest_info', 'quest_eligibility', 'item_info', 'item_source'}
_PARTY_INTENTS = _PUBLIC_INTENTS | {'boss_drop', 'boss_list', 'npc_service'}


def _knowledge(policy, current, config):
    if policy.knowledge not in ('intent', 'quest_id') or current is None:
        return []
    if str(config.get('LLMChatter.Knowledge.Enable', '1')) != '1':
        return []
    from chatter_knowledge import Intent, detect_intents, engine
    knowledge = engine()
    if knowledge is None:
        return []
    extra = current.extra
    intents = []
    if policy.knowledge == 'quest_id' and int(extra.get('quest_id') or 0) > 0:
        intents.append(Intent('quest_info', int(extra['quest_id']), str(extra.get('quest_name') or '')[:80]))
    elif policy.knowledge == 'intent':
        allowed = _PARTY_INTENTS if policy.audience == 'party' else _PUBLIC_INTENTS
        intents = [i for i in detect_intents(str(extra.get('player_message') or '')) if i.kind in allowed]
    if not intents:
        return []
    world = {k: extra.get(k) for k in ('map_id', 'zone_id', 'instance_id') if extra.get(k) is not None}
    deadline = time.monotonic() + float(config.get('LLMChatter.Surfaces.KnowledgeSeconds', 1.2))
    found, _ = knowledge.lookup(intents, world=world, deadline=deadline)
    return [e for e in found if e.allowed_for(policy.audience)]


def _rss(policy, label, config, metadata):
    if policy.rss != 'engine':
        return None
    from chatter_relationships import _scope as conversation
    current = conversation.get()
    speakers = [{'name': n, 'guid': g} for g, n in (current.trusted_speakers.items() if current else ())]
    if not speakers:
        return None
    from chatter_rss import select_topic
    return select_topic(config, speakers[:policy.max_speakers], label)


# -- grounding guards (real-model evaluation findings) -----------------------
#
# gpt-4o-mini named drop sources it had no data for (6/6, some wrong) and
# invented its own mana percentage on event surfaces (2/27). Prompt rules
# alone did not stop either, so call_llm also checks the reply.

SOURCE_RULE = ('SOURCE RULE: DATA has no verified source for what the player is asking about. Do not name '
               'mobs, creature types, zones, NPCs or vendors as where it drops, comes from or is sold. Say you '
               'are not sure, or suggest checking, in your own voice.')
_SOURCE_QUESTION = re.compile(
    r"\b(where|who|what)\b[^?.!]{0,60}\b(drops?|dropped|loot(?:s|ed)?|farm|get|find|buy|sells?|comes? from|"
    r"obtain)\b|\bwhat drops\b", re.I)
_SOURCE_CLAIM = re.compile(
    r"\b(drops? (?:from|off)|dropped by|drop from|loot(?:s|ed)? (?:from|off)|comes? from|sold by|sells? (?:it|them)|"
    r"farm (?:it|them) (?:from|in|at|off)|you can (?:get|find|buy|farm) [^.!?]{0,50}\b(?:from|at|in|off)\b|"
    r"from (?:killing )?[a-z' -]{0,30}\b(?:mobs?|humanoids?|sheep|gnolls?|murlocs?|kobolds?|wolves|wolf|"
    r"spiders?|boars?|bandits?|defias|trolls?|ogres?|vendors?))", re.I)
_SOURCE_LINE = re.compile(r'"kind":"loot","source":"server_data","status":"(verified|incomplete search)')
_SELF_STAT = re.compile(
    r"\b(\d{1,3})\s*%\s*(?:mana|health|hp)\b|\b(?:mana|health|hp)\s*(?:is\s*)?(?:at\s*)?(\d{1,3})\s*%", re.I)


# Word-form own-state claims ("i'm at half health", "full mana", "low on hp").
# Allowed only when the prompt carries the speaker's health/mana state at all.
_SELF_STATE_WORDS = re.compile(
    r"\b(?:i'?m|i am|im|me|my)\b[^.!?]{0,20}\b(?:at |on )?(?:half|full|low|quarter|no)\s+(?:on\s+)?"
    r"(?:health|hp|mana)\b|\blow on (?:health|hp)\b|\bnearly dead\b", re.I)
_STATE_IN_PROMPT = re.compile(r"\d{1,3}\s*%\s*(?:health|mana|hp)|_pct\"?\s*[:=]|"
                              r"\b(?:critically wounded|low on (?:health|mana))\b", re.I)


def _player_text(current):
    return str((current.extra.get('player_message') if current is not None else '') or '')


def source_question(text):
    return bool(text and _SOURCE_QUESTION.search(text))


def has_source_data(prompt_text):
    """DATA carries a found/partial loot-source record (the only verified 'where it drops')."""
    return bool(_SOURCE_LINE.search(prompt_text or ''))


def _stat_supported(number, prompt_text):
    n = re.escape(str(int(number)))
    return bool(re.search(rf'(?<!\d){n}\s*%|_pct"?\s*[:=]\s*{n}(?!\d)', prompt_text or ''))


def check_reply(response, prompt, label):
    """Reason to discard a model reply, or ''. Never raises.

    - ``invented_self_stat``: a concrete health/mana percentage the prompt never gave.
    - ``ungrounded_source``: the player asked where something comes from, DATA
      has no verified source, and the reply names one anyway.
    """
    if not _enabled or not response:
        return ''
    try:
        current = _dispatch.get()
        policy = surface_for(label, current.event_type if current else '')
        if policy.exempt:
            return ''
        # Models often emit typographic apostrophes ("i’m"); compare on ASCII.
        text = str(response).replace('’', "'").replace('‘', "'")
        prompt_text = str(prompt)
        for match in _SELF_STAT.finditer(text):
            number = match.group(1) or match.group(2)
            if not _stat_supported(number, prompt_text):
                _count(f'{policy.name}.invented_self_stat')
                return 'invented_self_stat'
        if _SELF_STATE_WORDS.search(text) and not _STATE_IN_PROMPT.search(prompt_text):
            _count(f'{policy.name}.invented_self_stat')
            return 'invented_self_stat'
        if (source_question(_player_text(current)) and not has_source_data(prompt_text)
                and _SOURCE_CLAIM.search(text)):
            _count(f'{policy.name}.ungrounded_source')
            return 'ungrounded_source'
    except Exception:
        LOG.debug('surface reply check skipped for %s', label, exc_info=True)
    return ''


def apply(prompt, label, config, metadata=None):
    """Add the surface's shared context to ``prompt``. Never raises."""
    if not _enabled:
        return prompt
    try:
        return _apply(prompt, label, config, metadata if metadata is not None else {})
    except Exception:
        LOG.debug('surface context skipped for %s', label, exc_info=True)
        _count('apply_error')
        return prompt


def _apply(prompt, label, config, metadata):
    current = _dispatch.get()
    policy = surface_for(label, current.event_type if current else '')
    _count(f'apply.{policy.name}')
    if policy.exempt:
        return prompt
    from chatter_shared import PromptParts
    system_add = []
    user_add = ''
    # Every surface, including the 2.0 reactive ones: a "where does X drop?"
    # question with no verified source in DATA must not get a guessed source.
    if source_question(_player_text(current)) and not has_source_data(str(prompt)):
        system_add.append(SOURCE_RULE)
        _count(f'{policy.name}.source_unknown')
    if policy.handled:
        if not system_add:
            return prompt
        if hasattr(prompt, 'system_prompt'):
            return PromptParts(prompt.user_prompt, (prompt.system_prompt + '\n' + SOURCE_RULE).strip())
        return PromptParts(str(prompt), SOURCE_RULE)
    if policy.rules:
        system_add.append(f'SURFACE ({policy.name}, heard by: {policy.channel}): {policy.rules}')
    evidence = _knowledge(policy, current, config)
    if evidence:
        from chatter_dialogue import EVIDENCE_RULES
        from chatter_evidence import render_block
        block, used = render_block(evidence, policy.audience, budget=policy.budget)
        if block:
            user_add += '\nDATA:\n' + block + '\n'
            system_add.append(EVIDENCE_RULES)
            metadata['surface_knowledge'] = ','.join(sorted({e.kind for e in used}))
            _count(f'{policy.name}.knowledge')
    if current is not None:
        current.applied.append(policy.name)
    selection = _rss(policy, label, config, metadata)
    if not system_add and not user_add and not selection:
        return prompt
    if hasattr(prompt, 'system_prompt'):
        prompt = PromptParts(prompt.user_prompt + user_add,
                             (prompt.system_prompt + '\n' + '\n'.join(system_add)).strip())
    else:
        prompt = PromptParts(str(prompt) + user_add, '\n'.join(system_add))
    if selection:
        from chatter_rss import attach_context
        prompt = attach_context(prompt, selection, metadata)
        _count(f'{policy.name}.rss')
    metadata['surface'] = policy.name
    return prompt
