"""Typed evidence shared by the Chatter 2.0 context and knowledge engines.

Every fact handed to a dialogue prompt is an ``Evidence`` record. A record
says what kind of fact it is, where it came from, who observed it, which
world instance it belongs to, which audience may hear it and whether it is
still usable. Builders render only records that are fresh and audience
compatible, inside a character budget, highest priority first.

Sources are deliberately distinct:

- ``observed``: a live world observation made by one bot (for example its
  own mana, or a rare it can see). Volatile, short-lived, instance bound.
- ``character``: private state of one character (quest log, gear).
- ``group``: established by the group itself (scoped transcript).
- ``server_data``: configured world database records (quest starters,
  loot tables). Describes configuration, never live availability.
- ``memory``: remembered interactions. Never gameplay proof.
- ``feed``: real-world RSS headlines. Never Azeroth facts.

Statuses distinguish a clean result from uncertainty. ``partial`` means the
search was incomplete (for example nested loot references or conditions),
so absence must never be concluded from it.
"""

from dataclasses import dataclass, field
import json
import time

SOURCES = ('observed', 'character', 'group', 'server_data', 'memory', 'feed')
STATUSES = ('found', 'partial', 'ambiguous', 'not_found', 'unsupported',
            'unavailable')
# Audiences a fact may reach. 'public' facts can be said in any channel;
# 'party' only to the captured party; 'private' only to the owning pair.
AUDIENCES = ('public', 'party', 'private')

# Wording hints for the model. These describe how certain a speaker may
# sound, independent of personality.
STATUS_HINT = {
    'found': 'verified',
    'partial': 'incomplete search; never conclude absence',
    'ambiguous': 'several matches; do not pick one',
    'not_found': 'no matching record; may still exist elsewhere',
    'unsupported': 'cannot be answered from server data',
    'unavailable': 'lookup failed; say you are not sure',
}

PRIORITY = {
    # Live observations and direct answers first, colour last.
    'live_self': 100,
    'rare_seen': 95,
    'quest': 90,
    'item': 85,
    'loot': 85,
    'boss': 80,
    'npc_service': 75,
    'quest_progress': 70,
    'group_fact': 50,
    'relationship': 30,
    'feed': 10,
}


@dataclass
class Evidence:
    kind: str
    source: str
    status: str
    facts: dict
    audience: str = 'public'
    observer: int = 0
    map_id: int = None
    instance_id: int = None
    phase: int = None
    captured_at: float = field(default_factory=time.time)
    ttl: float = None
    note: str = ''

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f'unknown evidence source {self.source!r}')
        if self.status not in STATUSES:
            raise ValueError(f'unknown evidence status {self.status!r}')
        if self.audience not in AUDIENCES:
            raise ValueError(f'unknown evidence audience {self.audience!r}')

    def age(self, now=None):
        return max(0.0, (time.time() if now is None else now) - self.captured_at)

    def is_fresh(self, now=None):
        return self.ttl is None or self.age(now) <= self.ttl

    def allowed_for(self, audience):
        """Whether this fact may be spoken to a channel of ``audience``."""
        if self.audience == 'public':
            return True
        if self.audience == 'party':
            return audience in ('party', 'raid')
        return audience == 'private'

    def matches_world(self, map_id=None, instance_id=None, phase=None):
        """Observations are only valid inside the world copy they came from."""
        for mine, theirs in ((self.map_id, map_id), (self.instance_id, instance_id),
                             (self.phase, phase)):
            if mine is not None and theirs is not None and int(mine) != int(theirs):
                return False
        return True

    def priority(self):
        return PRIORITY.get(self.kind, 40)

    def render(self, now=None):
        """One compact, JSON-quoted line. Values are data, never instructions."""
        payload = {'kind': self.kind, 'source': self.source,
                   'status': STATUS_HINT[self.status]}
        if self.source == 'observed':
            payload['age_s'] = round(self.age(now), 1)
        payload.update(self.facts)
        if self.note:
            payload['note'] = self.note
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'),
                          default=str)


def usable(evidence, audience, now=None, world=None):
    """Filter to records that are fresh, audience-safe and world-matched."""
    world = world or {}
    return [e for e in evidence
            if e.is_fresh(now) and e.allowed_for(audience)
            and e.matches_world(world.get('map_id'), world.get('instance_id'),
                                world.get('phase'))]


def render_block(evidence, audience, budget=900, now=None, world=None):
    """Render usable evidence, highest priority first, within ``budget`` chars.

    Returns ``(text, used)`` where ``used`` is the list of included records,
    so callers can derive delivery claims from exactly what the model saw.
    """
    lines, used, size = [], [], 0
    for item in sorted(usable(evidence, audience, now, world),
                       key=lambda e: (-e.priority(), e.age(now))):
        line = item.render(now)
        if size + len(line) + 1 > budget:
            continue
        lines.append(line)
        used.append(item)
        size += len(line) + 1
    return '\n'.join(lines), used
