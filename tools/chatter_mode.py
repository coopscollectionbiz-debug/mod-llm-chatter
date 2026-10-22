"""Canonical prompt rules for playerbot chatter modes.

Playerbots follow ``LLMChatter.ChatterMode``. Actual NPCs always remain
in-world, including when they share a proximity scene with playerbots.
"""

import hashlib


_NORMAL_PLAYER_STYLE_PROFILES = [
    (('friendly', 'easygoing'), 'casual and conversational'),
    (('quiet', 'reserved'), 'short and low-key'),
    (('helpful', 'practical'), 'straightforward and useful'),
    (('relaxed', 'casual'), 'loose and unhurried'),
    (('dry-humored', 'observant'), 'dry and concise'),
    (('chatty', 'curious'), 'talkative and engaged'),
    (('focused', 'no-nonsense'), 'brief and direct'),
    (('experienced', 'laid-back'), 'confident and casual'),
    (('playful', 'teasing'), 'lightly teasing'),
    (('blunt', 'impatient'), 'direct and occasionally annoyed'),
    (('competitive', 'intense'), 'competitive and energetic'),
    (('sarcastic', 'dry'), 'sarcastic without constantly joking'),
    (('newer', 'curious'), 'unsure sometimes and willing to ask'),
    (('social', 'talkative'), 'sociable and informal'),
    (('independent', 'matter-of-fact'), 'plain-spoken and self-contained'),
    (('salty', 'competitive'), 'occasionally salty when things go badly'),
    (('mature', 'calm'), 'steady without sounding formal'),
    (('casual', 'adaptable'), 'natural and low-key'),
    (('analytical', 'particular'), 'specific and opinionated'),
    (('self-deprecating', 'dry'), 'self-deprecating and casual'),
    (('impatient', 'blunt'), 'short when annoyed or waiting'),
    (('goofy', 'friendly'), 'silly sometimes without forcing jokes'),
    (('skeptical', 'opinionated'), 'willing to disagree'),
    (('tired', 'low-key'), 'low-energy and terse'),
]

_NORMAL_PLAYER_EXTRA_TRAITS = [
    'low-key',
    'curious',
    'straightforward',
    'good-humored',
    'game-focused',
    'opinionated',
    'laid-back',
    'talkative',
    'terse',
    'competitive',
    'sarcastic',
    'helpful',
    'impatient',
    'easygoing',
]


def normalize_chatter_mode(mode: str) -> str:
    """Return a supported playerbot chatter mode."""
    value = str(mode or '').strip().lower()
    return value if value in ('normal', 'roleplay') else 'normal'


def is_roleplay(mode: str) -> bool:
    """Return whether playerbots should speak in character."""
    return normalize_chatter_mode(mode) == 'roleplay'


def resolve_player_personality(
    name: str,
    traits=None,
    tone: str = '',
    mode: str = 'normal',
):
    """Return RP identity metadata or a stable normal-player profile.

    Persistent identities predate the mode boundary and may contain mystical
    or in-world traits. Normal mode must never send that legacy metadata to
    the model, so it derives a deterministic player-side style from the bot
    name instead. Roleplay mode receives the stored values unchanged.
    """
    if is_roleplay(mode):
        return [value for value in (traits or []) if value], tone or ''

    seed = str(name or 'playerbot').strip().casefold().encode('utf-8')
    digest = hashlib.sha256(seed).digest()
    index = int.from_bytes(digest[:4], 'big') % len(
        _NORMAL_PLAYER_STYLE_PROFILES
    )
    player_traits, player_tone = _NORMAL_PLAYER_STYLE_PROFILES[index]
    extra_index = int.from_bytes(digest[4:8], 'big') % len(
        _NORMAL_PLAYER_EXTRA_TRAITS
    )
    return (
        list(player_traits)
        + [_NORMAL_PLAYER_EXTRA_TRAITS[extra_index]],
        player_tone,
    )



# Stable fictional human backgrounds for Normal-mode playerbots.
# Independent of WoW character race, class, gender, and level.

def _load_human_population():
    """Load versioned fictional player background data."""
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent
        / "data"
        / "player_population.json"
    )

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if data.get("version") != "international-player-v1":
        raise ValueError("Unsupported population dataset version")

    def freeze(value):
        if isinstance(value, list):
            return tuple(freeze(item) for item in value)
        if isinstance(value, dict):
            return {
                key: freeze(item)
                for key, item in value.items()
            }
        return value

    return data, freeze


_HUMAN_POPULATION, _human_freeze = _load_human_population()

_HUMAN_CULTURES = _human_freeze(_HUMAN_POPULATION["cultures"])
_HUMAN_CAREERS = _human_freeze(_HUMAN_POPULATION["careers"])
_HUMAN_HOBBIES = _human_freeze(_HUMAN_POPULATION["hobbies"])
_HUMAN_SOCIAL_HABITS = _human_freeze(
    _HUMAN_POPULATION["social_habits"]
)
_HUMAN_WOW_HISTORY = _human_freeze(
    _HUMAN_POPULATION["wow_history"]
)




def _human_pick(digest, offset, choices):
    number = int.from_bytes(digest[offset:offset + 4], 'big')
    return choices[number % len(choices)]


def resolve_player_age(name: str) -> int:
    """Deterministic truncated normal distribution, ages 18-65."""
    import math

    seed = str(name or 'playerbot').strip().casefold()

    for attempt in range(100):
        digest = hashlib.sha256(
            f"human-age-v1:{seed}:{attempt}".encode('utf-8')
        ).digest()

        u1 = (int.from_bytes(digest[:8], 'big') + 0.5) / (2 ** 64)
        u2 = (int.from_bytes(digest[8:16], 'big') + 0.5) / (2 ** 64)

        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(
            2.0 * math.pi * u2
        )
        age = round(32 + 10 * z)

        if 18 <= age <= 65:
            return age

    return 32


def resolve_player_background(name: str, mode: str = 'normal'):
    """Stable, age-aware fictional human identity."""
    if is_roleplay(mode):
        return None

    seed = str(name or 'playerbot').strip().casefold().encode('utf-8')
    digest = hashlib.sha256(b'human-background-v2:' + seed).digest()

    age = resolve_player_age(name)

    culture, hometown = _human_pick(digest, 0, _HUMAN_CULTURES)

    if age <= 23:
        career_group = 'young'
    elif age >= 48:
        career_group = 'older'
    else:
        career_group = 'adult'

    career, education, schedule = _human_pick(
        digest, 4, _HUMAN_CAREERS[career_group]
    )

    if age <= 21:
        family_options = (
            "single, lives with family",
            "single, shares housing with friends",
            "dating, lives with roommates",
            "single, lives in student housing",
        )
    elif age <= 29:
        family_options = (
            "single, lives alone",
            "single, shares an apartment",
            "dating, lives with a partner",
            "married, no children",
            "married, has a young child",
        )
    elif age <= 44:
        family_options = (
            "single, lives alone",
            "married, no children",
            "married, has one child",
            "married, has two children",
            "divorced, co-parents a child",
            "lives with a long-term partner",
        )
    else:
        family_options = (
            "married, has teenage children",
            "married, has adult children",
            "single, lives alone",
            "divorced, has adult children",
            "married, no children",
            "lives with a long-term partner",
        )

    family = _human_pick(digest, 8, family_options)
    hobbies = _human_pick(digest, 12, _HUMAN_HOBBIES)
    social = _human_pick(digest, 16, _HUMAN_SOCIAL_HABITS)

    # WoW began in 2004; keep experience plausible for the bot's age.
    if age <= 22:
        wow_group = 'new'
    elif age <= 27:
        wow_group = 'intermediate'
    else:
        wow_group = ('veteran', 'intermediate', 'new')[
            int.from_bytes(digest[20:24], 'big') % 3
        ]

    wow_history = _human_pick(
        digest, 24, _HUMAN_WOW_HISTORY[wow_group]
    )

    return {
        'age': age,
        'ethnicity': culture,
        'hometown': hometown,
        'location': hometown,
        'career': career,
        'education': education,
        'work_schedule': schedule,
        'family': family,
        'hobbies': hobbies,
        'wow_history': wow_history,
        'social_habits': social,
    }


def build_player_background_context(
    name: str, mode: str = 'normal', compact: bool = False
) -> str:
    """Format private fictional player background for prompts."""
    background = resolve_player_background(name, mode)
    if background is None:
        return ''

    # Experimental version 2 is opt-in and Normal-mode only.
    # The default path preserves existing biographies and prompts.
    import os
    if os.environ.get("LLM_CHATTER_POPULATION_V2", "").strip() == "1":
        from chatter_population import resolve_player_background_v2

        background = resolve_player_background_v2(name).copy()
        background["family"] = background["household"]

    if compact:
        return (
            f"Fictional player background for {name}: "
            f"age {background['age']}; "
            f"{background['ethnicity']}; "
            f"lives in {background['location']}; "
            f"career: {background['career']}; "
            f"interests: {background['hobbies']}; "
            f"WoW history: {background['wow_history']}. "
            "Only mention these details when naturally relevant."
        )

    return (
        f"PRIVATE FICTIONAL PLAYER BACKGROUND ({name}):\n"
        f"Age: {background['age']}\n"
        f"Human ethnicity/cultural background: {background['ethnicity']}\n"
        f"Hometown: {background['hometown']}\n"
        f"Current location: {background['location']}\n"
        f"Career: {background['career']}\n"
        f"Education: {background['education']}\n"
        f"Work schedule: {background['work_schedule']}\n"
        f"Family/relationships: {background['family']}\n"
        f"Hobbies: {background['hobbies']}\n"
        f"WoW history: {background['wow_history']}\n"
        f"Social habits: {background['social_habits']}\n"
        "This background is private fictional context, not a script. "
        "Mention personal details only when naturally relevant. "
        "Never recite the biography or force unrelated personal stories "
        "into gameplay conversation. Ethnicity and nationality must not "
        "determine personality, accent, intelligence, or behavior."
    )


def build_player_identity(
    name: str,
    race: str = '',
    class_name: str = '',
    level=None,
    gender: str = '',
    mode: str = 'normal',
) -> str:
    """Build an identity with an explicit player/character boundary."""
    details = []
    if level not in (None, '', 0, '0'):
        details.append(f"level {level}")
    if gender:
        details.append(str(gender))
    if race:
        details.append(str(race))
    if class_name:
        details.append(str(class_name))
    avatar = ' '.join(details) or 'WoW character'

    if is_roleplay(mode):
        return f"You are {name}, a {avatar} in World of Warcraft."
    return (
        f"You are {name}, a person playing a {avatar} "
        "character in World of Warcraft.\n"
        + build_player_background_context(name, mode=mode)
    )


def build_player_identity_from_dict(
    bot: dict,
    mode: str,
    include_level: bool = True,
) -> str:
    """Build the mode-aware identity for a standard bot dictionary."""
    return build_player_identity(
        bot.get('name', 'Unknown'),
        bot.get('race', ''),
        bot.get('class', ''),
        bot.get('level') if include_level else None,
        bot.get('gender', ''),
        mode,
    )


def build_player_prompt_header(
    name: str,
    race: str = '',
    class_name: str = '',
    level=None,
    gender: str = '',
    mode: str = 'normal',
    channel: str = 'party',
) -> str:
    """Build a playerbot identity followed by its channel voice contract."""
    return (
        build_player_identity(
            name, race, class_name, level, gender, mode
        )
        + "\n"
        + build_player_chat_guidance(mode, channel)
    )


def build_player_prompt_header_from_dict(
    bot: dict,
    mode: str,
    channel: str = 'party',
) -> str:
    """Build a standard playerbot prompt header from a bot dictionary."""
    return build_player_prompt_header(
        bot.get('name', 'Unknown'),
        bot.get('race', ''),
        bot.get('class', ''),
        bot.get('level'),
        bot.get('gender', ''),
        mode,
        channel,
    )


def build_player_chat_guidance(
    mode: str,
    channel: str = 'party',
) -> str:
    """Return the shared voice contract for playerbot chat prompts."""
    if is_roleplay(mode):
        return (
            "CHAT MODE: ROLEPLAY. Speak as the character living in Azeroth. "
            "Stay in character and avoid game-system or real-world talk."
        )

    channel_note = {
        'general': (
            "Use ordinary zone chat: questions, help, progress, opinions, "
            "complaints, or loose banter."
        ),
        'guild': (
            "Use familiar guild chat between players who may be in "
            "different zones."
        ),
        'battleground': (
            "Use concise battleground team chat: tactical, reactive, and "
            "competitive without becoming hostile."
        ),
        'raid': (
            "Use concise raid chat: practical, reactive, and focused on the "
            "run when appropriate."
        ),
        'say': (
            "Use casual player /say near other characters and NPCs."
        ),
    }.get(
        channel,
        "Use casual party chat between people playing together.",
    )
    return (
        "CHAT MODE: NORMAL. You are a real person playing WoW, "
        "not a character living in Azeroth. "
        + (
            "Use casual player /say near other characters and NPCs. "
            if channel == 'say'
            else f"Chat naturally in {channel} as yourself. "
        )
        + "Write like an actual player typing in WoW chat, "
        "typed off the cuff. Use the simplest natural way to say things. "
        "Short reactions and fragments are fine. Your personality can "
        "make you terse, sarcastic, blunt, helpful, or chatty. "
        "Use WoW terminology when it fits; perfect grammar is not required. "
        "You control a game character and do not physically feel its "
        "wounds or surroundings. Treat in-world personality labels as "
        "legacy character metadata, not your real personality. "
        "Respond to actual events without inventing new ones. "
        "Avoid slurs and personal abuse."
    )


def build_npc_chat_guidance() -> str:
    """Return the mode-invariant voice contract for actual NPCs."""
    return (
        "SPEAKER TYPE: NPC. Speak as an inhabitant of Azeroth. Stay grounded "
        "and lore-friendly; never mention players, screens, UI, game systems, "
        "or the real world."
    )
