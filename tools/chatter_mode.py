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
        "character in World of Warcraft."
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
        "CHAT MODE: NORMAL. Speak as a person playing WoW, not as an "
        "inhabitant of Azeroth. Race, class, level, gear, deaths, travel, "
        "weather, and locations describe the character or game; never claim "
        "to physically feel the game world's wounds, armor, weather, hunger, "
        "smells, or fatigue. "
        "If any other prompt data contains mystical, devotional, heroic, "
        "racial, or in-world personality and tone labels, treat it as legacy "
        "character metadata and do not express it. "
        f"{channel_note} Write like an actual player typing in WoW chat. "
        "Most messages should feel typed off the cuff, not written for an "
        "audience. Prefer the simplest natural way a player would say something. "
        "Do not turn a small observation into a polished thought, reflection, "
        "speech, joke setup, or miniature story. Short reactions and fragments "
        "are complete responses when that is all the situation calls for. "
        "Players do not need to explain every opinion or add another sentence "
        "just to elaborate. "
        "Do not make every message polished, complete, friendly, or carefully "
        "worded. Let the assigned personality matter: players can be terse, "
        "chatty, dry, sarcastic, helpful, blunt, skeptical, playful, annoyed, "
        "competitive, or laid-back. Use contractions, fragments, shorthand, "
        "WoW terminology, and common chat abbreviations when they fit the "
        "speaker and situation. Do not deliberately cram slang into every "
        "message or imitate a stereotype of internet speech. Minor informality "
        "is normal; perfect grammar is not required. Do not sound like an "
        "assistant, customer-service agent, narrator, or motivational coach. "
        "Avoid slurs and personal abuse."
    )


def build_npc_chat_guidance() -> str:
    """Return the mode-invariant voice contract for actual NPCs."""
    return (
        "SPEAKER TYPE: NPC. Speak as an inhabitant of Azeroth. Stay grounded "
        "and lore-friendly; never mention players, screens, UI, game systems, "
        "or the real world."
    )
