"""
Chatter Prompts - Prompt builders and creative selection for LLM Chatter Bridge.

Imports from chatter_constants and chatter_shared.
"""

import collections
import logging
import random
from datetime import datetime
from typing import List, Tuple

from chatter_constants import (
    TONES, MOODS, CREATIVE_TWISTS, MESSAGE_CATEGORIES,
    GOSSIP_CREATIVE_TWISTS,
    LENGTH_HINTS,
    RP_TONES, RP_MOODS, RP_CREATIVE_TWISTS,
    RP_GOSSIP_CREATIVE_TWISTS,
    RP_MESSAGE_CATEGORIES, RP_LENGTH_HINTS,
    PERSONALITY_SPICES, RP_PERSONALITY_SPICES,
    CLASS_NAMES, RACE_NAMES, CLASS_ROLE_MAP,
)
from chatter_shared import (
    get_chatter_mode, build_race_class_context,
    build_race_class_context_parts,
    build_bot_identity,
    build_bot_identity_with_level,
    build_bot_state_context,
    get_zone_flavor, format_price,
    build_anti_repetition_context,
    append_json_instruction,
    append_conversation_json_instruction,
    select_conversation_message_count,
    get_subzone_name, get_subzone_lore,
)

logger = logging.getLogger(__name__)

# Recency buffer so the same spice doesn't repeat
# across consecutive calls.
_recent_spices = collections.deque(maxlen=30)


# =============================================================================
# PERSONALITY SPICE PICKER
# =============================================================================
def pick_personality_spices(
    config=None, mode='normal',
    spice_count_override=None,
):
    """Pick N random personality spices, avoiding
    recent repeats.

    Args:
        config: config dict (reads PersonalitySpiceCount)
        mode: 'normal' or 'roleplay'
        spice_count_override: explicit count (0-5),
            takes priority over config
    Returns:
        list of spice strings (may be empty)
    """
    # Determine count
    if spice_count_override is not None:
        count = spice_count_override
    elif config is not None:
        try:
            count = int(config.get(
                'LLMChatter.PersonalitySpiceCount', 2
            ))
        except Exception:
            count = 2
    else:
        count = 2
    count = max(0, min(count, 5))
    if count == 0:
        return []

    pool = (
        RP_PERSONALITY_SPICES
        if mode == 'roleplay'
        else PERSONALITY_SPICES
    )

    # Filter out recently used spices
    recent_set = set(_recent_spices)
    available = [s for s in pool if s not in recent_set]

    # If not enough available, clear recency buffer
    if len(available) < count:
        _recent_spices.clear()
        available = list(pool)

    picked = random.sample(
        available, min(count, len(available))
    )
    _recent_spices.extend(picked)
    return picked


# =============================================================================
# CREATIVE SELECTION FUNCTIONS
# =============================================================================
def pick_random_tone(mode: str = 'normal') -> str:
    """Pick a random tone for the message."""
    pool = RP_TONES if mode == 'roleplay' else TONES
    return random.choice(pool)


def pick_random_mood(mode: str = 'normal') -> str:
    """Pick a random mood/emotional angle for the message."""
    pool = RP_MOODS if mode == 'roleplay' else MOODS
    return random.choice(pool)


def maybe_get_creative_twist(
    chance: float = 0.3, mode: str = 'normal'
) -> str:
    """Maybe return a creative twist (30% chance by default)."""
    if random.random() < chance:
        pool = (
            RP_CREATIVE_TWISTS if mode == 'roleplay'
            else CREATIVE_TWISTS
        )
        return random.choice(pool)
    return None


def maybe_get_gossip_creative_twist(
    chance: float = 0.3, mode: str = 'normal'
) -> str:
    """Maybe return a gossip-specific creative twist."""
    if random.random() < chance:
        pool = (
            RP_GOSSIP_CREATIVE_TWISTS
            if mode == 'roleplay'
            else GOSSIP_CREATIVE_TWISTS
        )
        return random.choice(pool)
    return None


def pick_random_message_category(mode: str = 'normal') -> str:
    """Pick a random message category."""
    pool = (
        RP_MESSAGE_CATEGORIES if mode == 'roleplay'
        else MESSAGE_CATEGORIES
    )
    return random.choice(pool)


def generate_conversation_mood_sequence(
    message_count: int, mode: str = 'normal'
) -> List[str]:
    """Generate a mood sequence for a conversation."""
    pool = RP_MOODS if mode == 'roleplay' else MOODS
    return [random.choice(pool) for _ in range(message_count)]


# Conversation length labels â€” short descriptions
# mapped to rough character counts for the LLM.
CONV_LENGTHS = [
    "very short (under 40 chars)",
    "short (40-70 chars)",
    "medium (70-120 chars)",
    "longer (120-150 chars max)",
]
# Weights favour shorter messages; keeps
# conversations snappy and readable.
CONV_LENGTH_WEIGHTS = [35, 35, 25, 5]


def generate_conversation_length_sequence(
    message_count: int,
) -> List[str]:
    """Generate per-message length targets so
    conversations have varied message lengths
    instead of uniform output."""
    return random.choices(
        CONV_LENGTHS,
        weights=CONV_LENGTH_WEIGHTS,
        k=message_count,
    )


# =============================================================================
# ENVIRONMENTAL CONTEXT
# =============================================================================
def get_time_of_day_context() -> Tuple[str, str]:
    """Get current time-of-day context for immersive conversations."""
    hour = datetime.now().hour

    if 5 <= hour < 7:
        return (
            "dawn",
            "The early morning light is just appearing",
        )
    elif 7 <= hour < 9:
        return ("early_morning", "It's early morning")
    elif 9 <= hour < 12:
        return ("morning", "The morning sun is up")
    elif 12 <= hour < 14:
        return ("midday", "It's around midday")
    elif 14 <= hour < 17:
        return ("afternoon", "It's afternoon")
    elif 17 <= hour < 19:
        return ("evening", "Evening is approaching")
    elif 19 <= hour < 21:
        return ("dusk", "The sun is setting")
    elif 21 <= hour < 23:
        return ("night", "Night has fallen")
    elif hour == 23 or hour == 0:
        return ("midnight", "It's late at night")
    else:  # 1-4
        return (
            "late_night",
            "It's the deep hours of night",
        )


def get_season_context() -> Tuple[str, str]:
    """Get current season using AzerothCore weather season logic."""
    day_of_year = datetime.now().timetuple().tm_yday - 1
    season_index = ((day_of_year - 78 + 365) // 91) % 4
    seasons = [
        ("spring", "Spring is in the air"),
        ("summer", "It is summer"),
        ("fall", "It is fall"),
        ("winter", "Winter has settled in"),
    ]
    return seasons[season_index]


def get_environmental_context(
    current_weather: str = None,
    season_chance: float = 0.30,
) -> dict:
    """Get environmental context for prompts.

    Time is always included. Season and weather are
    included opportunistically to avoid repetitive
    prompt patterns.
    """
    _, time_desc = get_time_of_day_context()
    result = {
        'time': time_desc,
        'season': None,
        'weather': None,
    }

    if current_weather and random.random() < 0.50:
        result['weather'] = current_weather

    if random.random() < season_chance:
        _, season_desc = get_season_context()
        result['season'] = season_desc

    return result


def build_environmental_context_lines(
    current_weather: str = None
) -> List[str]:
    """Build prompt lines for shared environment context."""
    env_context = get_environmental_context(current_weather)
    lines = []
    if env_context['time']:
        lines.append(f"Time of day: {env_context['time']}")
    if env_context['season']:
        lines.append(f"Season: {env_context['season']}")
    if env_context['weather']:
        lines.append(
            f"Current weather: {env_context['weather']}"
        )
    return lines


def append_environmental_context(
    parts: list, current_weather: str = None
) -> None:
    """Append shared environment context to prompt parts."""
    parts.extend(build_environmental_context_lines(current_weather))


# =============================================================================
# DYNAMIC GUIDELINES
# =============================================================================
def build_dynamic_guidelines(
    include_humor: bool = None,
    include_length: bool = True,
    config: dict = None,
    mode: str = 'normal',
    length_hint: str = "",
) -> list:
    """Build a randomized list of guidelines.

    length_hint: if provided, overrides the random
    length pool pick (caller pre-selected via RNG).
    """
    is_rp = (mode == 'roleplay')

    if is_rp:
        guidelines = [
            "Stay in character but keep it natural and "
            "conversational, not dramatic or theatrical",
            "ALWAYS write in first person - you ARE the "
            "character speaking. Your message should be "
            "SPOKEN words only (actions go in the "
            "\"action\" JSON field, not in the message).",
            "NEVER use brackets [] around names "
            "(quests, items, zones, creatures, NPCs, "
            "factions) - write everything as plain "
            "text. Only use {quest:Name}, "
            "{item:Name}, or {spell:Name} "
            "placeholders when explicitly told to.",
        ]
    else:
        guidelines = [
            "Simulate a human player who happens to be playing WoW, "
            "not a bot generating WoW-themed dialogue. Do not perform "
            "or demonstrate being a WoW player.",
            "You are the PLAYER controlling the character. Race, "
            "class, level, location, quests, gear, mobs, and current "
            "activity are background context, not required subjects "
            "for conversation.",
            "Before writing, use the human-interest test: would a real "
            "player actually bother pressing Enter to type this? There "
            "should be a plausible social reason such as asking, "
            "answering, warning, coordinating, inviting, reacting, "
            "complaining, joking, disagreeing, continuing a thread, "
            "or casually sharing a thought.",
            "Do not narrate routine gameplay. Avoid announcing normal "
            "kills, pulls, travel, gathering, quest progress, loot, "
            "movement, ability use, or other routine actions merely "
            "because that information appears in context.",
            "Game state is evidence, not a topic assignment. Never "
            "mention a zone, quest, mob, item, class, profession, "
            "dungeon, or activity merely to prove you noticed it.",
            "Conversation continuity outranks ambient game context. "
            "When a conversation is already happening, stay with that "
            "thread unless there is a natural reason to change it.",
            "Topic drift is normal. Real players talk about WoW but "
            "also other games, movies, TV, music, sports, technology, "
            "internet culture, food, work, school, addons, server "
            "gossip, plans, and random everyday things.",
            "Normal-mode players live in the present day. Contemporary "
            "real-world subjects and current entertainment are valid "
            "conversation topics when reliable fresh context about "
            "them has been supplied.",
            "For CURRENT real-world facts, use only fresh current-topic "
            "context supplied in the prompt. If no fresh context "
            "supports a recent release, announcement, score, event, "
            "trailer, news story, or date-sensitive claim, do not "
            "invent specifics. You may still express timeless or "
            "clearly subjective opinions.",
            "Mundane is realistic. A short answer, partial thought, "
            "one-word reply, correction, practical question, or "
            "unremarkable comment is often better than a clever line.",
            "Players do not need to agree. Casual disagreement, "
            "preferences, imperfect opinions, misunderstandings, and "
            "minor friction are normal. Do not make everyone friendly, "
            "helpful, impressed, or enthusiastic by default.",
            "Hard live WoW state must remain grounded, but ordinary "
            "player opinions and remembered game knowledge may be "
            "uncertain or imperfect. 'i think' or 'pretty sure' can "
            "sound more human than pretending to know everything.",
            "Keep chat casual and low-effort. Lowercase, missing "
            "punctuation, fragments, abbreviations, occasional typos, "
            "and one-word replies are all normal. Do not deliberately "
            "insert a typo into every message.",
            "Use WoW shorthand naturally when it fits: gz, grats, ty, "
            "np, mb, brb, afk, oom, lfg, inv, sec, omw, dps, tank, "
            "heals, pull, adds, trash, aggro, wipe, need, greed, roll. "
            "Do not force jargon when ordinary wording is more natural.",
            "Internet slang such as lol, lmao, tbh, ngl, bruh, and rip "
            "is available vocabulary, not a style requirement. Use it "
            "only when the meaning or conversational moment naturally "
            "calls for it. In particular, do not automatically append "
            "lol, lmao, or haha to an ordinary statement or question "
            "just to make it sound casual. Avoid repeating the same "
            "filler tic across nearby messages.",
            "Do not try to make every message witty, funny, dramatic, "
            "helpful, emotional, sarcastic, polished, or memorable.",
            "NEVER use brackets [] around names "
            "(quests, items, zones, creatures, NPCs, factions) - "
            "write everything as plain text. Only use {quest:Name}, "
            "{item:Name}, or {spell:Name} placeholders when "
            "explicitly told to.",
        ]

    length_pool = RP_LENGTH_HINTS if is_rp else LENGTH_HINTS
    if include_length:
        picked = length_hint or random.choice(
            length_pool
        )
        guidelines.append(
            f"Length: {picked}"
        )
        long_chance = 15 if is_rp else 12
        if config is not None:
            try:
                long_chance = int(
                    config.get(
                        'LLMChatter.LongMessageChance',
                        long_chance
                    )
                )
                if is_rp:
                    long_chance = min(long_chance + 5, 30)
            except Exception:
                pass
        if random.randint(1, 100) <= long_chance:
            guidelines.append(
                "Length mode: longer allowed "
                "(up to ~150 chars max) if it "
                "feels natural — one sentence"
            )
        else:
            guidelines.append(
                "Length mode: short/medium only "
                "(avoid long messages)"
            )
        guidelines.append(
            "HARD LIMIT: Never exceed 150 "
            "characters total"
        )

    if include_humor is None:
        include_humor = random.random() < (
            0.35 if is_rp else 0.15
        )
    if include_humor:
        if is_rp:
            guidelines.append(
                "A touch of wry or dry humor fits here"
            )
        else:
            guidelines.append(
                random.choice([
                    "A touch of humor fits here",
                    "Dry/sarcastic humor fits here",

                ])
            )

    if is_rp:
        extras = [
            "Let your race flavor your words subtly, "
            "not heavily",
            "Keep it simple - like a real person talking, "
            "just in-character",
            "A small detail about the surroundings is nice",
            "Casual and grounded, not poetic or flowery",
        ]
    else:
        extras = [
            "A very short or boring message is completely fine",
            "A practical question is more realistic than an observation",
            "Players sometimes type half a thought and leave it there",
            "Casual disagreement or a different opinion is fine",
            "The current game activity does not have to be the subject",
            "A natural subject change is fine when conversation leads there",
            "Brief and direct is usually better than polished",
        ]
    if random.random() < 0.5:
        guidelines.append(random.choice(extras))

    # Personality spices are useful for roleplay, but in normal
    # mode they tend to make ordinary player chat feel authored
    # or unnecessarily distinctive.
    if is_rp:
        spices = pick_personality_spices(
            config=config, mode=mode
        )
        if spices:
            guidelines.append(
                "Background feelings (not the main topic, "
                "just texture you can weave in naturally): "
                + "; ".join(spices)
            )

    return guidelines


# =============================================================================
# PROMPT BUILDERS
# =============================================================================
def build_plain_statement_prompt(
    bot: dict,
    zone_id: int = 0,
    zone_mobs: list = None,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    topic: str = None,
    area_id: int = 0,
    length_hint: str = "",
) -> str:
    """Build a dynamically varied prompt for a plain statement."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    parts = []

    if is_rp:
        identity = build_bot_identity(
            bot['name'],
            bot.get('race', ''),
            bot.get('class', ''),
            bot.get('gender', ''),
        )
        parts.append(
            f"{identity} "
            f"Speak in-character in General chat in "
            f"{bot['zone']}."
        )
        rp_ctx = build_race_class_context(
            bot.get('race', ''), bot.get('class', '')
        )
        if rp_ctx:
            parts.append(rp_ctx)
    else:
        parts.append(
            f"Generate one brief General chat message from "
            f"a human player currently playing WoW in "
            f"{bot['zone']}. They are a person using server "
            f"chat, not a character roleplaying and not a "
            f"narrator. The message may be about WoW, another "
            f"current interest, or ordinary life; their present "
            f"in-game activity does not need to be the subject."
        )

    if topic:
        parts.append(f"Topic: {topic}")

    if not is_rp:
        parts.append(
            "NORMAL MODE RULE: Location and subzone are factual "
            "context only, never conversation topics. Do not comment "
            "on scenery, weather, lighting, brightness, darkness, "
            "the sky, atmosphere, views, landscape, ambience, the "
            "'vibe' of the zone, or how the area looks. Do not turn "
            "being in a zone into an observation about that zone. "
            "A Tone, Mood, Message type, or Creative twist must NEVER "
            "override this rule. If a modifier suggests making an "
            "observation, make it about gameplay, another player, "
            "the UI, or ordinary real-life player behavior instead."
        )

    zone_flavor = get_zone_flavor(zone_id)
    if is_rp and zone_flavor:
        parts.append(f"Zone context: {zone_flavor}")

    # Subzone context (matches group idle pattern)
    sz_lore = get_subzone_lore(zone_id, area_id)
    if is_rp and sz_lore:
        parts.append(
            f"Current subzone: {sz_lore}"
        )
    else:
        sz_name = get_subzone_name(
            zone_id, area_id
        )
        if sz_name:
            parts.append(f"Subzone: {sz_name}")

    if is_rp:
        append_environmental_context(parts, current_weather)

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if not is_rp:
        bot_state = bot.get('bot_state')

        if isinstance(bot_state, dict):
            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    "AUTHORITATIVE LIVE PLAYERBOT STATE:"
                )
                parts.append(state_context)
                parts.append(
                    "STRICT FACT RULE: Personal factual claims "
                    "must be compatible with the live state above. "
                    "This includes level, progression, money, "
                    "inventory, bag space, equipment, professions "
                    "and profession skill values, quests, activity, "
                    "and travel. Casual wording such as 'just hit', "
                    "'finally got', or 'been working on' is fine "
                    "when plausible, but the underlying factual "
                    "claim must match the live state. If the state "
                    "does not establish a specific personal fact, "
                    "do not invent it."
                )

    if random.random() < 0.6:
        parts.append(f"Player level: {bot['level']}")

    if zone_mobs:
        parts.append(
            f"Creatures here: {', '.join(zone_mobs)}"
        )
        parts.append(
            "IMPORTANT: If mentioning any creature, ONLY use "
            "ones from the list above. Write creature names "
            "as plain text, never in brackets."
        )

    if is_rp:
        tone = pick_random_tone(mode)
        mood = pick_random_mood(mode)
        parts.append(f"Tone: {tone}")
        parts.append(f"Mood: {mood}")

        twist = maybe_get_creative_twist(mode=mode)
        if twist:
            parts.append(f"Creative twist: {twist}")
    else:
        # Most ordinary player chat does not need an assigned
        # performance. Occasionally add some flavor so Normal
        # mode still has personality and variation.
        if random.random() < 0.25:
            parts.append(f"Tone: {pick_random_tone(mode)}")

        if random.random() < 0.15:
            parts.append(f"Mood: {pick_random_mood(mode)}")

        twist = maybe_get_creative_twist(
            chance=0.08,
            mode=mode,
        )
        if twist:
            parts.append(f"Creative twist: {twist}")

    category = pick_random_message_category(mode)
    parts.append(f"Message type: {category}")

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode,
        length_hint=length_hint,
    )
    guidelines.append(
        "Plain text only - never wrap creature, NPC, "
        "zone, or faction names in brackets"
    )
    if is_rp:
        guidelines.append(
            "Stay in character but sound natural, "
            "not theatrical"
        )
        guidelines.append(
            "No game terms, abbreviations, "
            "or OOC references"
        )
    else:
        guidelines.append(
            "Speak like someone using a real server chat channel. "
            "WoW is what they are doing, not necessarily what they "
            "are talking about. Mention in-game context only when "
            "a real player would have a reason to bring it up."
        )
    if is_rp:
        guidelines.append(
            "Be original and avoid repeating recent phrasing"
        )
    else:
        guidelines.append(
            "Do not try to make the message interesting, poetic, "
            "clever, or memorable. Ordinary, mundane player chat "
            "is preferred."
        )
        guidelines.append(
            "Never make scenery, weather, lighting, brightness, "
            "the sky, atmosphere, landscape, zone appearance, or "
            "the area's vibe the subject of the message. Location "
            "names may be used only as practical gameplay context."
        )
    if zone_mobs:
        guidelines.append(
            "Only mention creatures from the provided list "
            "- do NOT invent creatures"
        )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_json_instruction(
        prompt, allow_action, skip_emote=True
    )


def build_quest_statement_prompt(
    bot: dict,
    quest: dict,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    zone_id: int = 0,
) -> str:
    """Build a dynamically varied prompt for a quest statement."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    parts = []

    if is_rp:
        identity = build_bot_identity(
            bot['name'],
            bot.get('race', ''),
            bot.get('class', ''),
            bot.get('gender', ''),
        )
        parts.append(
            f"{identity} Speak in-character about "
            f"a quest in {bot['zone']}."
        )
        rp_ctx = build_race_class_context(
            bot.get('race', ''), bot.get('class', '')
        )
        if rp_ctx:
            parts.append(rp_ctx)
    else:
        parts.append(
            "Generate a brief WoW General chat message "
            "mentioning a quest."
        )
        parts.append(f"Zone: {bot['zone']}")

    zone_flavor = get_zone_flavor(zone_id)
    if is_rp and zone_flavor:
        parts.append(f"Zone context: {zone_flavor}")

    if is_rp:
        append_environmental_context(parts, current_weather)

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if random.random() < 0.5:
        parts.append(f"Player level: {bot['level']}")

    # Support both legacy static quest data and the
    # authoritative quest structure from bot_state.
    quest_name = (
        quest.get('name')
        or quest.get('quest_name')
        or ''
    )
    quest_link_token = quest.get('link_token')
    quest_objectives = quest.get('objectives')

    quest_placeholder = (
        quest_link_token
        if quest_link_token
        else f"{{{{quest:{quest_name}}}}}"
    )

    parts.append(f"Quest: {quest_name}")
    parts.append(
        f"REQUIRED: Include exactly "
        f"{quest_placeholder} in the \"message\" "
        f"JSON field (NOT in the action). "
        f"This becomes a clickable link"
    )

    # Live PlayerBot quest state is authoritative.
    if isinstance(quest_objectives, list):
        parts.append(
            "AUTHORITATIVE LIVE QUEST STATE:"
        )

        if quest.get('status') is not None:
            parts.append(
                f"Quest status: {quest.get('status')}"
            )

        for objective in quest_objectives:
            if not isinstance(objective, dict):
                continue

            obj_type = objective.get('type', 'objective')
            obj_name = (
                objective.get('name')
                or objective.get('text')
                or 'objective'
            )
            current = objective.get('current')
            required = objective.get('required')
            complete = objective.get('complete')

            if current is not None and required is not None:
                parts.append(
                    f"- {obj_type}: {obj_name}: "
                    f"{current}/{required}; "
                    f"complete={bool(complete)}"
                )
            else:
                parts.append(
                    f"- {obj_type}: {obj_name}; "
                    f"complete={bool(complete)}"
                )

        parts.append(
            "The live quest state above is factual, but General chat "
            "is not a quest-progress diary. Do not announce exact "
            "objective counts or how many remain merely to narrate "
            "progress. Mention exact progress only when it is directly "
            "useful to a help request, grouping, coordination, or a "
            "practical question. Do not invent any quest fact that is "
            "not shown."
        )

    elif quest.get('description') and random.random() < 0.4:
        parts.append(
            f"Quest involves: {quest['description'][:80]}"
        )

    if is_rp:
        tone = pick_random_tone(mode)
        mood = pick_random_mood(mode)
        parts.append(f"Tone: {tone}")
        parts.append(f"Mood: {mood}")

        twist = maybe_get_creative_twist(mode=mode)
        if twist:
            parts.append(f"Creative twist: {twist}")
    else:
        # Normal mode should usually sound like ordinary player chat,
        # not a deliberately performed character response.
        if random.random() < 0.25:
            parts.append(f"Tone: {pick_random_tone(mode)}")

        if random.random() < 0.15:
            parts.append(f"Mood: {pick_random_mood(mode)}")

        twist = maybe_get_creative_twist(
            chance=0.08,
            mode=mode,
        )
        if twist:
            parts.append(f"Creative twist: {twist}")

    if is_rp:
        quest_actions = [
            "seeking guidance on the task",
            "reflecting on the quest's meaning",
            "warning of the dangers involved",
            "rallying companions for the undertaking",
            "musing on the reward awaiting",
        ]
    else:
        quest_actions = [
            "asking if anyone else is doing it",
            "asking for help with it",
            "looking for group for it",
            "asking a practical question about it",
            "asking whether anyone else had trouble with it",
            "offering to group with others doing it",
        ]
    if random.random() < 0.6:
        parts.append(
            f"Approach: {random.choice(quest_actions)}"
        )

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append(
        "STRICT: Keep under 80 characters "
        "(the link counts as ~15 chars)"
    )
    if is_rp:
        guidelines.append(
            "Stay in character but sound natural, "
            "not theatrical"
        )
    if is_rp:
        guidelines.append("Be creative without becoming theatrical")
    else:
        guidelines.append(
            "Keep it ordinary and practical. Do not embellish the "
            "quest or turn it into a story."
        )
        guidelines.append(
            "Do not invent quest progress, objective status, spawn "
            "behavior, drop rates, locations, difficulty, rewards, "
            "or anything that just happened. If those facts were not "
            "provided, keep the comment general."
        )
        guidelines.append(
            "Any complaint must logically match the facts provided. "
            "Do not invent a specific reason the quest is annoying."
        )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_json_instruction(
        prompt, allow_action, skip_emote=True
    )


def build_loot_statement_prompt(
    bot: dict,
    item: dict,
    can_use: bool,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    zone_id: int = 0,
) -> str:
    """Build a dynamically varied prompt for a loot statement."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    quality_names = {
        0: "gray", 1: "white", 2: "green",
        3: "blue", 4: "purple",
    }
    quality = quality_names.get(
        item.get('item_quality', 2), "green"
    )

    parts = []
    item_placeholder = f"{{{{item:{item['item_name']}}}}}"

    if is_rp:
        identity = build_bot_identity(
            bot['name'],
            bot.get('race', ''),
            bot.get('class', ''),
            bot.get('gender', ''),
        )
        parts.append(
            f"{identity} Speak in-character about "
            f"finding loot."
        )
        rp_ctx = build_race_class_context(
            bot.get('race', ''), bot.get('class', '')
        )
        if rp_ctx:
            parts.append(rp_ctx)
    else:
        parts.append(
            "Generate a brief WoW General chat message "
            "about a loot drop."
        )

    zone_flavor = get_zone_flavor(zone_id)
    if is_rp and zone_flavor:
        parts.append(f"Zone context: {zone_flavor}")

    if is_rp:
        append_environmental_context(parts, current_weather)
    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if not is_rp:
        bot_state = bot.get('bot_state')

        if isinstance(bot_state, dict):
            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    "AUTHORITATIVE LIVE PLAYERBOT STATE:"
                )
                parts.append(state_context)
                parts.append(
                    "STRICT FACT RULE: Personal factual claims "
                    "about inventory, equipment, bag space, "
                    "money, professions, quests, progression, "
                    "activity, or travel must be compatible "
                    "with the live state above. The loot event "
                    "itself establishes that this item just "
                    "dropped; do not invent other possessions "
                    "or character-state facts."
                )

    parts.append(
        f"Item: {item['item_name']} ({quality} quality)"
    )
    parts.append(
        f"REQUIRED: Include exactly "
        f"{item_placeholder} in the \"message\" "
        f"JSON field (this becomes a clickable "
        f"link). NEVER put it in the action field"
    )

    if random.random() < 0.6:
        parts.append(f"Player class: {bot['class']}")
        if random.random() < 0.4:
            usability = (
                "can equip"
                if can_use
                else "cannot equip (wrong class)"
            )
            parts.append(f"Class fit: {usability}")

    if is_rp:
        tone = pick_random_tone(mode)
        mood = pick_random_mood(mode)
        parts.append(f"Tone: {tone}")
        parts.append(f"Mood: {mood}")

        twist = maybe_get_creative_twist(mode=mode)
        if twist:
            parts.append(f"Creative twist: {twist}")
    else:
        # Normal mode should usually sound like ordinary player chat,
        # not a deliberately performed character response.
        if random.random() < 0.25:
            parts.append(f"Tone: {pick_random_tone(mode)}")

        if random.random() < 0.15:
            parts.append(f"Mood: {pick_random_mood(mode)}")

        twist = maybe_get_creative_twist(
            chance=0.08,
            mode=mode,
        )
        if twist:
            parts.append(f"Creative twist: {twist}")

    if is_rp:
        reactions = [
            "impressed by the quality of the item",
            "wondering if the item suits your path",
            "offering it to anyone who could use it",
            "commenting on your luck today",
            "mentioning what you think of the item",
        ]
    else:
        reactions = [
            "excitement about the drop",
            "meh, vendor fodder",
            "offering to trade/give away",
            "commenting on luck",
            "just mentioning what dropped",
            "briefly comparing the item to what they use",
            "wondering about the item",
        ]
    parts.append(f"Reaction style: {random.choice(reactions)}")

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append(
        "STRICT: Keep under 80 characters "
        "(the link counts as ~15 chars)"
    )
    if is_rp:
        guidelines.append(
            "Stay in character but sound natural, "
            "not theatrical"
        )
    if is_rp:
        guidelines.append(
            "Be creative without becoming theatrical"
        )
    else:
        guidelines.append(
            "React like a player would actually react to loot. "
            "A short, boring, practical, excited, disappointed, "
            "or indifferent reaction is fine."
        )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_json_instruction(
        prompt, allow_action, skip_emote=True
    )


def build_quest_reward_statement_prompt(
    bot: dict,
    quest: dict,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    zone_id: int = 0,
) -> str:
    """Build a prompt for quest completion with reward."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')

    item_name = (
        quest.get('item1_name') or quest.get('item2_name')
    )
    item_quality = (
        quest.get('item1_quality')
        or quest.get('item2_quality')
        or 2
    )

    if not item_name:
        return build_quest_statement_prompt(
            bot, quest, config, current_weather,
            recent_messages=recent_messages,
            speaker_talent_context=(
                speaker_talent_context
            ),
            zone_id=zone_id,
        )

    quality_names = {
        0: "gray", 1: "white", 2: "green",
        3: "blue", 4: "purple",
    }
    quality = quality_names.get(item_quality, "green")

    parts = []

    if is_rp:
        identity = build_bot_identity(
            bot['name'],
            bot.get('race', ''),
            bot.get('class', ''),
            bot.get('gender', ''),
        )
        parts.append(
            f"{identity} Speak in-character about "
            f"completing a quest and its reward."
        )
        rp_ctx = build_race_class_context(
            bot.get('race', ''), bot.get('class', '')
        )
        if rp_ctx:
            parts.append(rp_ctx)
    else:
        parts.append(
            "Generate a brief WoW General chat message "
            "about finishing a quest."
        )

    zone_flavor = get_zone_flavor(zone_id)
    if is_rp and zone_flavor:
        parts.append(f"Zone context: {zone_flavor}")

    if is_rp:
        append_environmental_context(parts, current_weather)

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    parts.append(
        f"Quest: {quest['quest_name']} "
        f"(use {{{{quest:{quest['quest_name']}}}}} placeholder)"
    )
    parts.append(
        f"Reward: {item_name} ({quality}) "
        f"(use {{{{item:{item_name}}}}} placeholder)"
    )

    if random.random() < 0.5:
        parts.append(f"Player class: {bot['class']}")

    if is_rp:
        tone = pick_random_tone(mode)
        mood = pick_random_mood(mode)
        parts.append(f"Tone: {tone}")
        parts.append(f"Mood: {mood}")

        twist = maybe_get_creative_twist(mode=mode)
        if twist:
            parts.append(f"Creative twist: {twist}")
    else:
        # Normal mode should usually sound like ordinary player chat,
        # not a deliberately performed character response.
        if random.random() < 0.25:
            parts.append(f"Tone: {pick_random_tone(mode)}")

        if random.random() < 0.15:
            parts.append(f"Mood: {pick_random_mood(mode)}")

        twist = maybe_get_creative_twist(
            chance=0.08,
            mode=mode,
        )
        if twist:
            parts.append(f"Creative twist: {twist}")

    if is_rp:
        reactions = [
            "feeling satisfied about finishing",
            "commenting on the reward you received",
            "mentioning the journey it took",
            "thanking those who helped along the way",
        ]
    else:
        reactions = [
            "relief at finishing",
            "excitement about reward",
            "meh about the reward",
            "just noting completion",
            "sharing the achievement",
        ]
    if random.random() < 0.5:
        parts.append(
            f"Reaction: {random.choice(reactions)}"
        )

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append(
        "Use BOTH placeholders, each once, "
        "in the \"message\" field only "
        "(NOT in the action)"
    )
    guidelines.append(
        "STRICT: Keep under 80 characters "
        "(the link counts as ~15 chars)"
    )
    if is_rp:
        guidelines.append(
            "Stay in character but sound natural, "
            "not theatrical"
        )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_json_instruction(
        prompt, allow_action, skip_emote=True
    )


def build_plain_conversation_prompt(
    bots: List[dict],
    zone_id: int = 0,
    zone_mobs: list = None,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    topic: str = None,
    area_id: int = 0,
) -> str:
    """Build a prompt for a plain conversation with 2-4 bots."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    parts = []
    bot_count = len(bots)
    bot_names = [b['name'] for b in bots]

    if is_rp:
        parts.append(
            f"Generate an in-character General chat exchange "
            f"between {bot_count} adventurers in "
            f"{bots[0]['zone']}. Each speaks as their "
            f"race and class."
        )
    elif bot_count == 2:
        parts.append(
            f"Generate a casual General chat exchange between "
            f"two human players currently playing WoW in "
            f"{bots[0]['zone']}. They are people using a public "
            f"server chat channel, not characters roleplaying. "
            f"The conversation may be about WoW, another game, "
            f"entertainment, current interests, everyday life, "
            f"or whatever naturally gives one player a reason "
            f"to say something to the other."
        )
    else:
        parts.append(
            f"Generate a casual General chat exchange between "
            f"{bot_count} human players currently playing WoW in "
            f"{bots[0]['zone']}. They are people using a public "
            f"server chat channel, not characters roleplaying. "
            f"The conversation may be about WoW, another game, "
            f"entertainment, current interests, everyday life, "
            f"or whatever naturally gives these players a reason "
            f"to talk to one another."
        )

    if topic:
        parts.append(f"Topic: {topic}")
    if not is_rp:
        parts.append(
            "Location is factual context only, not a topic. "
            "Do not comment on scenery, weather, lighting, "
            "the sky, atmosphere, views, or how the zone "
            "looks unless the chosen topic specifically "
            "requires it."
        )

    zone_flavor = get_zone_flavor(zone_id)
    if is_rp and zone_flavor:
        parts.append(f"Zone context: {zone_flavor}")

    # Subzone context (matches group idle pattern)
    sz_lore = get_subzone_lore(zone_id, area_id)
    if is_rp and sz_lore:
        parts.append(
            f"Current subzone: {sz_lore}"
        )
    else:
        sz_name = get_subzone_name(
            zone_id, area_id
        )
        if sz_name:
            parts.append(f"Subzone: {sz_name}")

    if is_rp:
        append_environmental_context(parts, current_weather)

    parts.append(f"Speakers: {', '.join(bot_names)}")
    parts.append(
        "Names: Sometimes use their name when addressing "
        "directly (maybe 1-2 times in a conversation), but "
        "not every message - vary it naturally."
    )

    # Precompute shared race context once per unique race.
    # Pass race_count so lore uses cumulative probability
    # 1-(1-p)^n, preserving pre-dedup lore frequency.
    shared_race_cache = {}
    if is_rp:
        race_counts = {}
        for bot in bots:
            r = bot.get('race', '')
            if r:
                race_counts[r] = race_counts.get(r, 0) + 1
        for race, count in race_counts.items():
            _, sr, _ = build_race_class_context_parts(
                race, '', race_count=count
            )
            shared_race_cache[race] = sr

    seen_races = set()
    seen_classes = set()
    for bot in bots:
        if is_rp or random.random() < 0.4:
            race = bot.get('race', '')
            cls = bot.get('class', '')
            parts.append(
                f"{bot['name']} is a {race} {cls}"
            )
            if is_rp:
                per_bot, _, shared_class = (
                    build_race_class_context_parts(
                        race, cls
                    )
                )
                if per_bot:
                    parts.append(f"  {per_bot}")
                if race not in seen_races:
                    sr = shared_race_cache.get(
                        race, ''
                    )
                    if sr:
                        parts.append(
                            f"  {sr}"
                        )
                    seen_races.add(race)
                if cls not in seen_classes:
                    if shared_class:
                        parts.append(
                            f"  {shared_class}"
                        )
                    seen_classes.add(cls)

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if not is_rp:
        parts.append(
            "AUTHORITATIVE LIVE PLAYERBOT STATES:"
        )

        for bot in bots:
            bot_state = bot.get('bot_state')

            if not isinstance(bot_state, dict):
                continue

            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    f"STATE FOR {bot['name']} ONLY:"
                )
                parts.append(state_context)

        parts.append(
            "STRICT SPEAKER FACT RULE: Each speaker may "
            "only make personal factual claims compatible "
            "with that speaker's own live state above. "
            "This includes level, progression, money, "
            "inventory, bag space, equipment, professions "
            "and profession skill values, quests, activity, "
            "and travel. Casual wording such as 'just hit', "
            "'finally got', or 'been working on' is fine "
            "when plausible, but the underlying factual "
            "claim must match that speaker's live state."
        )
        parts.append(
            "Never transfer facts from one bot's state "
            "to another bot. If a speaker's state does "
            "not establish a specific personal fact, "
            "do not invent it for that speaker."
        )

    if zone_mobs:
        parts.append(
            f"Creatures here: {', '.join(zone_mobs)}"
        )
        parts.append(
            "IMPORTANT: If mentioning any creature, ONLY use "
            "ones from the list above. Write creature names "
            "as plain text, never in brackets."
        )

    if is_rp:
        tone = pick_random_tone(mode)
        parts.append(f"Overall tone: {tone}")
    elif random.random() < 0.20:
        parts.append(
            f"Loose conversational flavor: "
            f"{pick_random_tone(mode)}"
        )

    twist = maybe_get_creative_twist(
        chance=0.4 if is_rp else 0.08,
        mode=mode,
    )
    if twist:
        parts.append(
            f"Creative twist for this conversation: {twist}"
        )

    if is_rp:
        min_msgs = bot_count
        max_msgs = bot_count + 3
    else:
        min_msgs = 1
        max_msgs = min(4, bot_count + 2)

    msg_count = select_conversation_message_count(
        bot_count, min_msgs, max_msgs
    )
    if is_rp:
        mood_sequence = generate_conversation_mood_sequence(
            msg_count, mode
        )
        length_sequence = generate_conversation_length_sequence(
            msg_count
        )

        parts.append(
            "\nMOOD AND LENGTH SEQUENCE "
            "(follow this for each message):"
        )
        for i, mood in enumerate(mood_sequence):
            speaker = bot_names[i % bot_count]
            parts.append(
                f"  Message {i+1} ({speaker}): "
                f"mood={mood}, "
                f"length={length_sequence[i]}"
            )

    if is_rp:
        topics = [
            "discussing the dangers of these lands",
            "sharing tales of past battles",
            "debating the best path forward",
            "exchanging news from distant regions",
            "reflecting on the state of the war",
        ]
    else:
        topics = [
            "a practical question one player actually wants answered",
            "a useful warning, tip, recommendation, or correction",
            "looking for a group or asking whether anyone wants to join something",
            "a casual opinion or mild disagreement about WoW",
            "server, guild, addon, class, dungeon, or player-culture talk",
            "another game, movie, show, sport, technology, or current interest",
            "food, work, school, weekend plans, or ordinary everyday life",
            "a dumb joke, small complaint, misunderstanding, or random argument",
            "mundane small talk with no need to be interesting or memorable",
        ]
    if random.random() < 0.5:
        parts.append(
            f"Topic hint: {random.choice(topics)}"
        )

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append(
        "Plain text only - never wrap creature, NPC, "
        "zone, or faction names in brackets"
    )
    if is_rp:
        guidelines.append(
            "Follow the mood and length sequence above"
        )

        if bot_count > 2:
            guidelines.append(
                f"EVERY speaker MUST have at least one "
                f"message — do NOT skip any participant"
            )

        guidelines.append(
            "Each speaker stays in character for their "
            "race and class"
        )
        guidelines.append(
            "No game terms, abbreviations, or OOC references"
        )
        guidelines.append(
            "VARY message lengths naturally - some brief, "
            "some more expressive"
        )
    else:
        guidelines.append(
            "VARY message lengths naturally "
            "- some short, some medium, some longer"
        )
    if zone_mobs:
        guidelines.append(
            "Only mention creatures from the provided list "
            "- do NOT invent creatures"
        )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_conversation_json_instruction(
        prompt,
        bot_names,
        msg_count,
        allow_action,
        require_all_speakers=is_rp,
    )

def _format_gossip_target(target: dict, target_type: str) -> str:
    """Build a compact target description for gossip prompts."""
    if target_type == 'npc':
        details = [
            f"name={target.get('name', 'Unknown NPC')}",
            f"function={target.get('function', 'local NPC')}",
        ]
        if target.get('subname'):
            details.append(f"title={target['subname']}")
        if target.get('kind'):
            details.append(f"kind={target['kind']}")
        if target.get('combat_class'):
            details.append(
                f"combat style={target['combat_class']}"
            )
        min_level = int(target.get('minlevel') or 0)
        max_level = int(target.get('maxlevel') or 0)
        if min_level or max_level:
            if min_level == max_level:
                details.append(f"level={min_level}")
            else:
                details.append(
                    f"level range={min_level}-{max_level}"
                )
        return "; ".join(details)

    return "; ".join([
        f"name={target.get('name', 'Unknown bot')}",
        f"race={target.get('race', 'Unknown')}",
        f"class={target.get('class', 'Adventurer')}",
        f"level={target.get('level', '?')}",
    ])


def _append_gossip_context(
    parts: list,
    zone_id: int,
    area_id: int,
    current_weather: str,
    target: dict,
    target_type: str,
    is_rp: bool,
) -> None:
    """Append shared local and target context for gossip prompts."""
    zone_flavor = get_zone_flavor(zone_id)
    if is_rp and zone_flavor:
        parts.append(f"Zone context: {zone_flavor}")

    subzone_lore = get_subzone_lore(zone_id, area_id)
    if is_rp and subzone_lore:
        parts.append(f"Current subzone: {subzone_lore}")
    else:
        subzone_name = get_subzone_name(zone_id, area_id)
        if subzone_name:
            parts.append(f"Subzone: {subzone_name}")

    if is_rp:
        append_environmental_context(parts, current_weather)

    label = 'NPC' if target_type == 'npc' else 'bot'
    parts.append(
        f"Gossip subject ({label}): "
        f"{_format_gossip_target(target, target_type)}"
    )
    parts.append(
        "The subject is someone people could plausibly talk "
        "about, not someone being addressed directly."
    )


def build_gossip_statement_prompt(
    bot: dict,
    target: dict,
    target_type: str,
    zone_id: int,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    area_id: int = 0,
    length_hint: str = "",
) -> str:
    """Build a General-channel gossip statement prompt."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    parts = []

    if is_rp:
        identity = build_bot_identity(
            bot['name'], bot.get('race', ''),
            bot.get('class', ''), bot.get('gender', ''),
        )
        parts.append(
            f"{identity} Speak in-character in General "
            f"chat in {bot['zone']}."
        )
        rp_ctx = build_race_class_context(
            bot.get('race', ''), bot.get('class', '')
        )
        if rp_ctx:
            parts.append(rp_ctx)
    else:
        parts.append(
            f"Generate a brief WoW General chat message "
            f"from a player in {bot['zone']}."
        )

    _append_gossip_context(
        parts, zone_id, area_id, current_weather,
        target, target_type, is_rp,
    )

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if not is_rp:
        bot_state = bot.get('bot_state')

        if isinstance(bot_state, dict):
            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    "AUTHORITATIVE LIVE PLAYERBOT STATE:"
                )
                parts.append(state_context)
                parts.append(
                    "STRICT FACT RULE: The gossip context "
                    "establishes facts about the gossip subject. "
                    "Personal factual claims about yourself must "
                    "be compatible with your own live state above. "
                    "Do not invent your inventory, equipment, "
                    "money, professions, quest progress, level, "
                    "activity, or travel state."
                )

    if is_rp:
        tone = pick_random_tone(mode)
        mood = pick_random_mood(mode)
        parts.append(f"Tone: {tone}")
        parts.append(f"Mood: {mood}")

        twist = maybe_get_gossip_creative_twist(mode=mode)
        if twist:
            parts.append(f"Creative twist: {twist}")
    else:
        if random.random() < 0.25:
            parts.append(f"Tone: {pick_random_tone(mode)}")

        if random.random() < 0.15:
            parts.append(f"Mood: {pick_random_mood(mode)}")

        twist = maybe_get_gossip_creative_twist(
            chance=0.08,
            mode=mode,
        )
        if twist:
            parts.append(f"Creative twist: {twist}")

    target_label = 'NPC' if target_type == 'npc' else 'bot'
    guidelines = build_dynamic_guidelines(
        config=config, mode=mode,
        length_hint=length_hint,
    )
    guidelines.extend([
        f"Gossip about the {target_label}; do not speak "
        f"as the {target_label}",
        "Do not address the gossip subject directly",
        "Use the subject's exact name if it sounds natural",
        "No brackets around names",
    ])
    if is_rp:
        guidelines.append(
            "Stay in character but sound natural, not theatrical"
        )
    else:
        guidelines.append(
            "Speak as a player discussing the game, not as "
            "the character roleplaying"
        )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(recent_messages)
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_json_instruction(
        prompt, allow_action, skip_emote=True
    )


def build_gossip_conversation_prompt(
    bots: List[dict],
    target: dict,
    target_type: str,
    zone_id: int,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    area_id: int = 0,
) -> str:
    """Build a General-channel gossip conversation prompt."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    parts = []
    bot_count = len(bots)
    bot_names = [b['name'] for b in bots]

    if is_rp:
        parts.append(
            f"Generate an in-character General chat exchange "
            f"between {bot_count} adventurers in "
            f"{bots[0]['zone']}."
        )
    else:
        parts.append(
            f"Generate a casual General chat exchange between "
            f"{bot_count} WoW players in {bots[0]['zone']}."
        )

    _append_gossip_context(
        parts, zone_id, area_id, current_weather,
        target, target_type, is_rp,
    )

    parts.append(f"Speakers: {', '.join(bot_names)}")

    seen_races = set()
    seen_classes = set()
    for bot in bots:
        if is_rp or random.random() < 0.4:
            race = bot.get('race', '')
            cls = bot.get('class', '')
            parts.append(f"{bot['name']} is a {race} {cls}")
            if is_rp:
                per_bot, shared_race, shared_class = (
                    build_race_class_context_parts(race, cls)
                )
                if per_bot:
                    parts.append(f"  {per_bot}")
                if race not in seen_races and shared_race:
                    parts.append(f"  {shared_race}")
                    seen_races.add(race)
                if cls not in seen_classes and shared_class:
                    parts.append(f"  {shared_class}")
                    seen_classes.add(cls)

    if not is_rp:
        parts.append(
            "AUTHORITATIVE LIVE PLAYERBOT STATES:"
        )

        for bot in bots:
            bot_state = bot.get('bot_state')

            if not isinstance(bot_state, dict):
                continue

            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    f"STATE FOR {bot['name']} ONLY:"
                )
                parts.append(state_context)

        parts.append(
            "STRICT SPEAKER FACT RULE: The supplied gossip "
            "context establishes facts about the gossip subject. "
            "Each speaker's personal factual claims must be "
            "compatible with that speaker's own live state. "
            "Never transfer inventory, equipment, money, "
            "professions, quests, progression, activity, or "
            "travel facts between speakers. Do not invent "
            "personal facts that a speaker's state does not "
            "establish."
        )

    if is_rp:
        tone = pick_random_tone(mode)
        parts.append(f"Overall tone: {tone}")
    elif random.random() < 0.20:
        parts.append(
            f"Loose conversational flavor: "
            f"{pick_random_tone(mode)}"
        )

    twist = maybe_get_creative_twist(
        chance=0.4 if is_rp else 0.08,
        mode=mode,
    )
    if twist:
        parts.append(
            f"Creative twist for this conversation: {twist}"
        )

    if is_rp:
        min_msgs = bot_count
        max_msgs = bot_count + 3
    else:
        min_msgs = 1
        max_msgs = min(4, bot_count + 2)

    msg_count = select_conversation_message_count(
        bot_count, min_msgs, max_msgs
    )
    if is_rp:
        mood_sequence = generate_conversation_mood_sequence(
            msg_count, mode
        )
        length_sequence = generate_conversation_length_sequence(
            msg_count
        )

        parts.append(
            "\nMOOD AND LENGTH SEQUENCE "
            "(follow this for each message):"
        )
        for i, mood in enumerate(mood_sequence):
            speaker = bot_names[i % bot_count]
            parts.append(
                f"  Message {i+1} ({speaker}): "
                f"mood={mood}, "
                f"length={length_sequence[i]}"
            )

    target_label = 'NPC' if target_type == 'npc' else 'bot'
    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.extend([
        f"The conversation is gossip about the {target_label}",
        f"Do not make the {target_label} a speaker",
        "Do not address the gossip subject directly",
        "Use the subject's exact name if it sounds natural",
    ])

    if is_rp:
        guidelines.append(
            "Follow the mood and length sequence above"
        )

        if bot_count > 2:
            guidelines.append(
                f"EVERY speaker MUST have at least one "
                f"message — do NOT skip any participant"
            )

        guidelines.append(
            "Each speaker stays in character for their "
            "race and class"
        )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(recent_messages)
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_conversation_json_instruction(
        prompt, bot_names, msg_count, allow_action, require_all_speakers=is_rp,
    )


def build_quest_conversation_prompt(
    bots: List[dict],
    quest: dict,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    zone_id: int = 0,
) -> str:
    """Build a prompt for a quest conversation with 2-4 bots."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    parts = []
    bot_count = len(bots)
    bot_names = [b['name'] for b in bots]

    if is_rp:
        parts.append(
            f"Generate an in-character General chat exchange "
            f"about a quest in {bots[0]['zone']}. Each speaker "
            f"stays true to their race and class."
        )
    else:
        parts.append(
            f"Generate a casual General chat exchange about "
            f"a quest in {bots[0]['zone']}."
        )
    parts.append(f"Speakers: {', '.join(bot_names)}")
    parts.append(
        "Names: Sometimes use their name when addressing "
        "directly (maybe 1-2 times), but not every message."
    )

    if is_rp:
        for bot in bots:
            parts.append(
                f"{bot['name']} is a "
                f"{bot['race']} {bot['class']}"
            )

    zone_flavor = get_zone_flavor(zone_id)
    if is_rp and zone_flavor:
        parts.append(f"Zone context: {zone_flavor}")

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if not is_rp:
        parts.append(
            "AUTHORITATIVE LIVE PLAYERBOT STATES:"
        )

        for bot in bots:
            bot_state = bot.get('bot_state')
            if not isinstance(bot_state, dict):
                continue

            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    f"STATE FOR {bot['name']} ONLY:"
                )
                parts.append(state_context)

        parts.append(
            "STRICT SPEAKER FACT RULE: Each speaker may "
            "only make personal factual claims that are "
            "compatible with that speaker's own live state "
            "above. This includes level, progression, "
            "inventory, equipment, professions and profession "
            "skill values, quests, money, activity, and travel. "
            "Casual phrasing like 'just hit', 'finally got', "
            "or 'been working on' is allowed when it is "
            "plausible from the current state, but the actual "
            "underlying fact must match the live state."
        )
        parts.append(
            "Never transfer facts from one bot's state "
            "to another bot. If a speaker's state does "
            "not establish a fact, do not claim it as "
            "that speaker's fact."
        )

    if is_rp:
        append_environmental_context(parts, current_weather)

    # Support both legacy static quest data and the
    # authoritative quest structure from bot_state.
    quest_name = (
        quest.get('name')
        or quest.get('quest_name')
        or ''
    )
    quest_link_token = quest.get('link_token')
    quest_objectives = quest.get('objectives')

    quest_placeholder = (
        quest_link_token
        if quest_link_token
        else f"{{{{quest:{quest_name}}}}}"
    )

    parts.append(
        f"Quest: {quest_name} "
        f"(use {quest_placeholder} exactly)"
    )

    quest_owner_name = quest.get('_owner_name')

    if isinstance(quest_objectives, list):
        parts.append(
            "AUTHORITATIVE LIVE QUEST STATE:"
        )

        if quest_owner_name:
            parts.append(
                f"This quest state belongs ONLY to "
                f"{quest_owner_name}."
            )

        if quest.get('status') is not None:
            parts.append(
                f"{quest_owner_name or 'Quest owner'} "
                f"status: {quest.get('status')}"
            )

        for objective in quest_objectives:
            if not isinstance(objective, dict):
                continue

            obj_type = objective.get(
                'type', 'objective'
            )
            obj_name = (
                objective.get('name')
                or objective.get('text')
                or 'objective'
            )
            current = objective.get('current')
            required = objective.get('required')
            complete = objective.get('complete')

            if (
                current is not None
                and required is not None
            ):
                parts.append(
                    f"- {obj_type}: {obj_name}: "
                    f"{current}/{required}; "
                    f"complete={bool(complete)}"
                )
            else:
                parts.append(
                    f"- {obj_type}: {obj_name}; "
                    f"complete={bool(complete)}"
                )

        parts.append(
            "STRICT LIVE STATE RULES: The objective "
            "progress above belongs only to the named "
            "quest owner. Other speakers must NOT claim "
            "that progress as their own."
        )
        parts.append(
            "Do not invent objective locations, spawn "
            "behavior, drop rates, rewards, difficulty, "
            "completion, or events that just happened."
        )

    elif quest.get('description') and random.random() < 0.4:
        parts.append(
            f"Quest involves: "
            f"{quest['description'][:60]}"
        )

    if is_rp:
        tone = pick_random_tone(mode)
        parts.append(f"Overall tone: {tone}")
    elif random.random() < 0.20:
        parts.append(
            f"Loose conversational flavor: "
            f"{pick_random_tone(mode)}"
        )

    twist = maybe_get_creative_twist(
        chance=0.4 if is_rp else 0.08,
        mode=mode,
    )
    if twist:
        parts.append(
            f"Creative twist for this conversation: {twist}"
        )

    if is_rp:
        min_msgs = bot_count
        max_msgs = bot_count + 3
    else:
        min_msgs = 1
        max_msgs = min(4, bot_count + 2)

    msg_count = select_conversation_message_count(
        bot_count, min_msgs, max_msgs
    )
    if is_rp:
        mood_sequence = generate_conversation_mood_sequence(
            msg_count, mode
        )
        length_sequence = generate_conversation_length_sequence(
            msg_count
        )

        parts.append(
            "\nMOOD AND LENGTH SEQUENCE "
            "(follow this for each message):"
        )
        for i, mood in enumerate(mood_sequence):
            speaker = bot_names[i % bot_count]
            parts.append(
                f"  Message {i+1} ({speaker}): "
                f"mood={mood}, "
                f"length={length_sequence[i]}"
            )

    if is_rp:
        angles = [
            "seeking allies for a perilous task",
            "debating the best approach to the objective",
            "sharing knowledge of the quest's history",
            "steeling each other for the dangers ahead",
        ]
    else:
        angles = [
            "asking if anyone else is doing the quest",
            "asking for help with the quest",
            "asking a practical question about the quest",
            "asking whether anyone else had trouble with it",
            "offering to group with others doing the quest",
            "looking for group for the quest",
        ]
    if random.random() < 0.5:
        parts.append(
            f"Angle hint: {random.choice(angles)}"
        )

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append(
        f"Use {quest_placeholder} exactly at least once"
    )

    if not is_rp:
        guidelines.append(
            "Treat live bot_state facts as authoritative"
        )
        guidelines.append(
            "A speaker may only claim quest progress "
            "that belongs to that speaker"
        )
        guidelines.append(
            "Do not assume another speaker has the same "
            "quest or the same objective progress"
        )
        guidelines.append(
            "Do not invent quest facts that were not "
            "provided"
        )
        guidelines.append(
            "Other speakers may react, ask questions, "
            "offer generic help, ignore it, or give a "
            "short noncommittal response"
        )

    if is_rp:
        guidelines.append(
            "Follow the mood and length sequence above"
        )

        if bot_count > 2:
            guidelines.append(
                f"EVERY speaker MUST have at least one "
                f"message — do NOT skip any participant"
            )

        guidelines.append(
            "Each speaker stays in character for their "
            "race and class"
        )

    guidelines.append(
        "STRICT: Each message MUST be under 120 "
        "characters. Short is better"
    )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_conversation_json_instruction(
        prompt, bot_names, msg_count, allow_action, require_all_speakers=is_rp,
    )


def build_loot_conversation_prompt(
    bots: List[dict],
    item: dict,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    zone_id: int = 0,
) -> str:
    """Build a prompt for a loot conversation with 2-4 bots."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    parts = []
    bot_count = len(bots)
    bot_names = [b['name'] for b in bots]

    quality_names = {
        0: "gray", 1: "white", 2: "green",
        3: "blue", 4: "purple",
    }
    quality = quality_names.get(
        item.get('item_quality', 2), "green"
    )
    item_placeholder = f"{{{{item:{item['item_name']}}}}}"

    if is_rp:
        parts.append(
            f"Generate an in-character General chat exchange "
            f"about a loot find in {bots[0]['zone']}."
        )
    else:
        parts.append(
            f"Generate a casual General chat exchange about "
            f"a loot drop in {bots[0]['zone']}."
        )
    parts.append(f"Speakers: {', '.join(bot_names)}")
    parts.append(
        "Names: Sometimes use their name when addressing "
        "directly (maybe once), but not every message."
    )

    if is_rp:
        for bot in bots:
            parts.append(
                f"{bot['name']} is a "
                f"{bot['race']} {bot['class']}"
            )

    zone_flavor = get_zone_flavor(zone_id)
    if is_rp and zone_flavor:
        parts.append(f"Zone context: {zone_flavor}")

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if not is_rp:
        parts.append(
            "AUTHORITATIVE LIVE PLAYERBOT STATES:"
        )

        for bot in bots:
            bot_state = bot.get('bot_state')

            if not isinstance(bot_state, dict):
                continue

            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    f"STATE FOR {bot['name']} ONLY:"
                )
                parts.append(state_context)

        parts.append(
            "STRICT SPEAKER FACT RULE: Each speaker may "
            "only make personal factual claims compatible "
            "with that speaker's own live state. Do not "
            "transfer inventory, equipment, money, "
            "professions, quests, progression, activity, "
            "or travel facts between speakers. The loot "
            "event establishes that the displayed item "
            "dropped, but does not establish unrelated "
            "personal facts."
        )

    if is_rp:
        append_environmental_context(parts, current_weather)

    parts.append(
        f"Item: {item['item_name']} ({quality} quality)"
    )
    parts.append(
        f"REQUIRED: Use {item_placeholder} in the "
        f"\"message\" field (NOT in the action). "
        f"This becomes a clickable link"
    )

    if is_rp:
        tone = pick_random_tone(mode)
        parts.append(f"Overall tone: {tone}")
    elif random.random() < 0.20:
        parts.append(
            f"Loose conversational flavor: "
            f"{pick_random_tone(mode)}"
        )

    twist = maybe_get_creative_twist(
        chance=0.4 if is_rp else 0.08,
        mode=mode,
    )
    if twist:
        parts.append(
            f"Creative twist for this conversation: {twist}"
        )

    if is_rp:
        min_msgs = bot_count
        max_msgs = bot_count + 2
    else:
        min_msgs = 1
        max_msgs = min(4, bot_count + 2)

    msg_count = select_conversation_message_count(
        bot_count, min_msgs, max_msgs
    )
    if is_rp:
        mood_sequence = generate_conversation_mood_sequence(
            msg_count, mode
        )
        length_sequence = generate_conversation_length_sequence(
            msg_count
        )

        parts.append(
            "\nMOOD AND LENGTH SEQUENCE "
            "(follow this for each message):"
        )
        for i, mood in enumerate(mood_sequence):
            speaker = bot_names[i % bot_count]
            parts.append(
                f"  Message {i+1} ({speaker}): "
                f"mood={mood}, "
                f"length={length_sequence[i]}"
            )

    if is_rp:
        angles = [
            "one examines the find while others "
            "judge its worth",
            "debating who is most suited to wield it",
            "one offers the spoils to the group",
            "appraising the craftsmanship with "
            "lore knowledge",
        ]
    else:
        angles = [
            "one player got the drop and others are "
            "jealous/congratulating",
            "discussing if the item is good for "
            "their class",
            "debating whether to vendor or auction it",
            "one asking if others need the drop",
            "comparing the drop to gear someone currently uses",
        ]
    parts.append(f"Angle: {random.choice(angles)}")

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append("Use item placeholder at least once")

    if is_rp:
        guidelines.append(
            "Follow the mood and length sequence above"
        )

        if bot_count > 2:
            guidelines.append(
                f"EVERY speaker MUST have at least one "
                f"message — do NOT skip any participant"
            )

        guidelines.append(
            "Each speaker stays in character for their "
            "race and class"
        )

    guidelines.append(
        "STRICT: Each message MUST be under 120 "
        "characters. Short is better"
    )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_conversation_json_instruction(
        prompt, bot_names, msg_count, allow_action, require_all_speakers=is_rp,
    )


def build_event_conversation_prompt(
    bots: List[dict],
    event_context: str,
    zone_id: int = 0,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    area_id: int = 0,
) -> str:
    """Build a prompt for an event-triggered conversation."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    parts = []
    bot_count = len(bots)
    bot_names = [b['name'] for b in bots]

    if is_rp:
        parts.append(
            f"Generate an in-character General chat exchange "
            f"between {bot_count} adventurers in "
            f"{bots[0]['zone']}."
        )
    else:
        parts.append(
            f"Generate a casual General chat exchange between "
            f"{bot_count} human players currently playing WoW in "
            f"{bots[0]['zone']}. They are people using a public "
            f"server chat channel, not characters roleplaying. "
            f"The event below is context for why chat may happen, "
            f"not automatically the only subject unless the later "
            f"event-specific instructions say otherwise."
        )
    parts.append(f"Speakers: {', '.join(bot_names)}")
    parts.append(
        "Names: Sometimes use their name when addressing "
        "directly (maybe once), but not every message."
    )

    parts.append(f"\nEVENT CONTEXT: {event_context}")

    # Zone flavor and subzone context
    if is_rp and zone_id:
        zone_flav = get_zone_flavor(zone_id)
        if zone_flav:
            parts.append(
                f"Zone context: {zone_flav}"
            )
        subzone_lore = get_subzone_lore(
            zone_id, area_id
        )
        if subzone_lore:
            parts.append(
                f"Current subzone: {subzone_lore}"
            )
        else:
            subzone_name = get_subzone_name(
                zone_id, area_id
            )
            if subzone_name:
                parts.append(
                    f"Subzone: {subzone_name}"
                )

    is_transport = (
        'boat' in event_context.lower()
        or 'zeppelin' in event_context.lower()
        or 'turtle' in event_context.lower()
    )
    is_holiday = (
        'event has just begun' in event_context.lower()
        or 'event is coming to an end'
        in event_context.lower()
    )
    if is_transport:
        parts.append(
            "This transport just arrived - at least one bot "
            "should comment on it!"
        )
        parts.append(
            "Use the specific transport type "
            "(boat/zeppelin/turtle), NOT the generic word "
            "'transport'."
        )
        parts.append(
            "CRITICAL: Read the event context carefully - "
            "it tells you WHERE the transport arrived (your "
            "current location) and WHERE it came FROM."
        )
        parts.append(
            "If bots want to board or leave, they go TO the "
            "origin (where it came from), NOT to their "
            "current location!"
        )
        parts.append(
            "If a ship name is mentioned (e.g., 'The "
            "Moonspray'), you can optionally include it."
        )
    elif is_holiday:
        parts.append(
            "This conversation should be ABOUT the "
            "event! Each bot shares their opinion or "
            "feelings about it - excited, annoyed, "
            "nostalgic, indifferent, etc. "
            "Mention the event by name."
        )
    else:
        parts.append(
            "The conversation may naturally reference this "
            "event, or players may chat about something else."
        )
        if is_rp:
            parts.append(
                "The event provides context for the conversation, "
                "but speakers do not have to mention it explicitly."
            )
        else:
            parts.append(
                "The event is only a reason players might start "
                "chatting. Do not describe the atmosphere, scenery, "
                "weather, or surroundings. Players may react briefly, "
                "ignore the event, misunderstand it, complain, joke, "
                "or change the subject."
            )

    zone_flavor = get_zone_flavor(zone_id)
    if is_rp and zone_flavor:
        parts.append(f"Zone context: {zone_flavor}")

    if is_rp:
        weather_for_context = (
            current_weather
            if 'weather' not in event_context.lower()
            else None
        )
        append_environmental_context(
            parts, weather_for_context
        )

    # Precompute shared race context once per unique race.
    # Pass race_count so lore uses cumulative probability
    # 1-(1-p)^n, preserving pre-dedup lore frequency.
    shared_race_cache = {}
    if is_rp:
        race_counts = {}
        for bot in bots:
            r = bot.get('race', '')
            if r:
                race_counts[r] = race_counts.get(r, 0) + 1
        for race, count in race_counts.items():
            _, sr, _ = build_race_class_context_parts(
                race, '', race_count=count
            )
            shared_race_cache[race] = sr

    seen_races = set()
    seen_classes = set()
    for bot in bots:
        if is_rp or random.random() < 0.4:
            race = bot.get('race', '')
            cls = bot.get('class', '')
            parts.append(
                f"{bot['name']} is a {race} {cls}"
            )
            if is_rp:
                per_bot, _, shared_class = (
                    build_race_class_context_parts(
                        race, cls
                    )
                )
                if per_bot:
                    parts.append(f"  {per_bot}")
                if race not in seen_races:
                    sr = shared_race_cache.get(
                        race, ''
                    )
                    if sr:
                        parts.append(
                            f"  {sr}"
                        )
                    seen_races.add(race)
                if cls not in seen_classes:
                    if shared_class:
                        parts.append(
                            f"  {shared_class}"
                        )
                    seen_classes.add(cls)

    if not is_rp:
        parts.append(
            "AUTHORITATIVE LIVE PLAYERBOT STATES:"
        )

        for bot in bots:
            bot_state = bot.get('bot_state')

            if not isinstance(bot_state, dict):
                continue

            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    f"STATE FOR {bot['name']} ONLY:"
                )
                parts.append(state_context)

        parts.append(
            "STRICT FACT RULE: EVENT CONTEXT is authoritative "
            "for the world event itself. Each speaker's live "
            "state is authoritative for personal character facts. "
            "A speaker may react to, notice, misunderstand, or "
            "comment on the event without proving a detailed "
            "personal history. However, claims about inventory, "
            "equipment, money, professions, quest progress, "
            "level, current activity, or travel must be "
            "compatible with that speaker's own state. Never "
            "transfer personal facts between speakers."
        )

    if is_rp:
        tone = pick_random_tone(mode)
        parts.append(f"Overall tone: {tone}")
    elif random.random() < 0.20:
        parts.append(
            f"Loose conversational flavor: "
            f"{pick_random_tone(mode)}"
        )

    twist = maybe_get_creative_twist(
        chance=0.4 if is_rp else 0.08,
        mode=mode,
    )
    if twist:
        parts.append(
            f"Creative twist for this conversation: {twist}"
        )

    if is_rp:
        min_msgs = bot_count
        max_msgs = bot_count + 2
    else:
        min_msgs = 1
        max_msgs = min(4, bot_count + 2)

    msg_count = select_conversation_message_count(
        bot_count, min_msgs, max_msgs
    )
    if is_rp:
        mood_sequence = generate_conversation_mood_sequence(
            msg_count, mode
        )
        length_sequence = generate_conversation_length_sequence(
            msg_count
        )

        parts.append(
            "\nMOOD AND LENGTH SEQUENCE "
            "(follow this for each message):"
        )
        for i, mood in enumerate(mood_sequence):
            speaker = bot_names[i % bot_count]
            parts.append(
                f"  Message {i+1} ({speaker}): "
                f"mood={mood}, "
                f"length={length_sequence[i]}"
            )

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    if is_rp:
        guidelines.append(
            "Follow the mood and length sequence above"
        )

        if bot_count > 2:
            guidelines.append(
                f"EVERY speaker MUST have at least one "
                f"message — do NOT skip any participant"
            )

        guidelines.append(
            "Each speaker stays in character for their "
            "race and class"
        )
        guidelines.append(
            "VARY message lengths naturally - some brief, "
            "some more expressive"
        )
    else:
        guidelines.append(
            "VARY message lengths naturally - some very "
            "short ('lol', 'yeah'), some medium, "
            "occasionally longer"
        )
    parts.append("Guidelines: " + "; ".join(guidelines))

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_conversation_json_instruction(
        prompt, bot_names, msg_count, allow_action, require_all_speakers=is_rp,
    )


def build_event_statement_prompt(
    bot: dict,
    event_context: str,
    event_type: str = '',
    zone_name: str = 'the world',
    config: dict = None,
    extra_data: dict = None,
    allow_action: bool = True,
    zone_id: int = 0,
    area_id: int = 0,
) -> str:
    """Build a prompt for an event-triggered statement."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    extra_data = extra_data or {}

    is_transport = (
        'boat' in event_context.lower()
        or 'zeppelin' in event_context.lower()
        or 'turtle' in event_context.lower()
    )
    is_holiday = event_type.startswith('holiday')

    if is_transport:
        if is_rp:
            event_instruction = (
                "Comment naturally on this transport arrival. "
                "Use the specific type (boat/zeppelin/turtle), "
                "not the generic word 'transport'. Mention the "
                "destination if known."
            )
        else:
            event_instruction = (
                "This transport event may cause a brief player "
                "reaction. Use the specific type "
                "(boat/zeppelin/turtle), not the generic word "
                "'transport'. Mention the destination only if "
                "it is useful. A mundane reaction is fine."
            )
    elif is_holiday:
        if is_rp:
            event_instruction = (
                "React naturally to this event. You may mention "
                "the event by name and give your character's "
                "opinion about it."
            )
        else:
            event_instruction = (
                "React like a player noticing this event in-game. "
                "You may mention the event by name, complain about "
                "it, ask about it, joke briefly, or barely react."
            )

    elif event_type == 'weather_change':
        if is_rp:
            event_instruction = (
                "React naturally to the change in weather. "
                "The weather event may be noticed directly or "
                "worked naturally into the character's reaction."
            )
        else:
            event_instruction = (
                "Give one very brief reaction like a real WoW "
                "player who just noticed the weather change. "
                "Prefer a few casual words or a short fragment. "
                "Do not describe the scenery or atmosphere. "
                "Do not discuss seasons, climate, realism, or "
                "what the weather means. Do not compare it to "
                "summer, winter, spring, or fall. Do not turn "
                "the weather into a conversation topic. "
                "Examples of the desired level of reaction: "
                "'oh its snowing', 'great, rain lol', "
                "'wtf snow', 'rip visibility'."
            )

    else:
        if is_rp:
            event_instruction = (
                "You may naturally reference this event, or talk "
                "about something else. Do not force the event into "
                "the message."
            )
        else:
            event_instruction = (
                "The event is only a reason a player might type "
                "something. React to it only if that feels natural. "
                "Do not describe the scene, atmosphere, scenery, "
                "weather, or surroundings. A short practical or "
                "throwaway comment is preferred."
            )

    rp_personality = ""
    rp_style = ""
    zone_context = ""
    env_lines = ""

    if is_rp:
        weather_for_context = None
        if 'weather' not in event_context.lower():
            weather_for_context = extra_data.get(
                'current_weather'
            ) or None

        env_lines = "".join(
            f"\n{line}" for line
            in build_environmental_context_lines(
                weather_for_context
            )
        )

        rp_ctx = build_race_class_context(
            bot['bot1_race'],
            bot['bot1_class']
        )
        if rp_ctx:
            rp_personality = f"\n{rp_ctx}"

        rp_style = (
            "\nStay in character but keep it natural and "
            "conversational. No game terms or OOC references, "
            "but don't be overly dramatic or theatrical."
        )

        if zone_id:
            zone_flav = get_zone_flavor(zone_id)
            if zone_flav:
                zone_context += (
                    f"\nZone context: {zone_flav}"
                )

            subzone_lore = get_subzone_lore(
                zone_id, area_id
            )
            if subzone_lore:
                zone_context += (
                    f"\nCurrent subzone: {subzone_lore}"
                )
            else:
                subzone_name = get_subzone_name(
                    zone_id, area_id
                )
                if subzone_name:
                    zone_context += (
                        f"\nSubzone: {subzone_name}"
                    )

    if is_rp:
        identity = build_bot_identity_with_level(
            bot['bot1_name'],
            bot['bot1_race'],
            bot['bot1_class'],
            bot['bot1_level'],
            suffix=' adventurer in World of Warcraft.\n',
        )
    else:
        identity = (
            f"You are {bot['bot1_name']}, a level "
            f"{bot['bot1_level']} "
            f"{bot['bot1_race']} {bot['bot1_class']} "
            f"player in World of Warcraft.\n"
        )

    parts = [
        f"{identity}You are currently in {zone_name}."
    ]

    if not is_rp:
        bot_state = bot.get('bot_state')

        if isinstance(bot_state, dict):
            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    "AUTHORITATIVE LIVE PLAYERBOT STATE:"
                )
                parts.append(state_context)
                parts.append(
                    "STRICT FACT RULE: EVENT CONTEXT is "
                    "authoritative for the world event itself. "
                    "The live state above is authoritative for "
                    "your personal character facts. Personal "
                    "claims about level, progression, money, "
                    "inventory, bag space, equipment, "
                    "professions and profession skill values, "
                    "quests, current activity, or travel must "
                    "be compatible with that state. Casual "
                    "wording such as 'just hit', 'finally got', "
                    "or 'been working on' is fine when the "
                    "underlying factual claim is compatible "
                    "with the current state. If the state does "
                    "not establish a specific personal fact, "
                    "do not invent it."
                )

    if is_rp:
        if env_lines:
            parts.append(env_lines.strip())
        if zone_context:
            parts.append(zone_context.strip())
        if rp_personality:
            parts.append(rp_personality.strip())

    parts.append(f"CONTEXT: {event_context}")
    parts.append(event_instruction)

    if is_rp:
        parts.append(
            f"Your current mood: {pick_random_tone(mode)}"
        )
        if rp_style:
            parts.append(rp_style.strip())
    else:
        # Most event reactions should not be assigned a
        # performance. Occasionally give the player some flavor.
        if random.random() < 0.20:
            parts.append(
                f"Loose conversational flavor: "
                f"{pick_random_tone(mode)}"
            )

        parts.append(
            "Write like a real WoW player typing while playing. "
            "Keep it casual and low-effort. Lowercase, fragments, "
            "abbreviations, missing punctuation, or a very short "
            "reaction are fine. Do not narrate what your character "
            "is doing. Do not turn the event into a story. Do not "
            "try to sound poetic, immersive, clever, or profound."
        )

    parts.append(
        "Respond with one short General chat message. "
        "Usually keep it under 80 characters."
    )

    prompt = "\n\n".join(parts)

    return append_json_instruction(
        prompt, allow_action, skip_emote=True
    )

# =============================================================================
# SPELL PROMPTS
# =============================================================================
def build_spell_statement_prompt(
    bot: dict,
    spell: dict,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    zone_id: int = 0,
) -> str:
    """Build a prompt for a spell/ability statement."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    parts = []

    spell_placeholder = (
        f"{{{{spell:{spell['spell_name']}}}}}"
    )

    if is_rp:
        identity = build_bot_identity(
            bot['name'],
            bot.get('race', ''),
            bot.get('class', ''),
            bot.get('gender', ''),
        )
        parts.append(
            f"{identity} "
            f"Speak in-character about a spell "
            f"or ability you know."
        )
        rp_ctx = build_race_class_context(
            bot.get('race', ''),
            bot.get('class', '')
        )
        if rp_ctx:
            parts.append(rp_ctx)
    else:
        parts.append(
            "Generate a brief WoW General chat "
            "message about a class spell or ability."
        )
        parts.append(f"Zone: {bot['zone']}")

    if is_rp:
        append_environmental_context(parts, current_weather)

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if random.random() < 0.5:
        parts.append(f"Player level: {bot['level']}")
    parts.append(f"Player class: {bot['class']}")

    parts.append(
        f"Spell: {spell['spell_name']}"
    )
    if spell.get('spell_desc'):
        parts.append(
            f"What it does: {spell['spell_desc']}"
        )
    parts.append(
        f"REQUIRED: Include exactly "
        f"{spell_placeholder} in your message "
        f"(this becomes a clickable spell link)"
    )

    if is_rp:
        tone = pick_random_tone(mode)
        mood = pick_random_mood(mode)
        parts.append(f"Tone: {tone}")
        parts.append(f"Mood: {mood}")

        twist = maybe_get_creative_twist(mode=mode)
        if twist:
            parts.append(f"Creative twist: {twist}")
    else:
        # Normal mode should usually sound like ordinary player chat,
        # not a deliberately performed character response.
        if random.random() < 0.25:
            parts.append(f"Tone: {pick_random_tone(mode)}")

        if random.random() < 0.15:
            parts.append(f"Mood: {pick_random_mood(mode)}")

        twist = maybe_get_creative_twist(
            chance=0.08,
            mode=mode,
        )
        if twist:
            parts.append(f"Creative twist: {twist}")

    if is_rp:
        approaches = [
            "talking about mastering the ability",
            "saying how the new power feels",
            "mentioning your training experience",
            "comparing it to another technique",
            "wondering about what comes next",
        ]
    else:
        approaches = [
            "just trained it, excited",
            "asking if it's worth the gold",
            "comparing to another ability",
            "complaining about the spell",
            "bragging about damage/healing",
            "asking for tips on using it",
            "discussing spec or talent build",
        ]
    if random.random() < 0.6:
        parts.append(
            f"Approach: {random.choice(approaches)}"
        )

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append(
        "STRICT: Keep under 80 characters "
        "(the link counts as ~15 chars)"
    )
    if is_rp:
        guidelines.append(
            "Stay in character but sound natural, "
            "not theatrical"
        )
    if is_rp:
        guidelines.append(
            "Be creative without becoming theatrical"
        )
    else:
        guidelines.append(
            "React to the spell like an ordinary player. "
            "Short, practical, confused, impressed, annoyed, "
            "or indifferent reactions are all fine."
        )
    parts.append(
        "Guidelines: " + "; ".join(guidelines)
    )

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_json_instruction(
        prompt, allow_action, skip_emote=True
    )


def build_spell_conversation_prompt(
    bots: List[dict],
    spell: dict,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    zone_id: int = 0,
) -> str:
    """Build a prompt for a spell conversation
    with 2-4 bots discussing an ability."""
    mode = (
        get_chatter_mode(config)
        if config else 'normal'
    )
    is_rp = (mode == 'roleplay')
    parts = []
    bot_count = len(bots)
    bot_names = [b['name'] for b in bots]

    spell_placeholder = (
        f"{{{{spell:{spell['spell_name']}}}}}"
    )

    if is_rp:
        parts.append(
            f"Generate an in-character General "
            f"chat exchange about an ability "
            f"in {bots[0]['zone']}."
        )
    else:
        parts.append(
            f"Generate a casual General chat "
            f"exchange where players discuss a "
            f"class ability in {bots[0]['zone']}."
        )

    parts.append(
        f"Speakers: {', '.join(bot_names)}"
    )
    parts.append(
        "Names: Sometimes use their name when "
        "addressing directly (maybe once), but "
        "not every message."
    )
    parts.append(
        f"The first speaker ({bot_names[0]}) is a "
        f"{bots[0]['class']} who knows this spell."
    )

    # Precompute shared race context once per unique
    # race to avoid duplicating worldview/lore.
    shared_race_cache = {}
    if is_rp:
        race_counts = {}
        for bot in bots:
            r = bot.get('race', '')
            if r:
                race_counts[r] = (
                    race_counts.get(r, 0) + 1
                )
        for race, count in race_counts.items():
            _, sr, _ = build_race_class_context_parts(
                race, '', race_count=count
            )
            shared_race_cache[race] = sr

    seen_races = set()
    seen_classes = set()
    for bot in bots:
        parts.append(
            f"{bot['name']} is a "
            f"{bot['race']} {bot['class']}"
        )
        if is_rp:
            race = bot.get('race', '')
            cls = bot.get('class', '')
            per_bot, _, shared_class = (
                build_race_class_context_parts(
                    race, cls
                )
            )
            if per_bot:
                parts.append(f"  {per_bot}")
            if race not in seen_races:
                sr = shared_race_cache.get(race, '')
                if sr:
                    parts.append(f"  {sr}")
                seen_races.add(race)
            resolved_role = (
                CLASS_ROLE_MAP.get(cls) or ''
            )
            cls_role_key = (cls, resolved_role)
            if cls_role_key not in seen_classes:
                if shared_class:
                    parts.append(f"  {shared_class}")
                seen_classes.add(cls_role_key)

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if is_rp:
        append_environmental_context(parts, current_weather)

    parts.append(
        f"Spell being discussed: "
        f"{spell['spell_name']} "
        f"({bots[0]['class']} ability)"
    )
    if spell.get('spell_desc'):
        parts.append(
            f"What it does: {spell['spell_desc']}"
        )
    parts.append(
        f"REQUIRED: Use {spell_placeholder} in the "
        f"\"message\" field (NOT in the action). "
        f"This becomes a clickable link"
    )
    parts.append(
        "Other speakers may discuss the supplied spell "
        "or make general comments about their class. "
        "They must not claim to know, have trained, or "
        "currently use a specific ability unless that "
        "fact was explicitly supplied in their context."
    )

    if is_rp:
        tone = pick_random_tone(mode)
        parts.append(f"Overall tone: {tone}")
    elif random.random() < 0.20:
        parts.append(
            f"Loose conversational flavor: "
            f"{pick_random_tone(mode)}"
        )

    twist = maybe_get_creative_twist(
        chance=0.4 if is_rp else 0.08,
        mode=mode,
    )
    if twist:
        parts.append(
            f"Creative twist for this "
            f"conversation: {twist}"
        )

    if is_rp:
        min_msgs = bot_count
        max_msgs = bot_count + 2
    else:
        min_msgs = 1
        max_msgs = min(4, bot_count + 2)

    msg_count = select_conversation_message_count(
        bot_count, min_msgs, max_msgs
    )
    if is_rp:
        mood_sequence = (
            generate_conversation_mood_sequence(
                msg_count, mode
            )
        )
        length_sequence = (
            generate_conversation_length_sequence(
                msg_count
            )
        )

        parts.append(
            "\nMOOD AND LENGTH SEQUENCE "
            "(follow this for each message):"
        )
        for i, mood in enumerate(mood_sequence):
            speaker = bot_names[i % bot_count]
            parts.append(
                f"  Message {i+1} ({speaker}): "
                f"mood={mood}, "
                f"length={length_sequence[i]}"
            )

    if is_rp:
        angles = [
            "comparing techniques and training "
            "methods",
            "one demonstrates while others "
            "react",
            "debating which abilities are most "
            "vital",
            "sharing stories of the spell in "
            "battle",
        ]
    else:
        angles = [
            "one just learned it and others "
            "react with jealousy or advice",
            "debating if the spell is overpowered "
            "or underpowered",
            "comparing to similar abilities in "
            "other classes",
            "tips on when and how to use it "
            "effectively",
            "discussing talent builds that "
            "improve the spell",
        ]
    parts.append(f"Angle: {random.choice(angles)}")

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append(
        "Use spell placeholder at least once"
    )

    if is_rp:
        guidelines.append(
            "Follow the mood and length sequence above"
        )

        if bot_count > 2:
            guidelines.append(
                f"EVERY speaker MUST have at least one "
                f"message — do NOT skip any participant"
            )

        guidelines.append(
            "Each speaker stays in character for "
            "their race and class"
        )

    guidelines.append(
        "STRICT: Each message MUST be under 120 "
        "characters. Short is better"
    )
    parts.append(
        "Guidelines: " + "; ".join(guidelines)
    )

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_conversation_json_instruction(
        prompt, bot_names, msg_count, allow_action, require_all_speakers=is_rp,
    )


# =============================================================================
# TRADE / SELL PROMPTS
# =============================================================================
def build_trade_statement_prompt(
    bot: dict,
    item: dict,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    zone_id: int = 0,
) -> str:
    """Build a prompt for a trade/sell statement."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')
    quality_names = {
        0: "gray", 1: "white", 2: "green",
        3: "blue", 4: "purple",
    }
    quality = quality_names.get(
        item.get('item_quality', 2), "green"
    )

    parts = []
    item_placeholder = (
        f"{{{{item:{item['item_name']}}}}}"
    )

    if is_rp:
        identity = build_bot_identity(
            bot['name'],
            bot.get('race', ''),
            bot.get('class', ''),
            bot.get('gender', ''),
        )
        parts.append(
            f"{identity} You want to "
            f"sell or trade an item you found. "
            f"Speak in-character."
        )
        rp_ctx = build_race_class_context(
            bot.get('race', ''), bot.get('class', '')
        )
        if rp_ctx:
            parts.append(rp_ctx)
    else:
        parts.append(
            "Generate a WoW General chat message "
            "where a player is selling or looking "
            "to trade an item."
        )

    if is_rp:
        append_environmental_context(parts, current_weather)

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if not is_rp:
        bot_state = bot.get('bot_state')

        if isinstance(bot_state, dict):
            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    "AUTHORITATIVE LIVE PLAYERBOT STATE:"
                )
                parts.append(state_context)
                parts.append(
                    "STRICT FACT RULE: Personal factual claims "
                    "about inventory, equipment, bag space, "
                    "money, professions, quests, progression, "
                    "activity, or travel must be compatible "
                    "with the live state above. The trade event "
                    "establishes that this item is being offered "
                    "for trade, but do not invent other character "
                    "facts."
                )

    parts.append(
        f"Item: {item['item_name']} ({quality} "
        f"quality)"
    )
    vendor_price = format_price(
        item.get('sell_price', 0)
    )
    if vendor_price:
        parts.append(
            f"Authoritative vendor sell price: "
            f"{vendor_price}. This is ONLY the NPC vendor "
            f"value, not the player-market value. Do not "
            f"claim an AH price, market value, going rate, "
            f"or other factual player price."
        )
    parts.append(
        f"REQUIRED: Include exactly "
        f"{item_placeholder} in the \"message\" "
        f"JSON field (NOT in the action). "
        f"This becomes a clickable link"
    )

    if is_rp:
        tone = pick_random_tone(mode)
        mood = pick_random_mood(mode)
        parts.append(f"Tone: {tone}")
        parts.append(f"Mood: {mood}")

        twist = maybe_get_creative_twist(mode=mode)
        if twist:
            parts.append(f"Creative twist: {twist}")
    else:
        # Normal mode should usually sound like ordinary player chat,
        # not a deliberately performed character response.
        if random.random() < 0.25:
            parts.append(f"Tone: {pick_random_tone(mode)}")

        if random.random() < 0.15:
            parts.append(f"Mood: {pick_random_mood(mode)}")

        twist = maybe_get_creative_twist(
            chance=0.08,
            mode=mode,
        )
        if twist:
            parts.append(f"Creative twist: {twist}")

    if is_rp:
        styles = [
            "announcing you have something to sell",
            "looking for a fair trade",
            "mentioning you've outgrown this gear",
            "offering your find to anyone interested",
        ]
    else:
        styles = [
            "WTS style - short trade post, taking offers",
            "casual offer - mentioning you don't "
            "need it",
            "asking if anyone needs the item",
            "advertising the item with enthusiasm",
            "lowkey mention you're selling cheap",
            "LF buyer, taking offers",
        ]
    parts.append(f"Style: {random.choice(styles)}")

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append(
        "STRICT: Keep under 80 characters "
        "(the link counts as ~15 chars)"
    )
    guidelines.append(
        "Do NOT invent an exact asking price, AH price, "
        "market value, or going rate. Prefer phrases like "
        "'taking offers', 'pst offer', 'anyone need this?', "
        "'selling cheap', or 'wts'."
    )
    guidelines.append(
        "Trade abbreviations encouraged: WTS, WTB, "
        "WTT, pst, /w, OBO"
    )
    if is_rp:
        guidelines.append(
            "Stay in character but sound natural"
        )
    parts.append(
        "Guidelines: " + "; ".join(guidelines)
    )

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_json_instruction(
        prompt, allow_action, skip_emote=True
    )


def build_trade_conversation_prompt(
    bots: List[dict],
    item: dict,
    config: dict = None,
    current_weather: str = None,
    recent_messages: list = None,
    allow_action: bool = True,
    speaker_talent_context=None,
    zone_id: int = 0,
) -> str:
    """Build a prompt for a trade conversation
    with 2-4 bots haggling over an item."""
    mode = (
        get_chatter_mode(config)
        if config else 'normal'
    )
    is_rp = (mode == 'roleplay')
    parts = []
    bot_count = len(bots)
    bot_names = [b['name'] for b in bots]

    quality_names = {
        0: "gray", 1: "white", 2: "green",
        3: "blue", 4: "purple",
    }
    quality = quality_names.get(
        item.get('item_quality', 2), "green"
    )
    item_placeholder = (
        f"{{{{item:{item['item_name']}}}}}"
    )

    if is_rp:
        parts.append(
            f"Generate an in-character General "
            f"chat exchange about trading/selling "
            f"an item in {bots[0]['zone']}."
        )
    else:
        parts.append(
            f"Generate a casual General chat "
            f"exchange where players haggle or "
            f"discuss selling an item in "
            f"{bots[0]['zone']}."
        )

    parts.append(
        f"Speakers: {', '.join(bot_names)}"
    )
    parts.append(
        "Names: Sometimes use their name when "
        "addressing directly (maybe once), but "
        "not every message."
    )
    parts.append(
        f"The first speaker ({bot_names[0]}) is "
        f"the seller."
    )

    if is_rp:
        for bot in bots:
            parts.append(
                f"{bot['name']} is a "
                f"{bot['race']} {bot['class']}"
            )

    if speaker_talent_context:
        parts.append(speaker_talent_context)

    if not is_rp:
        parts.append(
            "AUTHORITATIVE LIVE PLAYERBOT STATES:"
        )

        for bot in bots:
            bot_state = bot.get('bot_state')

            if not isinstance(bot_state, dict):
                continue

            state_context = build_bot_state_context(
                bot_state
            )

            if state_context:
                parts.append(
                    f"STATE FOR {bot['name']} ONLY:"
                )
                parts.append(state_context)

        parts.append(
            "STRICT SPEAKER FACT RULE: Each speaker may "
            "only make personal factual claims compatible "
            "with that speaker's own live state. Never "
            "transfer inventory, equipment, money, "
            "professions, quests, progression, activity, "
            "or travel facts between speakers. The first "
            "speaker is the seller of the supplied item; "
            "that does not establish unrelated possessions "
            "or facts about any speaker."
        )

    if is_rp:
        append_environmental_context(parts, current_weather)

    parts.append(
        f"Item for sale: {item['item_name']} "
        f"({quality} quality)"
    )
    vendor_price = format_price(
        item.get('sell_price', 0)
    )
    if vendor_price:
        parts.append(
            f"Authoritative vendor sell price: "
            f"{vendor_price}. This is ONLY the NPC vendor "
            f"value. It does not establish an AH price, "
            f"market value, fair player price, or going rate."
        )
    parts.append(
        f"REQUIRED: Use {item_placeholder} in the "
        f"\"message\" field (NOT in the action). "
        f"This becomes a clickable link"
    )

    if is_rp:
        tone = pick_random_tone(mode)
        parts.append(f"Overall tone: {tone}")
    elif random.random() < 0.20:
        parts.append(
            f"Loose conversational flavor: "
            f"{pick_random_tone(mode)}"
        )

    twist = maybe_get_creative_twist(
        chance=0.4 if is_rp else 0.08,
        mode=mode,
    )
    if twist:
        parts.append(
            f"Creative twist for this "
            f"conversation: {twist}"
        )

    if is_rp:
        min_msgs = bot_count
        max_msgs = bot_count + 2
    else:
        min_msgs = 1
        max_msgs = min(4, bot_count + 2)

    msg_count = select_conversation_message_count(
        bot_count, min_msgs, max_msgs
    )
    if is_rp:
        mood_sequence = (
            generate_conversation_mood_sequence(
                msg_count, mode
            )
        )
        length_sequence = (
            generate_conversation_length_sequence(
                msg_count
            )
        )

        parts.append(
            "\nMOOD AND LENGTH SEQUENCE "
            "(follow this for each message):"
        )
        for i, mood in enumerate(mood_sequence):
            speaker = bot_names[i % bot_count]
            parts.append(
                f"  Message {i+1} ({speaker}): "
                f"mood={mood}, "
                f"length={length_sequence[i]}"
            )

    if is_rp:
        angles = [
            "bartering with in-character haggling",
            "one offers an item and others "
            "appraise its worth",
            "negotiating a trade between "
            "adventurers",
            "debating a fair price with lore "
            "flavor",
        ]
    else:
        angles = [
            "seller posts WTS, buyer makes an offer "
            "or says it's too expensive",
            "seller offers item, others comment "
            "on whether it's worth it",
            "back-and-forth negotiation with a "
            "deal or walkaway",
            "someone undercuts or offers a better "
            "item",
            "casual price check turning into a "
            "sale",
        ]
    parts.append(f"Angle: {random.choice(angles)}")

    guidelines = build_dynamic_guidelines(
        config=config, mode=mode
    )
    guidelines.append(
        "Use item placeholder at least once"
    )
    guidelines.append(
        "Do NOT invent exact asking prices, counteroffers, "
        "AH prices, market values, or going rates. Players "
        "may haggle qualitatively: 'too much', 'too high', "
        "'ill pass', 'can you do less?', 'make an offer', "
        "'deal', etc. The exact vendor sell price may only "
        "be mentioned if it was supplied above."
    )
    guidelines.append(
        "Trade abbreviations OK: WTS, WTB, WTT, "
        "pst, OBO"
    )
    if is_rp:
        guidelines.append(
            "Follow the mood and length sequence above"
        )

        if bot_count > 2:
            guidelines.append(
                f"EVERY speaker MUST have at least one "
                f"message — do NOT skip any participant"
            )

        guidelines.append(
            "Each speaker stays in character for "
            "their race and class"
        )

    guidelines.append(
        "STRICT: Each message MUST be under 120 "
        "characters. Short is better"
    )
    parts.append(
        "Guidelines: " + "; ".join(guidelines)
    )

    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        parts.append(anti_rep)

    prompt = "\n".join(parts)
    return append_conversation_json_instruction(
        prompt, bot_names, msg_count, allow_action, require_all_speakers=is_rp,
    )


# =============================================================================
# ZONE INTRUSION PROMPT
# =============================================================================
def build_zone_intrusion_prompt(
    extra_data, config
):
    """Build prompt for zone intrusion yell."""
    mode = get_chatter_mode(config) if config else 'normal'
    is_rp = (mode == 'roleplay')

    # Defender identity
    defender_name = extra_data.get(
        'defender_name', 'Unknown'
    )
    defender_class = CLASS_NAMES.get(
        int(extra_data.get('defender_class', 0)),
        'adventurer'
    )
    defender_race = RACE_NAMES.get(
        int(extra_data.get('defender_race', 0)),
        'Unknown'
    )
    defender_level = extra_data.get(
        'defender_level', '??'
    )

    # Intruder identity
    intruder_name = extra_data.get(
        'intruder_name', 'Unknown'
    )
    intruder_class = CLASS_NAMES.get(
        int(extra_data.get('intruder_class', 0)),
        'adventurer'
    )
    intruder_race = RACE_NAMES.get(
        int(extra_data.get('intruder_race', 0)),
        'Unknown'
    )
    intruder_level = extra_data.get(
        'intruder_level', '??'
    )

    zone_name = extra_data.get(
        'zone_name', 'this area'
    )
    is_capital = extra_data.get(
        'is_capital', False
    )

    capital_suffix = (
        " -- your faction's capital city!"
        if is_capital
        else " -- your faction's territory!"
    )

    parts = []

    if is_rp:
        parts.append(
            build_bot_identity_with_level(
                defender_name,
                defender_race,
                defender_class,
                defender_level,
                suffix='.',
            )
        )

        rc_context = build_race_class_context(
            defender_race, defender_class
        )
        if rc_context:
            parts.append(rc_context)
    else:
        parts.append(
            f"You are {defender_name}, a level "
            f"{defender_level} {defender_race} "
            f"{defender_class} player in World of Warcraft."
        )

    parts.append(
        f"\nSITUATION: An enemy {intruder_race} "
        f"{intruder_class} named {intruder_name} "
        f"(level {intruder_level}) has just been "
        f"spotted in {zone_name}"
        + capital_suffix
    )

    if is_rp:
        parts.append(
            "\nYell a brief, urgent warning to alert "
            "nearby allies. 1-2 sentences max. "
            "Your race/class personality may shape the tone: "
            "a warrior might give a battle cry, "
            "a rogue might give a terse warning, "
            "a priest might invoke the Light."
        )
    else:
        parts.append(
            "\nWrite the kind of quick warning a real WoW "
            "player would type after noticing an enemy player. "
            "Treat race, class, level, and zone as game "
            "information, not roleplay material. Keep it "
            "casual, urgent, and low-effort. Shorthand, "
            "lowercase, missing punctuation, and fragments "
            "are fine. Examples of the STYLE only: "
            "'ally rogue in org', 'horde here', "
            "'70 warr at the gate', 'inc', 'watch out'. "
            "Do not copy an example unless it naturally fits."
        )

    parts.append(
        "\nRules:"
        "\n- Respond with ONLY the yell text"
        "\n- No /slash commands"
        "\n- No *emotes* or action text"
        "\n- No quotation marks"
        "\n- Keep it short and urgent"
    )

    if is_rp:
        parts.append(
            "- Stay in character without becoming theatrical"
        )
    else:
        parts.append(
            "- Do not roleplay, narrate, give a battle cry, "
            "invoke your class fantasy, or use fantasy dialogue"
        )
        parts.append(
            "- Usually use one short sentence or fragment"
        )

    # Plain-string prompt path - not routed through
    # append_json_instruction, so inject the language
    # rule directly.
    from chatter_shared import get_language_rule
    lang_rule = get_language_rule()
    if lang_rule:
        parts.append(lang_rule)

    return '\n'.join(parts)
