"""Lazy, GUID-keyed normal-player profiles. No LLM or world-thread work.

SQLite is a bridge-owned sidecar, deliberately separate from RP identities and
conversational memories. Back up this file with the bridge configuration.
"""

import argparse
import copy
from collections import OrderedDict
from functools import wraps
import hashlib
import inspect
import json
from pathlib import Path
import random
import re
import sqlite3
import threading


TRAITS = (
    'friendliness', 'agreeableness', 'patience', 'talkativeness', 'helpfulness',
    'confidence', 'competitiveness', 'sarcasm',
)
# Moderate centers, rather than absolute character stereotypes. Style is drawn
# independently: competition does not imply slang, brevity, or sarcasm.
ARCHETYPES = {
    'quiet': (18, (40, 45, 65, 12, 35, 45, 35, 25)),
    'casual': (22, (55, 55, 65, 45, 45, 50, 25, 30)),
    'sociable': (15, (75, 60, 55, 80, 55, 60, 40, 35)),
    'sarcastic': (10, (40, 35, 45, 50, 30, 65, 45, 75)),
    'impatient': (8, (35, 35, 20, 40, 25, 65, 55, 40)),
    'competitive': (10, (40, 40, 40, 55, 40, 75, 85, 40)),
    'independent': (7, (30, 40, 50, 30, 15, 65, 45, 25)),
    'opinionated': (5, (40, 20, 45, 60, 35, 80, 50, 45)),
    'serious': (5, (45, 45, 60, 35, 55, 65, 60, 10)),
}
INTERESTS = ('quests', 'dungeons', 'raids', 'pvp', 'professions', 'economy', 'alts')
NEWS = ('none', 'none', 'none', 'gaming', 'technology', 'entertainment')
STYLE_CHOICES = {
    'capitalization': ('lowercase', 'sentence case', 'mixed casual case'),
    'punctuation': ('minimal', 'ordinary punctuation'),
    'abbreviations': ('rare', 'occasional common WoW abbreviations'),
    'message_length': ('short fragments', 'one short sentence', 'one or two short sentences'),
    'humor': ('rare', 'dry when appropriate', 'occasional playful remarks'),
}


def generate_profile(guid, version=1, seed='azeroth', weights=None):
    """Stable algorithm: use only a private RNG seeded with SHA-256."""
    guid = int(guid)
    if guid <= 0:
        raise ValueError('A profile requires a positive character GUID')
    digest = hashlib.sha256(f'{seed}:{version}:{guid}'.encode()).digest()
    rng = random.Random(int.from_bytes(digest, 'big'))
    names = list(ARCHETYPES)
    values = [float((weights or {}).get(n, ARCHETYPES[n][0])) for n in names]
    if any(v < 0 for v in values) or not sum(values) > 0:
        raise ValueError('Archetype weights must be nonnegative and total above zero')
    archetype = rng.choices(names, weights=values)[0]
    profile = dict(zip(TRAITS, (
        max(0, min(100, v + rng.randint(-12, 12))) for v in ARCHETYPES[archetype][1]
    )))
    profile.update({k: rng.choice(v) for k, v in STYLE_CHOICES.items()})
    profile.update(
        guid=guid, version=int(version), archetype=archetype,
        question_frequency=rng.randint(5, 25), initiation=rng.randint(25, 90),
        gameplay_interests=rng.sample(INTERESTS, 2), news_interests=[rng.choice(NEWS)],
    )
    return profile


def validate_overrides(values):
    """Reject typos and arbitrary prompt text in operator-supplied overrides."""
    for key, value in values.items():
        if key in (*TRAITS, 'question_frequency', 'initiation'):
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError(f'{key} must be an integer from 0 to 100')
        elif key in STYLE_CHOICES:
            if value not in STYLE_CHOICES[key]:
                raise ValueError(f'{key} must be one of {STYLE_CHOICES[key]}')
        elif key in ('gameplay_interests', 'news_interests'):
            choices = INTERESTS if key == 'gameplay_interests' else NEWS
            if not isinstance(value, list) or not value or any(v not in choices for v in value):
                raise ValueError(f'Invalid {key}')
        else:
            raise ValueError(f'Unknown profile field: {key}')


class ProfileStore:
    """Serialized lazy writes and bounded LRU; no population scan."""

    def __init__(self, path, version=1, seed='azeroth', weights=None, cache_size=4096):
        self.path = Path(path)
        self.version, self.seed, self.weights = int(version), seed, weights
        self.cache_size = max(1, int(cache_size))
        self.cache = OrderedDict()
        self.lock = threading.RLock()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS profiles ('
                   'guid INTEGER PRIMARY KEY, version INTEGER NOT NULL, '
                   'seed TEXT NOT NULL, profile TEXT NOT NULL, overrides TEXT NOT NULL)')
        return db

    def get(self, guid):
        guid = int(guid)
        with self.lock:
            if guid in self.cache:
                self.cache.move_to_end(guid)
                return copy.deepcopy(self.cache[guid])
            db = self._connect()
            try:
                with db:
                    row = db.execute('SELECT version, seed, profile, overrides FROM profiles '
                                     'WHERE guid = ?', (guid,)).fetchone()
                    overrides = json.loads(row[3]) if row else {}
                    if row and row[0] == self.version and row[1] == self.seed:
                        profile = json.loads(row[2])
                    else:
                        profile = generate_profile(guid, self.version, self.seed, self.weights)
                        db.execute('INSERT OR REPLACE INTO profiles VALUES (?, ?, ?, ?, ?)',
                                   (guid, self.version, self.seed, json.dumps(profile), json.dumps(overrides)))
                    profile.update(overrides)
            finally:
                db.close()
            self.cache[guid] = profile
            if len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
            return copy.deepcopy(profile)

    def edit(self, guid, overrides=None):
        """None resets only this profile; explicit edits survive version bumps."""
        if overrides is not None:
            validate_overrides(overrides)
        with self.lock:
            self.get(guid)
            db = self._connect()
            try:
                with db:
                    if overrides is None:
                        db.execute('DELETE FROM profiles WHERE guid = ?', (int(guid),))
                    else:
                        row = db.execute('SELECT overrides FROM profiles WHERE guid = ?', (int(guid),)).fetchone()
                        merged = json.loads(row[0])
                        merged.update(overrides)
                        db.execute('UPDATE profiles SET overrides = ? WHERE guid = ?',
                                   (json.dumps(merged), int(guid)))
            finally:
                db.close()
            self.cache.pop(int(guid), None)


_store = None
_lookup = None
_names = OrderedDict()
_name_lock = threading.RLock()


def configure(config, lookup=None):
    """Call once at bridge startup. A name-only builder resolves GUID once."""
    global _store, _lookup
    def value(key, default):
        raw = config.get(key, default)
        if isinstance(raw, str) and len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            return raw[1:-1]
        return raw
    _store = ProfileStore(
        value('LLMChatter.Personality.Path', str(Path(__file__).parent / 'state' / 'personalities.sqlite3')),
        config.get('LLMChatter.Personality.Version', 1),
        value('LLMChatter.Personality.Seed', 'azeroth'),
        json.loads(value('LLMChatter.Personality.Weights', '{}')),
        config.get('LLMChatter.Personality.CacheSize', 4096),
    )
    if not isinstance(_store.weights, dict):
        raise ValueError('Personality.Weights must be a JSON object')
    if set(_store.weights) - set(ARCHETYPES):
        raise ValueError('Unknown personality archetype weight')
    if lookup is None:
        def lookup(name):
            from chatter_db import get_db_connection
            db = get_db_connection(config)
            try:
                cursor = db.cursor()
                try:
                    cursor.execute('SELECT guid FROM characters WHERE name = %s', (name,))
                    row = cursor.fetchone()
                    if not row:
                        raise ValueError(f'Unknown playerbot {name}')
                    return row[0]
                finally:
                    cursor.close()
            finally:
                db.close()
    _lookup = lookup
    # Validate configuration before polling queues, without creating any rows.
    generate_profile(1, _store.version, _store.seed, _store.weights)
    _names.clear()


def profile_for(name, guid=None):
    if _store is None:
        # Pure builders are also used by offline tooling without bridge config.
        # Runtime ALWAYS configures the service before polling queues.
        key = guid or int.from_bytes(hashlib.sha256(name.casefold().encode()).digest()[:8], 'big')
        return generate_profile(key)
    with _name_lock:
        key = name.casefold()
        if guid:
            _names[key] = int(guid)
        elif key not in _names:
            _names[key] = int(_lookup(name))
        guid = _names[key]
        _names.move_to_end(key)
        if len(_names) > _store.cache_size:
            _names.popitem(last=False)
    return _store.get(guid)


def profile_instruction(name, guid=None):
    p = profile_for(name, guid)
    levels = lambda key: 'low' if p[key] < 35 else 'high' if p[key] > 65 else 'moderate'
    behavior = ', '.join(f'{key}={levels(key)}' for key in TRAITS)
    style = '; '.join(f'{k}={p[k]}' for k in STYLE_CHOICES)
    return (
        f'PLAYER PROFILE {name} [guid={p["guid"]},v={p["version"]}]: {behavior}. '
        f'Typing: {style}. Questions about {p["question_frequency"]}% of turns, never required. '
        f'Interests: {", ".join(p["gameplay_interests"])}. '
        'Apply these tendencies to attitude, participation and wording; avoid caricature.'
    )


def should_speak(profile, directly_addressed=False, important=False, rng=random.random):
    if important:
        return True
    chance = (0.65 + profile['talkativeness'] / 300 if directly_addressed
              else 0.05 + 0.9 * profile['talkativeness'] / 100 * profile['initiation'] / 100)
    return rng() < min(0.98, chance)


def willing_to_speak(name, guid=None, mode='normal', directly_addressed=False, question=False):
    """Optional chatter only. Unconfigured offline tooling retains legacy behavior."""
    if mode == 'roleplay' or _store is None:
        return True
    profile = profile_for(name, guid)
    return (should_speak(profile, directly_addressed)
            and (not question or random.random() * 100 < profile['question_frequency']))


def flexible_conversation(prompt):
    """Retain JSON schema and length limits while allowing a natural early end."""
    def flexible(text):
        text = re.sub(r'EXACTLY (\d+) messages', r'1 to \1 messages', text)
        replacements = {
            'Each bot speaks EXACTLY once': 'Each bot may speak at most once',
            'Every selected bot speaks exactly once.': 'Selected bots may stay silent.',
            'Every selected bot must speak at least once.':
                'Participation may be unequal; early endings are allowed.',
            'Every listed speaker must speak at least once.':
                'Participation may be unequal; early endings are allowed.',
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text
    if hasattr(prompt, 'system_prompt'):
        return type(prompt)(flexible(prompt.user_prompt), flexible(prompt.system_prompt))
    return flexible(prompt)


def player_prompt(builder):
    """Explicit adapter for builders with bot/bots/reactor arguments.

    Keeps PromptParts' system/user split and legacy RP behavior intact.
    Only speaker arguments are inspected, never player targets or NPCs.
    """
    signature = inspect.signature(builder)

    @wraps(builder)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        mode = values.get('mode') or (values.get('config') or {}).get('LLMChatter.ChatterMode', 'normal')
        if mode == 'roleplay':
            return builder(*args, **kwargs)
        speakers = values.get('bots') or [values.get('bot') or values.get('reactor')]
        if not any(speakers) and values.get('bot_name'):
            speakers = [{'name': values['bot_name'], 'guid': values.get('bot_guid')}]
        speakers = [b for b in speakers if isinstance(b, dict) and not b.get('is_npc')]
        instructions = [profile_instruction(b['name'], b.get('guid') or b.get('bot_guid')) for b in speakers]
        # Old session traits/tones may have been edited by the RP command. Never
        # pass those to normal-mode builders. RP identity rows remain untouched.
        if speakers:
            from chatter_mode import resolve_player_personality
            traits, tone = resolve_player_personality(speakers[0]['name'])
            for key in ('traits', 'reactor_traits'):
                if key in values:
                    values[key] = traits
            if 'stored_tone' in values:
                values['stored_tone'] = tone
            if 'traits_map' in values:
                values['traits_map'] = {
                    b['name']: resolve_player_personality(b['name'])[0] for b in speakers
                }
        prompt = builder(*bound.args, **bound.kwargs)
        if not instructions:
            return prompt
        instructions = [line for line in instructions if line not in prompt]
        block = '\n' + '\n'.join(instructions) + '\n'
        if len(speakers) > 1:
            prompt = flexible_conversation(prompt)
            block += ('Each speaker keeps their own style. Unequal participation, disagreement, '
                      'short replies, topic changes and early endings are allowed. '
                      'Do not force consensus, questions, or a line from everyone.\n')
        return prompt + block
    return wrapped


def main():
    parser = argparse.ArgumentParser(description='Offline personality sidecar editor; stop bridge before editing')
    parser.add_argument('path', type=Path)
    parser.add_argument('guid', type=int)
    parser.add_argument('--version', type=int, default=1)
    parser.add_argument('--seed', default='azeroth')
    parser.add_argument('--weights', default='{}', help='JSON weights matching bridge configuration')
    parser.add_argument('--set', dest='values', help='JSON overrides')
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()
    store = ProfileStore(args.path, args.version, args.seed, json.loads(args.weights))
    if args.reset:
        store.edit(args.guid)
    if args.values:
        store.edit(args.guid, json.loads(args.values))
    print(json.dumps(store.get(args.guid), indent=2))


if __name__ == '__main__':
    main()
