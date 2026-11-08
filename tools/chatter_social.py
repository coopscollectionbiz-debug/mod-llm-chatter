"""Chatter 2.0 social decision layer for reactive turns.

Before any lookup or model call, decide:

1. Is a response warranted (or is silence more natural)?
2. Who is being addressed?
3. Which eligible bot is the most appropriate single responder?
4. Has the conversation already moved on, or was the question answered?

Patch 1's ``select_party_responder`` remains the admission gate: it rejects
acknowledgments, lines aimed at another human and non-question chatter,
and honours a directly addressed bot. This layer adds deterministic
re-ranking of *unaddressed* turns (firsthand evidence, role relevance,
interests, familiarity, least-recent participation, GUID tie-break) and
transcript-based supersession and duplicate-question checks. It never
selects more than one responder, so the Patch 1 invariant of at most one
reactive Party reply per eligible turn is preserved.
"""

from dataclasses import dataclass, field
import re

from chatter_reactive import select_party_responder

_NORMALIZE = re.compile(r'[^a-z0-9 ]+')
_HEAL = re.compile(r'\b(heal|heals|healer|mana|oom|drink|rez|res)\b', re.I)
_TANK = re.compile(r'\b(tank|aggro|pull|taunt|patrol|pat)\b', re.I)
_RARE = re.compile(r'\brares?\b', re.I)
_INTEREST_BY_INTENT = {
    'quest_starter': 'quests', 'quest_ender': 'quests', 'quest_info': 'quests', 'quest_eligibility': 'quests',
    'item_source': 'dungeons', 'boss_drop': 'dungeons', 'boss_list': 'dungeons', 'item_info': 'economy',
    'npc_service': 'professions',
}


@dataclass
class Decision:
    respond: bool
    bot: dict = None
    addressed: bool = False
    reason: str = ''
    scores: dict = field(default_factory=dict)


def normalize(text):
    return ' '.join(_NORMALIZE.sub(' ', (text or '').casefold()).split())


def _turn_position(history, player_guid, message):
    """Index of this turn's own line in the scoped transcript, newest match."""
    target = normalize(message)
    for index in range(len(history) - 1, -1, -1):
        line = history[index]
        if (not line.get('is_bot') and int(line.get('speaker_guid') or 0) == int(player_guid)
                and normalize(line.get('message')) == target):
            return index
    return None


def superseded(history, player_guid, message):
    """A newer human line in the same audience means the moment has passed."""
    position = _turn_position(history, player_guid, message)
    if position is None:
        return False
    return any(not line.get('is_bot') for line in history[position + 1:])


def already_answered(history, player_guid, message, window=6):
    """The same person asked the same thing very recently and a bot replied."""
    target = normalize(message)
    if len(target) < 6:
        return False
    position = _turn_position(history, player_guid, message)
    earlier = history[:position] if position is not None else history
    earlier = earlier[-window:]
    for index, line in enumerate(earlier):
        if (not line.get('is_bot') and int(line.get('speaker_guid') or 0) == int(player_guid)
                and normalize(line.get('message')) == target):
            return any(reply.get('is_bot') for reply in earlier[index + 1:])
    return False


def score_candidates(message, bots, intents=(), firsthand=(), familiarity=None, profiles=None):
    """Deterministic suitability scores for an unaddressed question."""
    familiarity = familiarity or {}
    profiles = profiles or {}
    wanted_roles = set()
    if _HEAL.search(message or ''):
        wanted_roles.add('healer')
    if _TANK.search(message or ''):
        wanted_roles.add('tank')
    interests = {_INTEREST_BY_INTENT[i.kind] for i in intents if i.kind in _INTEREST_BY_INTENT}
    scores = {}
    for bot in bots:
        guid = int(bot['bot_guid'])
        profile = profiles.get(guid) or {}
        score = 0.0
        if guid in firsthand:
            score += 60  # it can actually see/knows the thing being asked about
        if (bot.get('role') or '').casefold() in wanted_roles:
            score += 40
        if interests & set(profile.get('gameplay_interests', ())):
            score += 12
        if profile:
            score += profile.get('helpfulness', 50) / 10 + profile.get('talkativeness', 50) / 20
        score += min(10.0, float(familiarity.get(guid, 0)) / 10)
        # Spread replies: penalise whoever spoke most recently in this audience.
        score -= 8 * float(bot.get('last_reply') or 0)
        scores[guid] = round(score, 2)
    return scores


def decide_party(message, bots, other_humans=(), history=(), player_guid=0, intents=(), live=None,
                 familiarity=None, profiles=None):
    """One responder or silence for a Party turn."""
    from chatter_group import _is_playerbot_command
    # Defence in depth: C++ ingress and the handler already drop commands.
    # "@heal follow" is a selector command; "@Thrall how are you" is chat.
    if _is_playerbot_command(message or ''):
        return Decision(False, reason='playerbot_command')
    history = list(history or ())
    if superseded(history, player_guid, message):
        return Decision(False, reason='conversation_moved_on')
    selection = select_party_responder(message, bots, other_humans)
    if selection is None:
        return Decision(False, reason='not_addressed_to_bots')
    bot, addressed = selection
    if addressed:
        return Decision(True, bot, True, 'directly_addressed')
    if already_answered(history, player_guid, message):
        return Decision(False, reason='already_answered')
    firsthand = set()
    if live and _RARE.search(message or ''):
        from chatter_live import firsthand_holders
        firsthand = firsthand_holders(live)
    scores = score_candidates(message, bots, intents, firsthand, familiarity, profiles)
    best = max(bots, key=lambda b: (scores.get(int(b['bot_guid']), 0), -int(b['bot_guid'])))
    reason = ('firsthand_evidence' if int(best['bot_guid']) in firsthand
              else 'role_relevance' if best.get('role') and best is not bot and scores else 'best_fit')
    return Decision(True, best, False, reason, scores)


def decide_general(message, history=(), player_guid=0):
    """Silence a General turn whose moment passed or that bots already answered.

    General is zone-wide: a newer human line in the same audience, or the
    same person repeating a question a bot just answered, means a reply now
    would be noise. Responder choice stays with the General handler.
    """
    from chatter_group import _is_playerbot_command
    if _is_playerbot_command(message or ''):
        return Decision(False, reason='playerbot_command')
    history = list(history or ())
    if superseded(history, player_guid, message):
        return Decision(False, reason='conversation_moved_on')
    if already_answered(history, player_guid, message):
        return Decision(False, reason='already_answered')
    return Decision(True, reason='general_turn')


def decide_whisper(message):
    """Whispers have one fixed recipient; only acknowledgments stay silent."""
    from chatter_reactive import _ACK
    text = (message or '').strip().casefold().rstrip('.!? ')
    if not text or _ACK.fullmatch(text):
        return Decision(False, reason='acknowledgment')
    return Decision(True, reason='whisper_recipient', addressed=True)
