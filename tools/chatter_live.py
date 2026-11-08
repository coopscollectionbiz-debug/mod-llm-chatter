"""Live, audience-scoped observations captured by the worldserver at ingress.

The C++ ingress (``BuildReactiveLiveJson``) attaches a compact ``live``
object to reactive Party and whisper events: the anchor's world copy, each
eligible responder's own state, and, only when the turn mentions rares,
rare creatures a responder can actually see. This module turns that into
``Evidence`` for the *selected* responder only; other bots' state never
reaches the prompt.

Observations are volatile. They carry their capture time, observer and
world copy, expire after a short TTL, and any claim a reply derives from
them is revalidated by delivery before the line is sent.
"""

import time

from chatter_evidence import Evidence

POWER_NAMES = {0: 'mana', 1: 'rage', 2: 'focus', 3: 'energy', 6: 'runic power'}
LOW_POWER_PCT = 35
DEFAULT_TTL = 6.0
RARE_TTL = 8.0


def parse_live(extra):
    live = (extra or {}).get('live')
    if not isinstance(live, dict) or not isinstance(live.get('bots'), list):
        return None
    try:
        live['_captured'] = float(live.get('t', 0)) / 1000.0
    except (TypeError, ValueError):
        return None
    return live


def bot_state(live, bot_guid):
    for row in (live or {}).get('bots', ()):
        if isinstance(row, dict) and int(row.get('g', 0) or 0) == int(bot_guid):
            return row
    return None


def world_of(live, bot_guid=None):
    """World copy and position used to scope knowledge lookups."""
    if not live:
        return {}
    row = bot_state(live, bot_guid) if bot_guid else None
    source = row or live.get('anchor') or {}
    world = {'map_id': source.get('m'), 'instance_id': source.get('i'), 'phase': source.get('p'),
             'difficulty': source.get('d', (live.get('anchor') or {}).get('d'))}
    if row:
        world.update({'x': row.get('x'), 'y': row.get('y'), 'target_entry': row.get('tgt') or None})
    return {k: v for k, v in world.items() if v is not None}


def speaker_evidence(live, bot_guid, now=None, ttl=DEFAULT_TTL, rare_ttl=RARE_TTL, audience='party'):
    """Evidence about the selected bot itself plus rares it can see.

    The speaker's own view is its own to share: ``audience`` follows the
    channel it is answering in (``party`` or ``private`` for a whisper).
    """
    now = time.time() if now is None else now
    if not live:
        return []
    captured = live.get('_captured') or now
    evidence = []
    row = bot_state(live, bot_guid)
    if row:
        power = int(row.get('pt', 0) or 0)
        pct = int(row.get('pp', -1))
        facts = {'alive': bool(row.get('a')), 'in_combat': bool(row.get('c')),
                 'health_pct': int(row.get('hp', 0) or 0), 'role': row.get('role') or ''}
        # Only mana says anything about readiness: rage, energy and runic
        # power sit near zero out of combat by design.
        if pct >= 0 and power == 0:
            facts['power'] = POWER_NAMES[0]
            facts['power_pct'] = pct
            facts['power_low'] = pct < LOW_POWER_PCT
        evidence.append(Evidence(
            'live_self', 'observed', 'found', facts, audience=audience, observer=int(bot_guid),
            map_id=row.get('m'), instance_id=row.get('i'), phase=row.get('p'), captured_at=captured, ttl=ttl,
            note='your own state right now; cooldowns, range and line of sight are not included'))
    for rare in live.get('rares', ()) or ():
        if int(rare.get('g', 0) or 0) != int(bot_guid):
            continue
        evidence.append(Evidence(
            'rare_seen', 'observed', 'found',
            {'name': rare.get('n', ''), 'yards': int(rare.get('yd', 0) or 0), 'spawn': int(rare.get('s', 0)),
             'entry': int(rare.get('e', 0))},
            audience=audience, observer=int(bot_guid), map_id=(row or {}).get('m'),
            instance_id=(row or {}).get('i'), phase=(row or {}).get('p'), captured_at=captured, ttl=rare_ttl,
            note='you can see it alive right now'))
    return evidence


def rare_question_without_sighting(live, bot_guid):
    """A rare was asked about but this bot saw none: an honest 'haven't seen it'."""
    if not live or 'rares' not in live:
        return False
    return not any(int(r.get('g', 0) or 0) == int(bot_guid) for r in live.get('rares') or ())


def firsthand_holders(live):
    """Bots with a fresh firsthand observation worth answering with."""
    return {int(r.get('g', 0) or 0) for r in (live or {}).get('rares', ()) or () if r.get('g')}


def low_power_bots(live):
    return {int(r['g']) for r in (live or {}).get('bots', ())
            if isinstance(r, dict) and int(r.get('pp', -1)) >= 0 and int(r.get('pp', -1)) < LOW_POWER_PCT}
