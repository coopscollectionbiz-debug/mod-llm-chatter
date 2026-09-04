"""
Chatter General - General channel reactions.

When a real player types in General channel, nearby
bots react with statements or conversations, making
the world feel alive and interactive.

Zone-scoped: history per zone, cooldowns per zone,
bot selection by zone.
"""

import logging
import random
import time

# Module-level config defaults (set by init_general_config)
_chat_history_limit = 10
_spice_count = 2
_extended_conv_chance = 40
_extended_max_messages = 3

from chatter_shared import (
    call_llm, cleanup_message, strip_speaker_prefix,
    get_chatter_mode, get_class_name, get_race_name,
    get_gender_label,
    build_race_class_context, parse_extra_data,
    calculate_dynamic_delay,
    find_addressed_bot,
    find_player_message_state_matches,
    player_premise_rule,
    insert_chat_message,
    build_anti_repetition_context,
    build_bot_identity_with_level,
    build_bot_state_context,
    get_recent_zone_messages,
    append_json_instruction,
    parse_single_response,
    should_include_action,
    _zone_delivery_delay,
    _zone_last_delivery,
    get_zone_flavor,
    get_zone_name,
    get_player_zone,
    build_zone_metadata,
    build_talent_context,
)
from chatter_prompts import (
    pick_random_tone,
    pick_random_mood,
    maybe_get_creative_twist,
    build_environmental_context_lines,
    pick_personality_spices,
)
from chatter_constants import (
    PERSONALITY_TRAITS,
    RACE_SPEECH_PROFILES,
    LENGTH_HINTS, RP_LENGTH_HINTS,
)
from chatter_links import resolve_and_format_links
from chatter_db import (
    fail_event,
    get_character_info_by_name,
    mark_event,
    reserve_playerbot_service_action,
)

from chatter_group_general_reaction import (
    maybe_queue_group_general_reaction,
)
from chatter_llm import quick_llm_analyze

logger = logging.getLogger(__name__)


def init_general_config(config):
    """Initialize module-level config values."""
    global _chat_history_limit, _spice_count
    try:
        raw = config.get(
            'LLMChatter.GeneralChat.HistoryLimit',
            config.get(
                'LLMChatter.ChatHistoryLimit', 10
            )
        )
        val = int(raw)
    except (ValueError, TypeError):
        val = 10
    _chat_history_limit = max(1, min(val, 50))
    try:
        _spice_count = int(config.get(
            'LLMChatter.PersonalitySpiceCount', 2
        ))
        _spice_count = max(0, min(_spice_count, 5))
    except Exception:
        logger.error(
            "Failed to parse PersonalitySpiceCount",
            exc_info=True,
        )
        _spice_count = 2

    global _extended_conv_chance, _extended_max_messages
    try:
        _extended_conv_chance = int(config.get(
            'LLMChatter.GeneralChat.'
            'ExtendedConversationChance', 40
        ))
        _extended_conv_chance = max(
            0, min(_extended_conv_chance, 100)
        )
    except (ValueError, TypeError):
        _extended_conv_chance = 40
    try:
        _extended_max_messages = int(config.get(
            'LLMChatter.GeneralChat.'
            'ExtendedMaxMessages', 3
        ))
        _extended_max_messages = max(
            3, min(_extended_max_messages, 8)
        )
    except (ValueError, TypeError):
        _extended_max_messages = 3



def _pick_random_traits():
    """Pick 3 random traits for a bot."""
    categories = random.sample(
        list(PERSONALITY_TRAITS.keys()), 3
    )
    return [
        random.choice(PERSONALITY_TRAITS[cat])
        for cat in categories
    ]


def _pick_length_hint(mode):
    """Pick a random length hint."""
    is_rp = (mode == 'roleplay')
    pool = RP_LENGTH_HINTS if is_rp else LENGTH_HINTS
    hint = random.choice(pool)
    long_chance = 15 if is_rp else 12
    if random.randint(1, 100) <= long_chance:
        return (
            f"Length: {hint}\n"
            f"Length mode: longer allowed "
            f"(up to ~150 chars max) â€” one "
            f"sentence\n"
            f"HARD LIMIT: Never exceed 150 "
            f"characters total"
        )
    return (
        f"Length: {hint}\n"
        f"Length mode: short/medium only "
        f"(avoid long messages)\n"
        f"HARD LIMIT: Never exceed 150 "
        f"characters total"
    )



def _get_general_chat_history(
    db, zone_id, limit=None
):
    """Get recent General channel messages for a zone.
    Returns oldest-first for natural prompt reading.
    """
    if limit is None:
        limit = _chat_history_limit
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT speaker_name, is_bot, message
        FROM llm_general_chat_history
        WHERE zone_id = %s
        ORDER BY id DESC
        LIMIT %s
    """, (zone_id, limit))
    rows = cursor.fetchall()
    return list(reversed(rows))


def _format_general_history(history):
    """Format General chat history for prompts."""
    if not history:
        return ""
    lines = []
    for msg in history:
        name = msg['speaker_name']
        text = msg['message']
        if msg['is_bot']:
            lines.append(f"  {name}: {text}")
        else:
            lines.append(
                f"  {name} (player): {text}"
            )
    return (
        "\nRecent General channel chat:\n"
        + '\n'.join(lines)
    )


def _store_general_chat(
    db, zone_id, speaker_name, is_bot, message
):
    """Store a message in General chat history
    and prune old messages per zone.
    """
    cursor = db.cursor()
    cursor.execute("""
        INSERT INTO llm_general_chat_history
        (zone_id, speaker_name, is_bot, message)
        VALUES (%s, %s, %s, %s)
    """, (
        zone_id, speaker_name,
        1 if is_bot else 0, message[:500]
    ))
    db.commit()

    # Prune to keep recent messages per zone
    cursor.execute("""
        DELETE FROM llm_general_chat_history
        WHERE zone_id = %s AND id NOT IN (
            SELECT id FROM (
                SELECT id
                FROM llm_general_chat_history
                WHERE zone_id = %s
                ORDER BY id DESC
                LIMIT %s
            ) AS keep
        )
    """, (zone_id, zone_id, _chat_history_limit))
    db.commit()


def _get_bot_info(db, bot_guid):
    """Fetch bot class/race/level from characters."""
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT name, class, race, level, gender
        FROM characters
        WHERE guid = %s
    """, (bot_guid,))
    return cursor.fetchone()


def _resolve_zone_context(db, player_name, extra_data):
    """Resolve player-centric zone for General chat.

    Uses get_player_zone() as source of truth, falls
    back to C++ event data. No subzone needed for
    General channel (zone-wide scope).

    Returns dict with zone_id, zone_name, zone_flavor,
    zone_meta.
    """
    pz, _ = get_player_zone(db, player_name)
    if pz:
        zone_id = pz
        zone_name = get_zone_name(pz) or 'Unknown'
    else:
        zone_id = int(
            extra_data.get('zone_id', 0)
        )
        zone_name = extra_data.get(
            'zone_name', 'Unknown'
        )
    zone_flavor = get_zone_flavor(zone_id) or ''
    zone_meta = build_zone_metadata(
        zone_name=(
            zone_name
            if zone_name != 'Unknown' else ''
        ),
        zone_flavor=zone_flavor,
        subzone_name='',
        subzone_lore='',
    )
    return {
        'zone_id': zone_id,
        'zone_name': zone_name,
        'zone_flavor': zone_flavor,
        'zone_meta': zone_meta,
    }


def _select_primary_bot(
    db, client, config, bot_guids, bot_names,
    player_name, player_message, mode,
    chat_hist="",
    bot_states=None,
):
    """Pick the primary bot for a General reaction.

    Handles addressed-bot detection, conversation
    vs statement decision, and bot info lookup.

    Returns dict with bot1_guid, bot1_idx, bot1_name,
    bot1_race, bot1_class, bot1_class_id, bot1_level,
    bot1_traits, is_conversation.
    Returns None if no valid bot found.
    """
    conv_chance = int(config.get(
        'LLMChatter.GeneralChat.'
        'ConversationChance', 30
    ))
    is_conversation = (
        len(bot_guids) >= 2
        and random.randint(1, 100) <= conv_chance
    )

    addr_result = find_addressed_bot(
        player_message, bot_names,
        client=client, config=config,
        chat_history=chat_hist,
    )
    addressed = addr_result.get('bot')

    bot1_idx = None
    explicit_recipient = False

    if addressed:
        for i, name in enumerate(bot_names):
            if name == addressed:
                bot1_idx = i
                explicit_recipient = True
                break

    if bot1_idx is None and len(bot_guids) == 1:
        bot1_idx = 0
        explicit_recipient = True

    if (
        bot1_idx is None
        and isinstance(bot_states, dict)
    ):
        routing_candidates = []

        for i, raw_guid in enumerate(bot_guids):
            guid = int(raw_guid)

            state = bot_states.get(
                str(guid),
                bot_states.get(guid, {}),
            )

            routing_candidates.append({
                'name': bot_names[i],
                'bot_state': state,
                '_idx': i,
            })

        state_matches = (
            find_player_message_state_matches(
                player_message,
                routing_candidates,
            )
        )

        if state_matches:
            picked = random.choice(
                state_matches
            )

            bot1_idx = int(
                picked['candidate']['_idx']
            )

    if bot1_idx is None:
        bot1_idx = random.randint(
            0, len(bot_guids) - 1
        )

    bot1_guid = int(bot_guids[bot1_idx])
    bot1_info = _get_bot_info(db, bot1_guid)
    if not bot1_info:
        return None

    return {
        'bot1_guid': bot1_guid,
        'bot1_idx': bot1_idx,
        'bot1_name': bot1_info['name'],
        'bot1_race': get_race_name(
            bot1_info['race']
        ),
        'bot1_class': get_class_name(
            bot1_info['class']
        ),
        'bot1_class_id': bot1_info['class'],
        'bot1_level': bot1_info['level'],
        'bot1_gender': get_gender_label(bot1_info['gender']),
        'bot1_traits': _pick_random_traits(),
        'is_conversation': is_conversation,
    }


def _build_general_response_prompt(
    bot_name, bot_race, bot_class, bot_level,
    bot_gender,
    traits, player_name, player_message,
    zone_name, chat_history, mode,
    recent_messages=None, allow_action=True,
    link_context="",
    bot_state=None,
    speaker_talent_context=None,
    target_talent_context=None,
    zone_flavor="",
    subzone_name="",
    subzone_lore="",
):
    """Build prompt for a bot responding to a
    player's General channel message.
    """
    is_rp = (mode == 'roleplay')
    trait_str = ', '.join(traits)
    tone = pick_random_tone(mode)
    mood = pick_random_mood(mode)
    twist = maybe_get_creative_twist(
        chance=1.0 if is_rp else 0.08,
        mode=mode,
)

    rp_context = ""
    if is_rp:
        ctx = build_race_class_context(
            bot_race, bot_class
        )
        if ctx:
            rp_context = f"\n{ctx}"

        profile = RACE_SPEECH_PROFILES.get(bot_race)
        if profile:
            fw = profile.get('flavor_words', [])
            flavor = ', '.join(
                random.sample(fw, min(3, len(fw)))
            )
            if flavor:
                rp_context += (
                    f"\nRace flavor words you might "
                    f"use: {flavor}"
                )

    if is_rp:
        style = (
            "Reply in-character. Stay natural and "
            "grounded. Don't break character."
        )
    else:
        style = (
            "Reply like a real WoW player casually "
            "typing in General chat while playing. "
            "Keep it low-effort and conversational. "
            "Lowercase, fragments, abbreviations, "
            "WoW shorthand, and occasional internet "
            "slang are natural when they fit. "
            "Do not sound like an NPC, lore writer, "
            "Reddit essay, or scripted comedian."
        )

    env_lines = (
        build_environmental_context_lines()
        if is_rp
        else []
    )

    identity = build_bot_identity_with_level(
        bot_name,
        bot_race,
        bot_class,
        bot_level,
        gender=bot_gender,
    )
    prompt = (
        f"{identity}\n"
        f"Your personality: {trait_str}\n"
    )

    factual_context = build_bot_state_context(
        bot_state or {}
    )
    if factual_context:
        prompt += f"\n{factual_context}\n"

        if not is_rp:
            prompt += (
                "\nLIVE STATE RULES:\n"
                "- Authoritative live state overrides prior context only "
                "when they conflict about CURRENT observable/mechanical "
                "character state. Absence from live state is not proof that "
                "the character lacks general WoW knowledge or past history.\n"
                "- Keep CURRENT state grounded: exact quest progress/counts, "
                "current inventory/equipment/money, exact current location or "
                "activity, nearby/local-world observations, and actual "
                "spell/service capability must come from authoritative context.\n"
                "- General Wrath-era WoW knowledge is allowed. You may "
                "accurately discuss quests, zones, dungeons, mobs, items, "
                "professions, class knowledge, leveling, and mechanics.\n"
                "- Plausible level/class-appropriate history and plans may be "
                "improvised and should remain consistent, including profession "
                "history/plans and quests previously done or intended. Do not "
                "invent precise CURRENT progress or possessions.\n"
                "- If the player makes a factual claim about YOU that "
                "conflicts with your authoritative current state, do "
                "not make the claim true merely to keep the reply "
                "smooth. A natural correction, disagreement, or "
                "confused response is allowed.\n"
                "- The live-state restriction applies to factual WoW "
                "game-state claims. Harmless social details, opinions, "
                "jokes, preferences, real-world topics, and conversational "
                "personality may be improvised naturally.\n"
                "- Do not say you don't know merely because a harmless "
                "social answer is absent from live game state.\n"
                "- Supplied [[quest:...]] and [[item:...]] "
                "tokens are exact opaque strings. Copy a "
                "token exactly when relevant or omit it. "
                "Never create or modify a token.\n"
            )
    if speaker_talent_context:
        prompt += f"{speaker_talent_context}\n"
    if target_talent_context:
        prompt += f"{target_talent_context}\n"
    prompt += (
        f"Your tone: {tone}\n"
        f"Your mood: {mood}\n"
    )
    if twist:
        prompt += f"Creative twist: {twist}\n"

    address_hint = ""
    if is_rp and random.random() < 0.4:
        address_hint = (
            f"- You may address {player_name} by "
            f"name in your reply\n"
        )

    if is_rp:
        prompt += (
            f"You are in {zone_name}."
        )
    else:
        prompt += (
            f"You are in {zone_name}. "
            f"This location is factual gameplay context only, "
            f"not a conversation topic. Do not comment on "
            f"scenery, weather, lighting, brightness, darkness, "
            f"the sky, atmosphere, landscape, views, ambience, "
            f"or the zone's 'vibe'. Do not turn the player's "
            f"message into an observation about the area."
        )
    if env_lines:
        prompt += "\n" + "\n".join(env_lines)
    if is_rp and zone_flavor:
        prompt += f"\nZone context: {zone_flavor}"
    if is_rp and subzone_lore:
        prompt += (
            f"\nCurrent subzone: {subzone_lore}"
        )
    elif subzone_name:
        prompt += f"\nSubzone: {subzone_name}"
    prompt += (
        f"{rp_context}\n"
        f"{chat_history}\n\n"
    )
    if link_context:
        prompt += f"{link_context}\n\n"
    prompt += (
        f"{player_name} just said in General "
        f"channel:\n"
        f"\"{player_message}\"\n\n"
        f"{style}\n\n"
        f"Reply in General channel.\n"
        f"{_pick_length_hint(mode)}\n"
        f"Rules:\n"
        f"- No quotes, no emojis\n"
        f"- Use normal WoW shorthand and casual internet "
        f"language when it fits. Things like lol, lmao, "
        f"tbh, ngl, bruh, rip, gz, omw, sec, mb, dps, "
        f"tank, healer, gg, buff, and nerf are fine. "
        f"Do not force slang or memes into every reply\n"
        f"- NEVER use brackets [] around creature, "
        f"NPC, zone, or faction names - write them "
        f"as plain text\n"
        f"- Respond directly to what {player_name} said. "
        f"Do not ignore it and introduce an unrelated topic\n"
        f"- In Normal mode, never make scenery, weather, "
        f"lighting, brightness, the sky, atmosphere, "
        f"landscape, zone appearance, or the area's vibe "
        f"the subject of the reply. Location names are "
        f"practical gameplay context only\n"
        f"{address_hint}"
        f"- Reflect your personality traits\n"
        f"- Don't repeat what they said\n"
        f"- If there's chat history, stay "
        f"consistent with the conversation\n"
        f"- General is public chat, so drive-by replies and brief "
        f"answers are normal, but a direct question should still get "
        f"enough information to answer it clearly\n"
        f"- Do not make every speaker use lowercase, fragments, slang, "
        f"abbreviations, or missing punctuation; player typing styles "
        f"should vary naturally\n"
    )
    spices = pick_personality_spices(
        mode=mode, spice_count_override=_spice_count
    )
    if spices:
        prompt += (
            "\nBackground feelings (texture, "
            "not the topic): "
            + "; ".join(spices)
        )
    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        prompt += f"\n{anti_rep}"
    prompt = append_json_instruction(
        prompt, allow_action, skip_emote=True,
        skip_action_rng=True,
    )
    return prompt


def _build_general_followup_prompt(
    bot_name, bot_race, bot_class, bot_level,
    bot_gender,
    traits, first_bot_name, first_bot_response,
    player_name, player_message,
    zone_name, chat_history, mode,
    recent_messages=None, allow_action=True,
    link_context="",
    bot_state=None,
    speaker_talent_context=None,
    target_talent_context=None,
    zone_flavor="",
    subzone_name="",
    subzone_lore="",
):
    """Build prompt for a 2nd bot following up
    on the 1st bot's reaction in General channel.
    """
    is_rp = (mode == 'roleplay')
    trait_str = ', '.join(traits)
    tone = pick_random_tone(mode)
    mood = pick_random_mood(mode)

    rp_context = ""
    if is_rp:
        ctx = build_race_class_context(
            bot_race, bot_class
        )
        if ctx:
            rp_context = f"\n{ctx}"

        profile = RACE_SPEECH_PROFILES.get(bot_race)
        if profile:
            fw = profile.get('flavor_words', [])
            flavor = ', '.join(
                random.sample(fw, min(3, len(fw)))
            )
            if flavor:
                rp_context += (
                    f"\nRace flavor words you might "
                    f"use: {flavor}"
                )

    if is_rp:
        style = (
            "Reply in-character. Stay natural and "
            "grounded."
        )
    else:
        style = (
            "Reply like a real WoW player casually "
            "typing in General chat while playing. "
            "Keep it low-effort and conversational. "
            "Lowercase, fragments, abbreviations, "
            "WoW shorthand, and occasional internet "
            "slang are natural when they fit. "
            "Do not sound like an NPC, lore writer, "
            "Reddit essay, or scripted comedian."
        )

    # Explicit name-addressing is mainly an RP feature.
    address_hint = ""
    if is_rp and random.random() < 0.4:
        target = random.choice(
            [player_name, first_bot_name]
        )
        address_hint = (
            f"- You may address {target} by "
            f"name in your reply\n"
        )

    identity = build_bot_identity_with_level(
        bot_name,
        bot_race,
        bot_class,
        bot_level,
        gender=bot_gender,
    )
    prompt = (
        f"{identity}\n"
        f"Your personality: {trait_str}\n"
    )

    factual_context = build_bot_state_context(
        bot_state or {}
    )
    if factual_context:
        prompt += f"\n{factual_context}\n"

        if not is_rp:
            prompt += (
                "\nLIVE STATE RULES:\n"
                "- Authoritative live state overrides prior context only "
                "when they conflict about CURRENT observable/mechanical "
                "character state. Absence from live state is not proof that "
                "the character lacks general WoW knowledge or past history.\n"
                "- Keep CURRENT state grounded: exact quest progress/counts, "
                "current inventory/equipment/money, exact current location or "
                "activity, nearby/local-world observations, and actual "
                "spell/service capability must come from authoritative context.\n"
                "- General Wrath-era WoW knowledge is allowed. You may "
                "accurately discuss quests, zones, dungeons, mobs, items, "
                "professions, class knowledge, leveling, and mechanics.\n"
                "- Plausible level/class-appropriate history and plans may be "
                "improvised and should remain consistent, including profession "
                "history/plans and quests previously done or intended. Do not "
                "invent precise CURRENT progress or possessions.\n"
                "- If the player makes a factual claim about YOU that "
                "conflicts with your authoritative current state, do "
                "not make the claim true merely to keep the reply "
                "smooth. A natural correction, disagreement, or "
                "confused response is allowed.\n"
                "- The live-state restriction applies to factual WoW "
                "game-state claims. Harmless social details, opinions, "
                "jokes, preferences, real-world topics, and conversational "
                "personality may be improvised naturally.\n"
                "- Do not say you don't know merely because a harmless "
                "social answer is absent from live game state.\n"
                "- Supplied [[quest:...]] and [[item:...]] "
                "tokens are exact opaque strings. Copy a "
                "token exactly when relevant or omit it. "
                "Never create or modify a token.\n"
            )
    if speaker_talent_context:
        prompt += f"{speaker_talent_context}\n"
    if target_talent_context:
        prompt += f"{target_talent_context}\n"
    prompt += (
        f"Your tone: {tone}\n"
        f"Your mood: {mood}\n"
    )

    if is_rp:
        prompt += f"You are in {zone_name}."
    else:
        prompt += (
            f"You are in {zone_name}. "
            f"This location is factual gameplay context only, "
            f"not a conversation topic. Do not comment on "
            f"scenery, weather, lighting, brightness, darkness, "
            f"the sky, atmosphere, landscape, views, ambience, "
            f"or the zone's 'vibe'. Do not turn the conversation "
            f"into an observation about the area."
        )
    if is_rp and zone_flavor:
        prompt += f"\nZone context: {zone_flavor}"
    if is_rp and subzone_lore:
        prompt += (
            f"\nCurrent subzone: {subzone_lore}"
        )
    elif subzone_name:
        prompt += f"\nSubzone: {subzone_name}"
    prompt += (
        f"{rp_context}\n"
        f"{chat_history}\n\n"
    )
    if link_context:
        prompt += f"{link_context}\n\n"
    prompt += (
        f"{player_name} said in General channel:\n"
        f"\"{player_message}\"\n\n"
        f"Then {first_bot_name} responded:\n"
        f"\"{first_bot_response}\"\n\n"
        f"{style}\n"
        f"Add to the conversation - react to "
        f"{first_bot_name}'s response or add your "
        f"own take on what {player_name} said.\n"
        f"{_pick_length_hint(mode)}\n"
        f"Rules:\n"
        f"- No quotes, no emojis\n"
        f"- Normal WoW shorthand is natural: gz, ty, "
        f"np, mb, brb, afk, oom, lfg, inv, sec, omw, "
        f"dps, tank, healer, gg\n"
        f"- Occasional casual internet language like "
        f"lol, lmao, tbh, ngl, bruh, or rip is fine "
        f"when it actually fits; do not force slang "
        f"or memes into every reply\n"
        f"- NEVER use brackets [] around creature, "
        f"NPC, zone, or faction names - write them "
        f"as plain text\n"
        f"- Don't repeat what others said\n"
        f"- Stay on the subject of what {player_name} said "
        f"or {first_bot_name}'s response. Do not introduce "
        f"an unrelated topic just to make the reply interesting\n"
        f"- In Normal mode, never make scenery, weather, "
        f"lighting, brightness, the sky, atmosphere, "
        f"landscape, zone appearance, or the area's vibe "
        f"the subject of the reply. Location names are "
        f"practical gameplay context only\n"
        f"{address_hint}"
        f"- Keep it brief - General channel\n"
        f"- Reflect your personality traits"
    )
    spices = (
        pick_personality_spices(
            mode=mode,
            spice_count_override=_spice_count,
        )
        if is_rp
        else []
    )
    if spices:
        prompt += (
            "\nBackground feelings (texture, "
            "not the topic): "
            + "; ".join(spices)
        )
    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        prompt += f"\n{anti_rep}"
    prompt = append_json_instruction(
        prompt, allow_action, skip_emote=True,
        skip_action_rng=True,
    )
    return prompt


def _classify_general_mage_service(
    client,
    config,
    *,
    player_name,
    player_message,
):
    """Classify only public Mage-service discovery.

    This intentionally does not use the broad Playerbot action
    classifier. General is not a gameplay command channel.
    """
    prompt = f"""
You are classifying one World of Warcraft General chat message.

Player: {player_name}
Message: {player_message}

This classifier is ONLY for public Mage service discovery.

Allowed service values:
- water
- food
- portal
- none

Allowed canonical portal destinations:
- stormwind
- ironforge
- darnassus
- exodar
- orgrimmar
- undercity
- thunder_bluff
- silvermoon
- shattrath
- dalaran
- theramore
- stonard

Return exactly one JSON object:
{{"service":"water|food|portal|none",
  "destination":"canonical_destination_or_empty",
  "requested_level":null}}

Interpret normal WoW shorthand naturally.

Common portal language includes:
- port, portal, tele, teleport
- "port sw", "sw port", "tele IF", "org port?", "dal pls"
- A Mage plus an obvious destination can imply a portal request,
  e.g. "mage sw?" or "any mage for dal"

Common destination shorthand:
- stormwind: sw
- ironforge: if
- darnassus: darn
- exodar: exo
- orgrimmar: org, orgri
- undercity: uc
- thunder_bluff: tb, thunder bluff
- silvermoon: smc
- shattrath: shatt, shat
- dalaran: dal, dala
- theramore: thera
- stonard: ston

Normalize portal destinations to the canonical values above.
A minor typo may be understood when the intended supported
destination is clear. Never invent an unsupported destination.

Food/water shorthand is also normal:
- "water", "mage water", "need water", "drink pls"
- "food", "mage food", "bread pls", "can someone make food"
- "55 food", "food 55", "can I get 55 food?"
- "55 water", "water for 55", "lvl 55 water"

IMPORTANT NUMBER RULE:
- A bare level-like number associated with food/water means the
  requested CHARACTER LEVEL for the conjured item tier.
- Example: "55 food" means requested_level=55.
- It does NOT mean quantity 55.
- Set requested_level only for food/water.
- Valid requested_level range is 1 through 80.
- If no level is stated, use null.
- Stack/count requests such as "3 stacks water" are not a level;
  requested_level remains null.

Rules:
- water = player is asking a Mage for conjured water.
- food = player is asking a Mage for conjured food.
- portal = player is asking for a Mage portal/teleport service.
- none = discussion, jokes, statements, questions about these topics,
  combat/control requests, buffs, following, attacking, healing,
  crowd control, unrelated trading, or anything else.
- A Mage being mentioned by itself is not enough.
- For portal, destination MUST be one canonical supported destination
  above, or empty if the player did not specify where.
- For water/food, destination must be empty.
- For portal/none, requested_level must be null.
""".strip()

    raw = quick_llm_analyze(
        client,
        config,
        prompt,
        max_tokens=120,
        label='general_mage_service',
    )

    if not raw:
        return {
            'service': 'none',
            'destination': '',
            'requested_level': None,
        }

    import json

    cleaned = str(raw).strip()

    if cleaned.startswith('```'):
        cleaned = cleaned.strip('`').strip()

        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
    except (TypeError, ValueError):
        logger.warning(
            "[GEN-SERVICE] invalid classifier JSON: %r",
            raw,
        )
        return {
            'service': 'none',
            'destination': '',
            'requested_level': None,
        }

    if not isinstance(parsed, dict):
        return {
            'service': 'none',
            'destination': '',
            'requested_level': None,
        }

    service = str(
        parsed.get('service') or 'none'
    ).strip().lower()

    if service not in {
        'water',
        'food',
        'portal',
        'none',
    }:
        service = 'none'

    destination = str(
        parsed.get('destination') or ''
    ).strip().casefold()

    destination_aliases = {
        'stormwind': 'stormwind',
        'sw': 'stormwind',
        'ironforge': 'ironforge',
        'if': 'ironforge',
        'darnassus': 'darnassus',
        'darn': 'darnassus',
        'exodar': 'exodar',
        'exo': 'exodar',
        'orgrimmar': 'orgrimmar',
        'org': 'orgrimmar',
        'orgri': 'orgrimmar',
        'undercity': 'undercity',
        'uc': 'undercity',
        'thunder_bluff': 'thunder_bluff',
        'thunder bluff': 'thunder_bluff',
        'tb': 'thunder_bluff',
        'silvermoon': 'silvermoon',
        'smc': 'silvermoon',
        'shattrath': 'shattrath',
        'shatt': 'shattrath',
        'shat': 'shattrath',
        'dalaran': 'dalaran',
        'dal': 'dalaran',
        'dala': 'dalaran',
        'theramore': 'theramore',
        'thera': 'theramore',
        'stonard': 'stonard',
        'ston': 'stonard',
    }

    if service == 'portal':
        destination = destination_aliases.get(
            destination,
            '',
        )
    else:
        destination = ''

    requested_level = None

    if service in {'food', 'water'}:
        raw_level = parsed.get(
            'requested_level'
        )

        try:
            parsed_level = int(
                raw_level
            )
        except (TypeError, ValueError):
            parsed_level = 0

        if 1 <= parsed_level <= 80:
            requested_level = parsed_level

    return {
        'service': service,
        'destination': destination,
        'requested_level': requested_level,
    }


def _pick_general_service_bot(
    db,
    bot_guids,
    bot_states,
    expected_class_id,
    return_all=False,
):
    """Select exactly one C++-approved service bot deterministically."""
    candidates = []

    for raw_guid in bot_guids or []:
        try:
            guid = int(raw_guid)
        except (TypeError, ValueError):
            continue

        if guid <= 0:
            continue

        state = {}

        if isinstance(bot_states, dict):
            state = bot_states.get(
                str(guid),
                bot_states.get(guid, {}),
            )

        if not isinstance(state, dict):
            state = {}

        # C++ service payloads wrap the live snapshot beneath
        # "bot_state". Accept that authoritative shape while
        # retaining compatibility with a direct state object.
        live_state = state.get(
            'bot_state',
            state,
        )

        if not isinstance(live_state, dict):
            live_state = {}

        identity = live_state.get(
            'identity',
            {},
        )

        if not isinstance(identity, dict):
            identity = {}

        live_class = str(
            identity.get('class') or ''
        ).strip().casefold()

        expected_live_class = {
            4: 'rogue',
            8: 'mage',
        }.get(
            int(expected_class_id),
            '',
        )

        if (
            not expected_live_class
            or live_class != expected_live_class
        ):
            continue

        info = _get_bot_info(
            db,
            guid,
        )

        if not info:
            continue

        if (
            int(info.get('class') or 0)
            != int(expected_class_id)
        ):
            continue

        candidates.append({
            'guid': guid,
            'name': str(
                info.get('name') or ''
            ).strip(),
            'race': get_race_name(
                info.get('race')
            ),
            'class': get_class_name(
                info.get('class')
            ),
            'level': int(
                info.get('level') or 0
            ),
            'gender': get_gender_label(
                info.get('gender')
            ),
            'state': state,
        })

    candidates = [
        bot
        for bot in candidates
        if bot['name']
    ]

    if not candidates:
        if return_all:
            return []
        return None

    candidates.sort(
        key=lambda bot: bot['guid']
    )

    if return_all:
        return candidates

    return candidates[0]


def _pick_general_service_mage(
    db,
    bot_guids,
    bot_states,
):
    return _pick_general_service_bot(
        db,
        bot_guids,
        bot_states,
        expected_class_id=8,
    )


def _pick_general_service_rogue(
    db,
    bot_guids,
    bot_states,
):
    return _pick_general_service_bot(
        db,
        bot_guids,
        bot_states,
        expected_class_id=4,
    )


def _get_general_service_rogues(
    db,
    bot_guids,
    bot_states,
):
    return _pick_general_service_bot(
        db,
        bot_guids,
        bot_states,
        expected_class_id=4,
        return_all=True,
    )


def _build_general_service_whisper_prompt(
    *,
    bot,
    player_name,
    player_message,
    service,
    destination,
    zone_name,
    mode,
):
    """Build a private discovery response, never action confirmation."""
    identity = build_bot_identity_with_level(
        bot['name'],
        bot['race'],
        bot['class'],
        bot['level'],
        gender=bot['gender'],
    )

    factual_context = build_bot_state_context(
        bot.get('state') or {}
    )

    service_detail = service

    if service == 'portal' and destination:
        service_detail = (
            f"portal requested to: {destination}"
        )

    if service == 'lockpick':
        service_detail = (
            "lockpicking service for the exact linked lockbox"
        )

    coordination_rule = ""

    if service == 'lockpick':
        coordination_rule = (
            "- Tell the player to invite you to their party. "
            "You have a short service reservation while waiting "
            "for that party invite. "
            "Once the normal Playerbots party behavior has brought "
            "you together, the player should trade the SAME linked "
            "lockbox in the Do Not Trade slot. "
            "Do not claim it has already been unlocked.\n"
        )

    if mode == 'roleplay':
        style = (
            "Reply in-character but keep this private whisper "
            "brief and practical."
        )
    else:
        style = (
            "Reply like a real WoW player sending a quick private "
            "whisper after noticing a General chat service request. "
            "Natural fragments and WoW shorthand are fine."
        )

    prompt = (
        f"{identity}\n"
    )

    if factual_context:
        prompt += (
            f"\n{factual_context}\n"
        )

    prompt += f"""
You noticed {player_name} ask in General:
"{player_message}"

Detected public service request:
{service_detail}

You are now privately whispering {player_name}.

{style}

AUTHORITATIVE ACTION STATUS:
- This is discovery/coordination only.
- No Playerbot gameplay action has been queued, accepted, or executed.
- Do NOT claim that you already gave water or food.
- Do NOT claim that you already opened or cast a portal.
- Do NOT claim that you teleported the player.
- Do NOT claim that you already picked or unlocked a lockbox.
- Do NOT claim the service is mechanically guaranteed.
{coordination_rule}- You may acknowledge the request and coordinate the next step.
- For an unspecified portal destination, ask where they need to go.
- Keep the whisper short: normally one brief sentence.
- No quotes and no emojis.
"""

    prompt = append_json_instruction(
        prompt,
        allow_action=False,
        skip_emote=True,
        skip_action_rng=True,
    )

    return prompt


def process_general_service_request_event(
    db,
    client,
    config,
    event,
):
    """Handle one dry-run General service discovery event."""
    event_id = event['id']

    extra_data = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'player_general_service_request',
    )

    if not extra_data:
        mark_event(
            db,
            event_id,
            'skipped',
        )
        return False

    player_name = str(
        extra_data.get('player_name') or 'someone'
    ).strip()

    player_message = str(
        extra_data.get('player_message') or ''
    ).strip()

    bot_guids = extra_data.get(
        'bot_guids',
        [],
    )

    bot_states = extra_data.get(
        'bot_states',
        {},
    )

    service_hint = str(
        extra_data.get('service_hint') or ''
    ).strip().lower()

    try:
        item_entry = int(
            extra_data.get('item_entry') or 0
        )
    except (TypeError, ValueError):
        item_entry = 0

    zone_id = int(
        extra_data.get('zone_id') or 0
    )

    zone_name = str(
        extra_data.get('zone_name')
        or 'Unknown'
    ).strip()

    try:
        player_guid = int(
            event.get('subject_guid') or 0
        )
    except (TypeError, ValueError):
        player_guid = 0

    if (
        not player_name
        or not player_message
        or not player_guid
        or not zone_id
        or not bot_guids
    ):
        mark_event(
            db,
            event_id,
            'skipped',
        )
        return False

    try:
        if service_hint == 'lockpick':
            if item_entry <= 0:
                logger.warning(
                    "[GEN-SERVICE] event=%s player=%s "
                    "lockpick payload missing item_entry",
                    event_id,
                    player_name,
                )

                mark_event(
                    db,
                    event_id,
                    'skipped',
                )
                return False

            service = 'lockpick'
            destination = ''
        else:
            classification = (
                _classify_general_mage_service(
                    client,
                    config,
                    player_name=player_name,
                    player_message=player_message,
                )
            )

            service = classification['service']
            destination = classification['destination']

        if service == 'none':
            logger.info(
                "[GEN-SERVICE] event=%s player=%s "
                "message=%r result=classifier_rejected",
                event_id,
                player_name,
                player_message,
            )

            mark_event(
                db,
                event_id,
                'skipped',
            )
            return False

        reservation_id = 0

        if service == 'lockpick':
            service_bot = None

            rogue_candidates = (
                _get_general_service_rogues(
                    db,
                    bot_guids,
                    bot_states,
                )
            )

            for rogue in rogue_candidates:
                reservation_id = (
                    reserve_playerbot_service_action(
                        db,
                        player_guid=player_guid,
                        bot_guid=rogue['guid'],
                        source_channel='general',
                        action_key='lockpick_trade_item',
                        action_arg=str(item_entry),
                        target_type='player',
                        target_guid=player_guid,
                        target_name=player_name,
                        event_id=event_id,
                        waiting_seconds=120,
                    )
                )

                if reservation_id:
                    service_bot = rogue
                    break
        else:
            service_bot = _pick_general_service_mage(
                db,
                bot_guids,
                bot_states,
            )

        if not service_bot:
            logger.info(
                "[GEN-SERVICE] event=%s player=%s "
                "service=%s result=no_valid_service_bot",
                event_id,
                player_name,
                service,
            )

            mark_event(
                db,
                event_id,
                'skipped',
            )
            return False

        logger.info(
            "[PLAYERBOT-DRYRUN] event=%s player=%s "
            "channel=general service=%s destination=%r "
            "resolved=[%s:%s] reservation=%s gameplay=none",
            event_id,
            player_name,
            service,
            destination,
            service_bot['guid'],
            service_bot['name'],
            reservation_id or 0,
        )

        mode = get_chatter_mode(
            config
        )

        prompt = (
            _build_general_service_whisper_prompt(
                bot=service_bot,
                player_name=player_name,
                player_message=player_message,
                service=service,
                destination=destination,
                zone_name=zone_name,
                mode=mode,
            )
        )

        max_tokens = min(
            int(
                config.get(
                    'LLMChatter.MaxTokens',
                    200,
                )
            ),
            120,
        )

        response = call_llm(
            client,
            prompt,
            config,
            max_tokens_override=max_tokens,
            context=(
                f"general-service:#{event_id}:"
                f"{service_bot['name']}"
            ),
            label='general_service_reply',
            metadata={
                'zone_id': zone_id,
                'zone_name': zone_name,
                'service': service,
            },
        )

        if not response:
            mark_event(
                db,
                event_id,
                'skipped',
            )
            return False

        parsed = parse_single_response(
            response
        )

        message = strip_speaker_prefix(
            parsed.get('message', ''),
            service_bot['name'],
        )

        message = cleanup_message(
            message,
            action=None,
        )

        if not message:
            mark_event(
                db,
                event_id,
                'skipped',
            )
            return False

        if len(message) > 255:
            message = (
                message[:252]
                + '...'
            )

        delay = min(
            calculate_dynamic_delay(
                len(message),
                config,
                prev_message_length=len(
                    player_message
                ),
                responsive=True,
            ),
            5.0,
        )

        insert_chat_message(
            db,
            service_bot['guid'],
            service_bot['name'],
            message,
            channel='whisper',
            delay_seconds=delay,
            event_id=event_id,
            player_guid=player_guid,
            config=config,
            delivery_policy='responsive',
            delivery_reason=(
                'general_service_discovery'
            ),
            owner_subsystem='general_service',
        )

        mark_event(
            db,
            event_id,
            'completed',
        )

        return True

    except Exception:
        fail_event(
            db,
            event_id,
            'player_general_service_request',
            'handler error',
        )
        return False


def process_general_player_msg_event(
    event, db, client, config
):
    """Handle a player_general_msg event.

    A real player said something in General channel.
    Pick 1-2 bots from the zone to respond.
    """
    event_id = event['id']
    extra_data = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'player_general_msg'
    )

    if not extra_data:
        mark_event(db, event_id, 'skipped')
        return False

    player_name = extra_data.get(
        'player_name', 'someone'
    )
    player_message = extra_data.get(
        'player_message', ''
    )
    bot_guids = extra_data.get('bot_guids', [])
    bot_names = extra_data.get('bot_names', [])

    # Resolve zone from player's location
    zctx = _resolve_zone_context(
        db, player_name, extra_data
    )
    zone_id = zctx['zone_id']
    zone_name = zctx['zone_name']
    zone_flavor = zctx['zone_flavor']
    zone_meta = zctx['zone_meta']
    subzone_name = ''
    subzone_lore = ''

    if not zone_id or not player_message:
        mark_event(db, event_id, 'skipped')
        return False

    # Parse and resolve WoW links in message
    link_context = ""
    player_message, link_context = (
        resolve_and_format_links(
            config, player_message
        )
    )

    if not bot_guids:
        mark_event(db, event_id, 'skipped')
        return False

    try:
        mode = get_chatter_mode(config)

        # Fetch recent messages for anti-repetition
        recent_msgs = get_recent_zone_messages(
            db, zone_id
        )

        # Fetch chat history for this zone
        history = _get_general_chat_history(
            db, zone_id
        )
        chat_hist = _format_general_history(history)

        routing_bot_states = extra_data.get(
            'bot_states',
            {},
        )

        # Explicit recipient beats current-state inference.
        primary = _select_primary_bot(
            db, client, config, bot_guids,
            bot_names, player_name,
            player_message, mode,
            chat_hist=chat_hist,
            bot_states=routing_bot_states,
        )
        if not primary:
            mark_event(db, event_id, 'skipped')
            return False

        bot1_guid = primary['bot1_guid']
        bot1_idx = primary['bot1_idx']
        bot1_name = primary['bot1_name']
        bot1_race = primary['bot1_race']
        bot1_class = primary['bot1_class']
        bot1_class_id = primary['bot1_class_id']
        bot1_level = primary['bot1_level']
        bot1_gender = primary['bot1_gender']
        bot1_traits = primary['bot1_traits']
        is_conversation = primary['is_conversation']

        player_info = get_character_info_by_name(
            db, player_name,
        )
        player_guid = (
            int(player_info.get('guid') or 0)
            if player_info
            else 0
        )

        # Live authoritative state was captured in C++
        # when the player sent the General message.
        bot_states = extra_data.get('bot_states', {})
        bot1_state = {}

        if isinstance(bot_states, dict):
            bot1_state = bot_states.get(
                str(bot1_guid),
                bot_states.get(bot1_guid, {})
            )

        if not isinstance(bot1_state, dict):
            bot1_state = {}

        # Talent context injection
        speaker_talent = None
        target_talent = None
        talent_chance = int(config.get(
            'LLMChatter.TalentInjectionChance',
            '40',
        ))
        if (
            talent_chance > 0
            and random.randint(1, 100)
            <= talent_chance
        ):
            speaker_talent = build_talent_context(
                db, bot1_guid,
                bot1_class_id,
                bot1_name,
                perspective='speaker',
            )
        if (
            talent_chance > 0
            and random.randint(1, 100)
            <= talent_chance
        ):
            pinfo = get_character_info_by_name(
                db, player_name,
            )
            if pinfo:
                target_talent = (
                    build_talent_context(
                        db, pinfo['guid'],
                        pinfo['class'],
                        player_name,
                        perspective='target',
                    )
                )

        # Build and send first bot prompt
        allow_action = (mode == 'roleplay')
        prompt1 = _build_general_response_prompt(
            bot1_name, bot1_race, bot1_class,
            bot1_level, bot1_gender, bot1_traits,
            player_name, player_message,
            zone_name, chat_hist, mode,
            recent_messages=recent_msgs,
            allow_action=allow_action,
            link_context=link_context,
            bot_state=bot1_state,
            speaker_talent_context=speaker_talent,
            target_talent_context=target_talent,
            zone_flavor=zone_flavor,
            subzone_name=subzone_name,
            subzone_lore=subzone_lore,
        )

        relationship_context = ""

        if (
            int(config.get(
                'LLMChatter.Memory.Enable', 1
            ))
            and player_guid
        ):
            from chatter_memory import (
                get_relationship_memory_context,
            )

            relationship_context = (
                get_relationship_memory_context(
                    db,
                    bot1_guid,
                    player_guid,
                    player_name,
                    count=4,
                )
            )

        if relationship_context:
            prompt1 = (
                relationship_context
                + "\n\n"
                + prompt1
            )

        max_tokens = int(config.get(
            'LLMChatter.MaxTokens', 200
        ))
        if speaker_talent:
            zone_meta['speaker_talent'] = (
                speaker_talent
            )
        if target_talent:
            zone_meta['target_talent'] = (
                target_talent
            )
        response1 = call_llm(
            client, prompt1, config,
            max_tokens_override=max_tokens,
            context=(
                f"gen-msg:#{event_id}"
                f":{bot1_name}"
            ),
            label='general_player_msg',
            metadata=zone_meta,
        )

        if not response1:
            mark_event(db, event_id, 'skipped')
            return False

        parsed1 = parse_single_response(response1)
        if (parsed1.get('action')
                and not should_include_action()):
            parsed1['action'] = None
        msg1 = strip_speaker_prefix(
            parsed1['message'], bot1_name
        )
        msg1 = cleanup_message(
            msg1, action=parsed1.get('action')
        )
        if not msg1:
            mark_event(db, event_id, 'skipped')
            return False
        if len(msg1) > 255:
            msg1 = msg1[:252] + "..."


        # Queue first bot's message â€” responsive
        # since player is waiting for a reply.
        # Skip zone gap: the player asked a direct
        # question and is actively waiting. Cap at
        # 5s so the reply feels conversational.
        delay1 = min(
            calculate_dynamic_delay(
                len(msg1), config, responsive=True,
            ),
            5.0,
        )
        conv_label = "conv" if is_conversation else "stmt"
        logger.info(
            "[GEN-FLOW] player-react %s | "
            "bot=%s delay=%.1fs seq=0",
            conv_label, bot1_name, delay1,
        )
        # General channel: skip emotes
        # (proximity-based, not visible
        #  to zone-wide recipients)
        insert_chat_message(
            db, bot1_guid, bot1_name, msg1,
            channel='general',
            delay_seconds=delay1,
            event_id=event_id,
            sequence=0,
        )
        maybe_queue_group_general_reaction(
            db, config,
            bot1_guid, bot1_name, msg1,
            zone_id, int(event.get('map_id') or 0),
            source_event_id=event_id,
            source_sequence=0,
            source_delay_seconds=delay1,
        )

        # Store in General chat history
        _store_general_chat(
            db, zone_id, bot1_name, True, msg1
        )

        if player_guid:
            from chatter_memory import (
                queue_relationship_memory,
            )

            queue_relationship_memory(
                config,
                bot1_guid,
                player_guid,
                event_context=(
                    f"{player_name}: "
                    f"{player_message[:250]}\n"
                    f"{bot1_name}: {msg1[:250]}"
                ),
                source='general_player',
                bot_name=str(bot1_name),
                player_name=str(player_name),
            )

        # Conversation mode: second bot follows up
        if is_conversation:
            try:
                followup = _general_followup(
                    db, client, config,
                    event_id, zone_id, zone_name,
                    bot_guids, bot1_idx, bot1_guid,
                    bot1_name, msg1,
                    player_name, player_message,
                    mode, delay1,
                    recent_msgs=recent_msgs,
                    allow_action=allow_action,
                    link_context=link_context,
                    bot_states=bot_states,
                    speaker_talent_context=(
                        speaker_talent
                    ),
                    target_talent_context=(
                        target_talent
                    ),
                    zone_flavor=zone_flavor,
                    subzone_name=subzone_name,
                    subzone_lore=subzone_lore,
                    zone_meta=zone_meta,
                )
                # Extended conversation chance
                if (
                    followup
                    and _extended_conv_chance > 0
                    and random.randint(1, 100)
                    <= _extended_conv_chance
                ):
                    try:
                        _general_extended_conversation(
                            db, client, config,
                            event_id, zone_id,
                            zone_name,
                            bot_guids,
                            bot1_guid, bot1_name,
                            bot1_traits,
                            msg1,
                            followup['bot2_guid'],
                            followup['bot2_name'],
                            followup['bot2_traits'],
                            followup['bot2_response'],
                            player_name,
                            player_message,
                            mode,
                            followup['delay2'],
                            recent_msgs=recent_msgs,
                            allow_action=allow_action,
                            link_context=link_context,
                            bot_states=bot_states,
                            speaker_talent_context=(
                                speaker_talent
                            ),
                            target_talent_context=(
                                target_talent
                            ),
                            zone_flavor=zone_flavor,
                            subzone_name=subzone_name,
                            subzone_lore=subzone_lore,
                            zone_meta=zone_meta,
                        )
                    except Exception as e3:
                        logger.error(
                            "[GEN] extended conv "
                            "failed event=%s: %s",
                            event_id, e3,
                            exc_info=True
                        )
            except Exception as e2:
                logger.error(
                    "[GEN] followup failed "
                    "event=%s: %s",
                    event_id, e2,
                    exc_info=True
                )

        mark_event(db, event_id, 'completed')
        return True

    except Exception:
        fail_event(
            db, event_id,
            'player_general_msg', 'handler error',
        )
        return False


def _general_followup(
    db, client, config,
    event_id, zone_id, zone_name,
    bot_guids, bot1_idx, bot1_guid,
    bot1_name, bot1_response,
    player_name, player_message,
    mode, delay1,
    recent_msgs=None,
    allow_action=True,
    link_context="",
    bot_states=None,
    speaker_talent_context=None,
    target_talent_context=None,
    zone_flavor="",
    subzone_name="",
    subzone_lore="",
    zone_meta=None,
):
    """Generate a second bot's followup response
    in General channel conversation mode.
    """
    # Pick a different bot
    other_guids = [
        int(g) for i, g in enumerate(bot_guids)
        if i != bot1_idx
    ]
    if not other_guids:
        return

    bot2_guid = random.choice(other_guids)

    # Match the second speaker to the authoritative
    # live state captured when the player spoke.
    bot2_state = {}
    if isinstance(bot_states, dict):
        bot2_state = bot_states.get(
            str(bot2_guid),
            bot_states.get(bot2_guid, {})
        )

    if not isinstance(bot2_state, dict):
        bot2_state = {}

    bot2_info = _get_bot_info(db, bot2_guid)
    if not bot2_info:
        return

    bot2_name = bot2_info['name']
    bot2_race = get_race_name(bot2_info['race'])
    bot2_class = get_class_name(bot2_info['class'])
    bot2_level = bot2_info['level']
    bot2_gender = get_gender_label(bot2_info['gender'])
    bot2_traits = _pick_random_traits()

    player_info = get_character_info_by_name(
        db, player_name,
    )
    player_guid = (
        int(player_info.get('guid') or 0)
        if player_info
        else 0
    )

    # Recompute speaker talent for bot2
    bot2_speaker_talent = None
    talent_chance = int(config.get(
        'LLMChatter.TalentInjectionChance',
        '40',
    ))
    if (
        talent_chance > 0
        and random.randint(1, 100)
        <= talent_chance
    ):
        bot2_speaker_talent = build_talent_context(
            db, bot2_guid,
            bot2_info['class'],
            bot2_name,
            perspective='speaker',
        )

    # Get updated history (includes first response)
    history = _get_general_chat_history(db, zone_id)
    chat_hist = _format_general_history(history)

    prompt2 = _build_general_followup_prompt(
        bot2_name, bot2_race, bot2_class,
        bot2_level, bot2_gender, bot2_traits,
        bot1_name, bot1_response,
        player_name, player_message,
        zone_name, chat_hist, mode,
        recent_messages=recent_msgs,
        allow_action=allow_action,
        link_context=link_context,
        bot_state=bot2_state,
        speaker_talent_context=(
            bot2_speaker_talent
        ),
        target_talent_context=(
            target_talent_context
        ),
        zone_flavor=zone_flavor,
        subzone_name=subzone_name,
        subzone_lore=subzone_lore,
    )

    if int(config.get(
        'LLMChatter.Memory.Enable', 1
    )):
        from chatter_memory import (
            get_relationship_memory_context,
        )

        relationship_blocks = []

        if player_guid:
            player_memory = (
                get_relationship_memory_context(
                    db,
                    bot2_guid,
                    player_guid,
                    player_name,
                    count=4,
                )
            )

            if player_memory:
                relationship_blocks.append(
                    player_memory
                )

        bot1_memory = (
            get_relationship_memory_context(
                db,
                bot2_guid,
                bot1_guid,
                bot1_name,
                count=3,
            )
        )

        if bot1_memory:
            relationship_blocks.append(
                bot1_memory
            )

        if relationship_blocks:
            prompt2 = (
                "\n\n".join(
                    relationship_blocks
                )
                + "\n\n"
                + prompt2
            )

    max_tokens = int(config.get(
        'LLMChatter.MaxTokens', 200
    ))
    if zone_meta is None:
        zone_meta = {}
    if bot2_speaker_talent:
        zone_meta['speaker_talent'] = (
            bot2_speaker_talent
        )
    if target_talent_context:
        zone_meta['target_talent'] = (
            target_talent_context
        )
    response2 = call_llm(
        client, prompt2, config,
        max_tokens_override=max_tokens,
        context=f"gen-followup:{bot2_name}",
        label='general_followup',
        metadata=zone_meta,
    )
    if not response2:
        return

    parsed2 = parse_single_response(response2)
    if (parsed2.get('action')
            and not should_include_action()):
        parsed2['action'] = None
    msg2 = strip_speaker_prefix(
        parsed2['message'], bot2_name
    )
    msg2 = cleanup_message(
        msg2, action=parsed2.get('action')
    )
    if not msg2:
        return
    if len(msg2) > 255:
        msg2 = msg2[:252] + "..."


    # Stagger: first bot delay + responsive gap
    delay2 = delay1 + random.randint(2, 5)

    logger.info(
        "[GEN-FLOW] player-react followup | "
        "bot=%s delay=%.1fs seq=1 (gap=%.1fs)",
        bot2_name, delay2, delay2 - delay1,
    )
    # General channel: skip emotes
    insert_chat_message(
        db, bot2_guid, bot2_name, msg2,
        channel='general',
        delay_seconds=delay2,
        event_id=event_id,
        sequence=1,
    )
    maybe_queue_group_general_reaction(
        db, config,
        bot2_guid, bot2_name, msg2,
        zone_id, 0,
        source_event_id=event_id,
        source_sequence=1,
        source_delay_seconds=delay2,
    )

    # Push zone timestamp past bot2's delivery so
    # the gap enforced from the END of the exchange.
    _zone_last_delivery[zone_id] = (
        time.monotonic() + delay2
    )

    # Store in General chat history
    _store_general_chat(
        db, zone_id, bot2_name, True, msg2
    )

    if int(config.get(
        'LLMChatter.Memory.Enable', 1
    )):
        from chatter_memory import (
            queue_relationship_memory,
        )

        if player_guid:
            queue_relationship_memory(
                config,
                bot2_guid,
                player_guid,
                event_context=(
                    f"{player_name}: "
                    f"{player_message[:220]}\n"
                    f"{bot2_name}: {msg2[:220]}"
                ),
                source='general_player',
                bot_name=str(bot2_name),
                player_name=str(player_name),
            )

        transcript = (
            f"{player_name}: "
            f"{player_message[:180]}\n"
            f"{bot1_name}: "
            f"{bot1_response[:180]}\n"
            f"{bot2_name}: {msg2[:180]}"
        )

        queue_relationship_memory(
            config,
            bot2_guid,
            bot1_guid,
            event_context=transcript,
            source='general_bot',
            bot_name=str(bot2_name),
            player_name=str(bot1_name),
        )

        queue_relationship_memory(
            config,
            bot1_guid,
            bot2_guid,
            event_context=transcript,
            source='general_bot',
            bot_name=str(bot1_name),
            player_name=str(bot2_name),
        )

    return {
        'bot2_guid': bot2_guid,
        'bot2_name': bot2_name,
        'bot2_traits': bot2_traits,
        'bot2_response': msg2,
        'delay2': delay2,
    }


def _build_general_continuation_prompt(
    bot_name, bot_race, bot_class, bot_level,
    bot_gender,
    traits, conversation_thread,
    zone_name, chat_history, mode,
    recent_messages=None, allow_action=True,
    remaining_messages=3, link_context="",
    bot_state=None,
    speaker_talent_context=None,
    target_talent_context=None,
    zone_flavor="",
    subzone_name="",
    subzone_lore="",
):
    """Build prompt for a continuation message in
    an extended General channel conversation.

    conversation_thread is a list of dicts:
      [{'name': str, 'message': str, 'is_bot': bool}]
    """
    is_rp = (mode == 'roleplay')
    trait_str = ', '.join(traits)
    tone = pick_random_tone(mode)
    mood = pick_random_mood(mode)

    rp_context = ""
    if is_rp:
        ctx = build_race_class_context(
            bot_race, bot_class
        )
        if ctx:
            rp_context = f"\n{ctx}"

        profile = RACE_SPEECH_PROFILES.get(bot_race)
        if profile:
            fw = profile.get('flavor_words', [])
            flavor = ', '.join(
                random.sample(fw, min(3, len(fw)))
            )
            if flavor:
                rp_context += (
                    f"\nRace flavor words you might "
                    f"use: {flavor}"
                )

    if is_rp:
        style = (
            "Reply in-character. Stay natural and "
            "grounded."
        )
    else:
        style = (
            "Reply like a real WoW player casually "
            "typing in General chat while playing. "
            "Keep it conversational and appropriate for public chat. "
            "Different players type differently: some use complete "
            "sentences, normal capitalization, and punctuation; others "
            "may use lowercase, fragments, abbreviations, WoW shorthand, "
            "or occasional internet slang. These are optional styles, "
            "not a shared voice for every speaker. "
            "As a conversation develops, answer substantive or clarifying "
            "questions with enough detail to move the thread forward "
            "instead of repeatedly giving generic throwaway replies. "
            "A dry joke or mildly salty comment is fine, but do not force "
            "memes, snark, or trash talk. Do not sound like an NPC, lore "
            "writer, Reddit essay, or scripted comedian."
        )

    # Format the conversation thread
    thread_lines = []
    for entry in conversation_thread:
        tag = "" if entry['is_bot'] else " (player)"
        thread_lines.append(
            f"  {entry['name']}{tag}: "
            f"{entry['message']}"
        )
    thread_text = "\n".join(thread_lines)

    # Explicit name-addressing is mainly an RP feature.
    other_names = list(set(
        e['name'] for e in conversation_thread
        if e['name'] != bot_name
    ))
    address_hint = ""
    if (
        is_rp
        and other_names
        and random.random() < 0.4
    ):
        target = random.choice(other_names)
        address_hint = (
            f"- You may address {target} by "
            f"name in your reply\n"
        )

    identity = build_bot_identity_with_level(
        bot_name,
        bot_race,
        bot_class,
        bot_level,
        gender=bot_gender,
    )
    prompt = (
        f"{identity}\n"
        f"Your personality: {trait_str}\n"
    )

    factual_context = build_bot_state_context(
        bot_state or {}
    )
    if factual_context:
        prompt += f"\n{factual_context}\n"

        if not is_rp:
            prompt += (
                "\nLIVE STATE RULES:\n"
                "- Authoritative live state overrides prior context only "
                "when they conflict about CURRENT observable/mechanical "
                "character state. Absence from live state is not proof that "
                "the character lacks general WoW knowledge or past history.\n"
                "- Keep CURRENT state grounded: exact quest progress/counts, "
                "current inventory/equipment/money, exact current location or "
                "activity, nearby/local-world observations, and actual "
                "spell/service capability must come from authoritative context.\n"
                "- General Wrath-era WoW knowledge is allowed. You may "
                "accurately discuss quests, zones, dungeons, mobs, items, "
                "professions, class knowledge, leveling, and mechanics.\n"
                "- Plausible level/class-appropriate history and plans may be "
                "improvised and should remain consistent, including profession "
                "history/plans and quests previously done or intended. Do not "
                "invent precise CURRENT progress or possessions.\n"
                "- The live-state restriction applies to factual WoW "
                "game-state claims. Harmless social details, opinions, "
                "jokes, preferences, real-world topics, and conversational "
                "personality may be improvised naturally.\n"
                "- Do not say you don't know merely because a harmless "
                "social answer is absent from live game state.\n"
                "- Supplied [[quest:...]] and [[item:...]] "
                "tokens are exact opaque strings. Copy a "
                "token exactly when relevant or omit it. "
                "Never create or modify a token.\n"
            )
    if speaker_talent_context:
        prompt += f"{speaker_talent_context}\n"
    if target_talent_context:
        prompt += f"{target_talent_context}\n"
    prompt += (
        f"Your tone: {tone}\n"
        f"Your mood: {mood}\n"
    )

    if is_rp:
        prompt += f"You are in {zone_name}."
    else:
        prompt += (
            f"You are in {zone_name}. "
            f"This location is factual gameplay context only, "
            f"not a conversation topic. Do not comment on "
            f"scenery, weather, lighting, brightness, darkness, "
            f"the sky, atmosphere, landscape, views, ambience, "
            f"or the zone's 'vibe'. Do not turn the conversation "
            f"into an observation about the area."
        )
    if is_rp and zone_flavor:
        prompt += f"\nZone context: {zone_flavor}"
    if is_rp and subzone_lore:
        prompt += (
            f"\nCurrent subzone: {subzone_lore}"
        )
    elif subzone_name:
        prompt += f"\nSubzone: {subzone_name}"
    prompt += (
        f"{rp_context}\n"
        f"{chat_history}\n\n"
    )
    if link_context:
        prompt += f"{link_context}\n\n"
    prompt += (
        f"A conversation is happening in "
        f"General channel:\n"
        f"{thread_text}\n\n"
        f"{style}\n"
        f"Continue the conversation naturally. "
        f"React to what was just said or add "
        f"your own perspective.\n"
        f"{_pick_length_hint(mode)}\n"
        f"Rules:\n"
        f"- No quotes, no emojis\n"
        f"- Normal WoW shorthand is natural: gz, ty, "
        f"np, mb, brb, afk, oom, lfg, inv, sec, omw, "
        f"dps, tank, healer, gg\n"
        f"- Occasional casual internet language like "
        f"lol, lmao, tbh, ngl, bruh, or rip is fine "
        f"when it actually fits; do not force slang "
        f"or memes into every reply\n"
        f"- NEVER use brackets [] around creature, "
        f"NPC, zone, or faction names - write them "
        f"as plain text\n"
        f"- Don't repeat what others said\n"
        f"- Keep established factual claims consistent across follow-ups "
        f"unless authoritative live state actually changes\n"
        f"- If someone asks for clarification, become more specific when "
        f"supported instead of retreating to a generic answer\n"
        f"- Stay on the subject of the current conversation. "
        f"Do not introduce an unrelated topic just to make "
        f"the reply interesting\n"
        f"- In Normal mode, never make scenery, weather, "
        f"lighting, brightness, the sky, atmosphere, "
        f"landscape, zone appearance, or the area's vibe "
        f"the subject of the reply. Location names are "
        f"practical gameplay context only\n"
        f"{address_hint}"
        f"- Keep it brief - General channel\n"
        f"- Reflect your personality traits"
    )
    if is_rp and remaining_messages <= 2:
        prompt += (
            f"\n- The conversation should feel "
            f"like it's winding down naturally"
        )
    spices = pick_personality_spices(
        mode=mode, spice_count_override=_spice_count
    )
    if spices:
        prompt += (
            "\nBackground feelings (texture, "
            "not the topic): "
            + "; ".join(spices)
        )
    anti_rep = build_anti_repetition_context(
        recent_messages
    )
    if anti_rep:
        prompt += f"\n{anti_rep}"
    prompt = append_json_instruction(
        prompt, allow_action, skip_emote=True,
        skip_action_rng=True,
    )
    return prompt


def _general_extended_conversation(
    db, client, config,
    event_id, zone_id, zone_name,
    bot_guids,
    bot1_guid, bot1_name, bot1_traits,
    bot1_response,
    bot2_guid, bot2_name, bot2_traits,
    bot2_response,
    player_name, player_message,
    mode, last_delay,
    recent_msgs=None,
    allow_action=True,
    link_context="",
    bot_states=None,
    speaker_talent_context=None,
    target_talent_context=None,
    zone_flavor="",
    subzone_name="",
    subzone_lore="",
    zone_meta=None,
):
    """Generate additional messages beyond the
    initial 2-message conversation in General
    channel. Bots alternate with diminishing
    continuation chance.
    """
    # Diminishing chances per additional message
    continuation_chances = [70, 50, 30]

    # Build conversation thread so far
    thread = [
        {
            'name': player_name,
            'message': player_message,
            'is_bot': False,
        },
        {
            'name': bot1_name,
            'message': bot1_response,
            'is_bot': True,
        },
        {
            'name': bot2_name,
            'message': bot2_response,
            'is_bot': True,
        },
    ]

    # Participating bots: bot1 and bot2 always,
    # optionally a 3rd joins
    participants = [
        {
            'guid': bot1_guid,
            'name': bot1_name,
            'traits': bot1_traits,
            'bot_state': (
                bot_states.get(
                    str(bot1_guid),
                    bot_states.get(bot1_guid, {})
                )
                if isinstance(bot_states, dict)
                else {}
            ),
        },
        {
            'guid': bot2_guid,
            'name': bot2_name,
            'traits': bot2_traits,
            'bot_state': (
                bot_states.get(
                    str(bot2_guid),
                    bot_states.get(bot2_guid, {})
                )
                if isinstance(bot_states, dict)
                else {}
            ),
        },
    ]

    # Maybe add a 3rd bot (50% chance if available)
    other_guids = [
        int(g) for i, g in enumerate(bot_guids)
        if int(g) not in (bot1_guid, bot2_guid)
    ]
    if other_guids and random.random() < 0.5:
        bot3_guid = random.choice(other_guids)
        bot3_info = _get_bot_info(db, bot3_guid)
        if bot3_info:
            bot3_state = {}
            if isinstance(bot_states, dict):
                bot3_state = bot_states.get(
                    str(bot3_guid),
                    bot_states.get(bot3_guid, {})
                )

            if not isinstance(bot3_state, dict):
                bot3_state = {}

            participants.append({
                'guid': bot3_guid,
                'name': bot3_info['name'],
                'traits': _pick_random_traits(),
                'bot_state': bot3_state,
            })

    # Track who spoke last to avoid repeats
    last_speaker_guid = bot2_guid
    # Messages sent so far (bot1 + bot2 = 2)
    msg_count = 2
    current_delay = last_delay
    max_msgs = _extended_max_messages

    max_tokens = int(config.get(
        'LLMChatter.MaxTokens', 200
    ))

    player_info = get_character_info_by_name(
        db, player_name,
    )
    player_guid = (
        int(player_info.get('guid') or 0)
        if player_info
        else 0
    )

    # cont_turn tracks how many continuation
    # RNG rolls we've made (0-indexed).
    # First continuation (turn 0) is guaranteed.
    cont_turn = 0

    while msg_count < max_msgs:
        # First extra message is guaranteed;
        # subsequent ones use diminishing chance
        if cont_turn > 0:
            chance_idx = min(
                cont_turn - 1,
                len(continuation_chances) - 1,
            )
            roll = random.randint(1, 100)
            if roll > continuation_chances[chance_idx]:
                break

        # Pick next speaker (not the last one)
        eligible = [
            p for p in participants
            if p['guid'] != last_speaker_guid
        ]
        if not eligible:
            break
        speaker = random.choice(eligible)

        # Fetch bot info for prompt
        sp_info = _get_bot_info(
            db, speaker['guid']
        )
        if not sp_info:
            participants = [
                p for p in participants
                if p['guid'] != speaker['guid']
            ]
            # Don't consume a turn â€” retry
            continue

        sp_race = get_race_name(sp_info['race'])
        sp_class = get_class_name(sp_info['class'])
        sp_level = sp_info['level']
        sp_gender = get_gender_label(sp_info['gender'])

        # Recompute speaker talent for this bot
        sp_speaker_talent = None
        talent_chance = int(config.get(
            'LLMChatter.TalentInjectionChance',
            '40',
        ))
        if (
            talent_chance > 0
            and random.randint(1, 100)
            <= talent_chance
        ):
            sp_speaker_talent = (
                build_talent_context(
                    db, speaker['guid'],
                    sp_info['class'],
                    speaker['name'],
                    perspective='speaker',
                )
            )

        # Get updated history
        history = _get_general_chat_history(
            db, zone_id
        )
        chat_hist = _format_general_history(history)

        # remaining after this message is sent
        remaining = max_msgs - (msg_count + 1)
        prompt = _build_general_continuation_prompt(
            speaker['name'], sp_race, sp_class,
            sp_level, sp_gender, speaker['traits'],
            thread, zone_name, chat_hist, mode,
            recent_messages=recent_msgs,
            allow_action=allow_action,
            remaining_messages=remaining,
            link_context=link_context,
            bot_state=speaker.get('bot_state', {}),
            speaker_talent_context=(
                sp_speaker_talent
            ),
            target_talent_context=(
                target_talent_context
            ),
            zone_flavor=zone_flavor,
            subzone_name=subzone_name,
            subzone_lore=subzone_lore,
        )

        if int(config.get(
            'LLMChatter.Memory.Enable', 1
        )):
            from chatter_memory import (
                get_relationship_memory_context,
            )

            relationship_blocks = []

            if player_guid:
                player_memory = (
                    get_relationship_memory_context(
                        db,
                        speaker['guid'],
                        player_guid,
                        player_name,
                        count=3,
                    )
                )

                if player_memory:
                    relationship_blocks.append(
                        player_memory
                    )

            spoken_bot_names = {
                entry['name']
                for entry in thread
                if entry.get('is_bot')
            }

            for other in participants:
                if (
                    other['guid'] == speaker['guid']
                    or other['name']
                    not in spoken_bot_names
                ):
                    continue

                bot_memory = (
                    get_relationship_memory_context(
                        db,
                        speaker['guid'],
                        other['guid'],
                        other['name'],
                        count=2,
                    )
                )

                if bot_memory:
                    relationship_blocks.append(
                        bot_memory
                    )

            if relationship_blocks:
                prompt = (
                    "\n\n".join(
                        relationship_blocks
                    )
                    + "\n\n"
                    + prompt
                )

        if zone_meta is None:
            zone_meta = {}
        if sp_speaker_talent:
            zone_meta['speaker_talent'] = (
                sp_speaker_talent
            )
        else:
            zone_meta.pop(
                'speaker_talent', None
            )
        if target_talent_context:
            zone_meta['target_talent'] = (
                target_talent_context
            )
        response = call_llm(
            client, prompt, config,
            max_tokens_override=max_tokens,
            context=(
                f"gen-extended:{speaker['name']}"
            ),
            label='general_conv',
            metadata=zone_meta,
        )
        if not response:
            break

        parsed = parse_single_response(response)
        if (parsed.get('action')
                and not should_include_action()):
            parsed['action'] = None
        msg = strip_speaker_prefix(
            parsed['message'], speaker['name']
        )
        msg = cleanup_message(
            msg, action=parsed.get('action')
        )
        if not msg:
            break
        if len(msg) > 255:
            msg = msg[:252] + "..."

        msg_count += 1
        prev_delay = current_delay
        current_delay += random.randint(2, 5)

        logger.info(
            "[GEN-FLOW] extended conv | "
            "bot=%s delay=%.1fs seq=%d "
            "(gap=%.1fs)",
            speaker['name'], current_delay,
            msg_count - 1,
            current_delay - prev_delay,
        )
        insert_chat_message(
            db, speaker['guid'],
            speaker['name'], msg,
            channel='general',
            delay_seconds=current_delay,
            event_id=event_id,
            sequence=msg_count - 1,
        )
        maybe_queue_group_general_reaction(
            db, config,
            speaker['guid'], speaker['name'], msg,
            zone_id, 0,
            source_event_id=event_id,
            source_sequence=msg_count - 1,
            source_delay_seconds=current_delay,
        )

        _store_general_chat(
            db, zone_id,
            speaker['name'], True, msg
        )

        if int(config.get(
            'LLMChatter.Memory.Enable', 1
        )):
            from chatter_memory import (
                queue_relationship_memory,
            )

            if player_guid:
                queue_relationship_memory(
                    config,
                    speaker['guid'],
                    player_guid,
                    event_context=(
                        f"{player_name}: "
                        f"{player_message[:220]}\n"
                        f"{speaker['name']}: "
                        f"{msg[:220]}"
                    ),
                    source='general_player',
                    bot_name=str(
                        speaker['name']
                    ),
                    player_name=str(
                        player_name
                    ),
                )

            # Pair-focused bot memories. Only bots that
            # already spoke in this actual conversation
            # are eligible counterparts.
            prior_bot_lines = {}

            for entry in thread:
                if not entry.get('is_bot'):
                    continue

                prior_bot_lines[
                    entry['name']
                ] = entry['message']

            for target in participants:
                if (
                    target['guid']
                    == speaker['guid']
                    or target['name']
                    not in prior_bot_lines
                ):
                    continue

                target_line = str(
                    prior_bot_lines[
                        target['name']
                    ]
                )

                pair_context = (
                    f"{player_name}: "
                    f"{player_message[:160]}\n"
                    f"{target['name']}: "
                    f"{target_line[:180]}\n"
                    f"{speaker['name']}: "
                    f"{msg[:180]}"
                )

                queue_relationship_memory(
                    config,
                    speaker['guid'],
                    target['guid'],
                    event_context=pair_context,
                    source='general_bot',
                    bot_name=str(
                        speaker['name']
                    ),
                    player_name=str(
                        target['name']
                    ),
                )

                queue_relationship_memory(
                    config,
                    target['guid'],
                    speaker['guid'],
                    event_context=pair_context,
                    source='general_bot',
                    bot_name=str(
                        target['name']
                    ),
                    player_name=str(
                        speaker['name']
                    ),
                )

        # Update thread and last speaker
        thread.append({
            'name': speaker['name'],
            'message': msg,
            'is_bot': True,
        })
        last_speaker_guid = speaker['guid']
        cont_turn += 1
