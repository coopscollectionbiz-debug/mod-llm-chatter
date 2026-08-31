"""Bridge handler for screenshot vision observations.

Stage 2: receives structured vision description from the host-side
screenshot agent, wraps it in bot personality and zone context, and
generates a natural in-character party chat comment.

Supports both single-bot statements and multi-bot conversations
using the same RNG-gated pattern as nearby object events.
"""

import random

from chatter_db import (
    fail_event,
    get_group_location,
    insert_chat_message,
)
from chatter_party_gate import (
    defer_event_for_party_gate,
    should_defer_party_generation,
)
from chatter_group_state import (
    _get_recent_chat,
    format_chat_history,
    get_group_members,
    _mark_event,
    _store_chat,
)
from chatter_llm import call_llm
from chatter_shared import (
    append_conversation_json_instruction,
    build_anti_repetition_context,
    build_bot_identity,
    build_bot_identity_with_level,
    calculate_dynamic_delay,
    get_class_name,
    get_gender_label,
    get_dungeon_flavor,
    get_race_name,
    get_subzone_lore,
    get_subzone_name,
    get_zone_flavor,
    get_zone_name,
    build_travel_metadata,
    format_travel_context,
    parse_conversation_response,
    parse_extra_data,
    run_single_reaction,
    append_json_instruction,
    strip_conversation_actions,
    get_chatter_mode,
)
from chatter_prompts import build_environmental_context_lines
from chatter_text import cleanup_message, strip_speaker_prefix

# Varied reaction styles to avoid samey comments
_REACTION_STYLES = [
    "Ask a question about what you see.",
    "Express a feeling the scene gives you.",
    "Compare it to somewhere else you've been.",
    "Point out one small detail others might miss.",
    "Wonder aloud about something in the scene.",
    "Say whether this place feels safe or dangerous.",
    "React with awe, unease, or curiosity.",
    "Make a practical observation about the terrain.",
    "Comment on the mood or atmosphere.",
    "Notice something beautiful or unsettling.",
]


def handle_screenshot_observation(db, client, config, event):
    """Generate a bot comment from a vision-analyzed
    screenshot. May trigger a multi-bot conversation."""
    event_id = event['id']
    extra = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'bot_group_screenshot_observation',
    )
    if not extra:
        _mark_event(db, event_id, 'skipped')
        return False

    bot_guid      = int(extra.get('bot_guid') or 0)
    bot_name      = extra.get('bot_name', 'Bot')
    group_id      = int(extra.get('group_id') or 0)
    weather       = extra.get('weather', 'none')
    time_of_day   = extra.get('time_of_day', 'unknown')
    atmosphere    = extra.get('atmosphere', '')
    environment   = extra.get('environment', '')
    creatures     = extra.get('creatures', '')
    travel_state  = extra.get('travel_state')
    if not isinstance(travel_state, dict):
        travel_state = _get_bot_travel_state(
            db, group_id, bot_guid)
    travel_context = format_travel_context(
        travel_state)
    travel_meta = build_travel_metadata(
        travel_state,
        travel_context,
    )

    if should_defer_party_generation(
        db, config, group_id,
        policy='filler',
        reason='bot_group_screenshot_observation',
    ):
        defer_event_for_party_gate(
            db,
            config,
            event_id,
            'bot_group_screenshot_observation',
        )
        return False

    # -- Resolve zone, subzone, flavor --
    zone_name = 'the area'
    subzone_name = ''
    zone_flavor = ''
    map_id = event.get('map_id') or 0
    if group_id:
        zone_id, area_id, _ = get_group_location(
            db, group_id)
        if zone_id:
            resolved = get_zone_name(zone_id)
            if resolved:
                zone_name = resolved
            zone_flavor = get_zone_flavor(zone_id) or ''
            sz_name = get_subzone_name(zone_id, area_id)
            if sz_name:
                subzone_name = sz_name
            sz_lore = get_subzone_lore(zone_id, area_id)
            if sz_lore:
                zone_flavor = sz_lore
    dungeon_flav = get_dungeon_flavor(map_id)
    if dungeon_flav:
        zone_flavor = dungeon_flav

    # -- Build observation --
    observation_parts = []
    if environment:
        observation_parts.append(environment)
    if creatures:
        observation_parts.append(creatures)
    if atmosphere and not environment:
        observation_parts.append(atmosphere)

    if not observation_parts:
        _mark_event(db, event_id, 'skipped')
        return False

    observation = '. '.join(observation_parts)

    # -- Location context --
    if subzone_name:
        location_str = f"{zone_name} — {subzone_name}"
    else:
        location_str = zone_name
    context_parts = [f"Location: {location_str}"]
    if weather and weather != 'none':
        context_parts.append(f"Weather: {weather}")
    context_parts.extend(build_environmental_context_lines())
    context_str = ', '.join(context_parts)

    # -- Recent chat + anti-repetition --
    history = _get_recent_chat(db, group_id)
    chat_block = format_chat_history(history)
    recent_bot_msgs = [
        m['message'] for m in history
        if m.get('is_bot')
    ]
    anti_rep = build_anti_repetition_context(
        recent_bot_msgs)

    # -- Decide: conversation or single statement? --
    members = get_group_members(db, group_id)
    conv_chance = int(config.get(
        'LLMChatter.Screenshot.ConversationChance',
        30,
    ))
    do_conversation = (
        len(members) >= 2
        and random.randint(1, 100) <= conv_chance
    )

    if do_conversation:
        try:
            return _screenshot_conversation(
                db, client, config, event_id,
                group_id, bot_guid, bot_name,
                members, observation, context_str,
                zone_flavor, chat_block, anti_rep,
                travel_context, travel_meta,
            )
        except Exception:
            fail_event(
                db, event_id,
                'bot_group_screenshot_observation',
                'conversation handler error',
            )
            return False

    return _screenshot_single(
        db, client, config, event_id,
        group_id, bot_guid, bot_name,
        observation, context_str, zone_flavor,
        chat_block, anti_rep, travel_context,
        travel_meta,
    )


def _build_location_block(
    bot_name, context_str, zone_flavor,
    chatter_mode='roleplay',
):
    """Build the location/flavor header for prompts."""
    is_rp = (chatter_mode == 'roleplay')

    if is_rp:
        block = (
            f"You are {bot_name}, travelling through "
            f"{context_str} with your group.\n"
        )
        if zone_flavor:
            block += (
                f"About this place: {zone_flavor}\n"
            )
        return block

    return (
        f"You are {bot_name}, a real WoW player. "
        f"Your character is currently at "
        f"{context_str} with the group.\n"
    )


def _get_bot_identity(db, bot_guid, bot_name):
    """Fetch race/class for a bot and build identity."""
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT class, race, gender FROM characters
        WHERE guid = %s
    """, (bot_guid,))
    row = cursor.fetchone()
    cursor.close()
    if row:
        return build_bot_identity(
            bot_name,
            get_race_name(row['race']),
            get_class_name(row['class']),
            get_gender_label(row['gender']),
        )
    return f"You are {bot_name}."


def _get_bot_travel_state(db, group_id, bot_guid):
    """Fetch persisted travel state for old screenshot
    events that predate embedded travel_state."""
    if not group_id or not bot_guid:
        return None
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT travel_mode, travel_context,
               is_mounted, is_flying,
               is_taxi_flying, is_on_transport,
               mount_display_id, transport_name
        FROM llm_group_bot_traits
        WHERE group_id = %s AND bot_guid = %s
        LIMIT 1
    """, (group_id, bot_guid))
    row = cursor.fetchone()
    cursor.close()
    if not row or not row.get('travel_mode'):
        return None
    return {
        'mode': row.get('travel_mode') or '',
        'context': row.get('travel_context') or '',
        'mounted': bool(row.get('is_mounted')),
        'flying': bool(row.get('is_flying')),
        'taxi_flight': bool(row.get('is_taxi_flying')),
        'on_transport': bool(row.get('is_on_transport')),
        'mount_display_id': int(
            row.get('mount_display_id') or 0),
        'transport_name': row.get('transport_name') or '',
    }


def _screenshot_single(
    db, client, config, event_id,
    group_id, bot_guid, bot_name,
    observation, context_str, zone_flavor,
    chat_block, anti_rep, travel_context='',
    travel_meta=None,
):
    """Single-bot statement about the screenshot."""
    chatter_mode = get_chatter_mode(config)
    is_rp = (chatter_mode == 'roleplay')

    if is_rp:
        style = random.choice(_REACTION_STYLES)
        identity = _get_bot_identity(
            db, bot_guid, bot_name)

        # Fetch personality traits
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT trait1, trait2, trait3, tone
            FROM llm_group_bot_traits
            WHERE group_id = %s AND bot_name = %s
        """, (group_id, bot_name))
        traits_row = cursor.fetchone()
        cursor.close()

        traits = ''
        if traits_row:
            t = [
                traits_row[k]
                for k in ('trait1', 'trait2', 'trait3')
                if traits_row[k]
            ]
            if t:
                traits = (
                    f"Your personality: "
                    f"{', '.join(t)}\n"
                )

        tone = ''
        if traits_row and traits_row.get('tone'):
            tone = (
                f"Your tone: "
                f"{traits_row['tone']}\n"
            )

        prompt = (
            f"{identity} {traits}{tone}"
            f"Travelling through {context_str} "
            f"with your group.\n"
            + (
                f"About this place: {zone_flavor}\n"
                if zone_flavor else ''
            )
            + "\nYou look around and notice:\n"
            f"{observation}\n\n"
            f"Style: {style}\n"
            "One or two sentences, "
            "80-150 characters.\n\n"
            "DO NOT:\n"
            "- Narrate or describe actions "
            "(no *looks around*)\n"
            "- Mention any people, players, or "
            "humanoid NPCs\n"
            "- Recite lore or history unless it comes "
            "naturally\n"
            "- Comment on UI, health bars, or game "
            "mechanics\n"
            "You are physically in the scene — convey "
            "what stands out to you as if you were really "
            "there. You can connect what you see to what "
            "you know about this place. "
            "Speak naturally and briefly.\n"
        )

    else:
        location_block = _build_location_block(
            bot_name,
            context_str,
            zone_flavor,
            chatter_mode,
        )

        prompt = (
            f"{location_block}"
            "\nThe game view currently shows:\n"
            f"{observation}\n\n"
            "Write something this player might actually "
            "type in party chat after seeing this. "
            "The screenshot is context, not a requirement "
            "to describe the scenery. "
            "The player can react to something visible, "
            "ask a practical question, complain, joke, "
            "mention where the group is going, seem "
            "confused, or make a completely mundane "
            "comment related to the situation. "
            "If nothing deserves a big reaction, keep "
            "the response low-key.\n"
            "Usually use 1-10 words. One-word replies "
            "and fragments are fine. Lowercase, missing "
            "punctuation, shorthand, and occasional typos "
            "are fine. Casual WoW/game terminology is fine. "
            "Occasional 'lol', 'lmao', 'bruh', 'rip', "
            "'tbh', or 'ngl' is fine when it naturally fits, "
            "but do not force slang.\n"
            "Do not narrate the scene. "
            "Do not describe what the character is doing. "
            "Do not turn the screenshot into travel writing. "
            "Do not recite lore. "
            "Do not force awe, curiosity, humor, or insight. "
            "Do not write fantasy dialogue."
        )

    if chat_block:
        prompt += chat_block + '\n'
    if anti_rep:
        prompt += anti_rep + '\n'
    if travel_context:
        prompt += travel_context + '\n'

    prompt = append_json_instruction(
        prompt, allow_action=is_rp)

    result = run_single_reaction(
        db, client, config,
        prompt=prompt,
        speaker_name=bot_name,
        bot_guid=bot_guid,
        channel='party',
        delay_seconds=2,
        event_id=event_id,
        allow_emote_fallback=False,
        context=(
            f"screenshot:#{event_id}:{bot_name}"
        ),
        label='screenshot_vision',
        max_tokens_override=120,
        metadata=travel_meta,
        group_id=group_id,
        delivery_policy='filler',
        delivery_reason=(
            'bot_group_screenshot_observation'
        ),
    )

    if not result['ok']:
        _mark_event(db, event_id, 'skipped')
        return False

    _store_chat(
        db, group_id, bot_guid,
        bot_name, True, result['message'],
    )
    _mark_event(db, event_id, 'completed')
    return True


def _screenshot_conversation(
    db, client, config, event_id,
    group_id, bot_guid, bot_name,
    members, observation, context_str,
    zone_flavor, chat_block, anti_rep,
    travel_context='', travel_meta=None,
):
    """Multi-bot conversation about screenshot context."""
    chatter_mode = get_chatter_mode(config)
    is_rp = (chatter_mode == 'roleplay')

    # Pick 2-3 available bots and ensure the
    # triggering bot is available.
    num_pick = random.randint(
        2, min(len(members), 3))
    other_names = [
        m for m in members if m != bot_name
    ]
    random.shuffle(other_names)
    picked = (
        [bot_name]
        + other_names[:num_pick - 1]
    )
    random.shuffle(picked)

    # Gather traits and character info.
    bots = []
    bot_guids = {}
    traits_map = {}
    tone_map = {}

    for name in picked:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT bot_guid, trait1, trait2, trait3,
                   tone
            FROM llm_group_bot_traits
            WHERE group_id = %s AND bot_name = %s
        """, (group_id, name))
        row = cursor.fetchone()
        cursor.close()

        if not row:
            continue

        guid = int(row['bot_guid'])
        bot_guids[name] = guid

        traits_map[name] = [
            row['trait1'],
            row['trait2'],
            row['trait3'],
        ]
        tone_map[name] = row.get('tone') or ''

        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT class, race, level, gender
            FROM characters WHERE guid = %s
        """, (guid,))
        char = cursor.fetchone()
        cursor.close()

        if not char:
            continue

        bots.append({
            'name': name,
            'class': get_class_name(
                char['class']),
            'race': get_race_name(
                char['race']),
            'level': char['level'],
            'gender': get_gender_label(
                char['gender']),
        })

    if len(bots) < 2:
        _mark_event(db, event_id, 'skipped')
        return False

    bot_names = [b['name'] for b in bots]

    if is_rp:
        # Preserve the original RP identities.
        bot_lines = []

        for b in bots:
            traits = traits_map.get(
                b['name'], [])
            trait_str = ', '.join(
                t for t in traits if t
            ) or 'adventurous'

            gender_prefix = (
                f"{b['gender']} "
                if b.get('gender') else ''
            )

            bot_lines.append(
                f"- {b['name']}: "
                f"{gender_prefix}"
                f"{b['race']} {b['class']}, "
                f"personality: {trait_str}"
                + (
                    f", tone: "
                    f"{tone_map.get(b['name'], '')}"
                    if tone_map.get(
                        b['name'], '')
                    else ""
                )
            )

        bot_block = '\n'.join(bot_lines)

        prompt = (
            "The following party members are "
            f"travelling through {context_str}:\n"
            f"{bot_block}\n\n"
        )

        if zone_flavor:
            prompt += (
                f"About this place: "
                f"{zone_flavor}\n\n"
            )

        prompt += (
            "They look around and notice:\n"
            f"{observation}\n\n"
            "Write a short conversation (2-4 lines) "
            "where the party members react to what "
            "they see. Each character should respond "
            "differently based on their personality "
            "and background.\n\n"
            "Rules:\n"
            "- Each line: 40-80 characters\n"
            "- No narrator actions "
            "(no *looks around*)\n"
            "- No mentions of people, players, "
            "or humanoid NPCs\n"
            "- Focus on the world: terrain, sky, "
            "buildings, wildlife\n"
            "- Each bot speaks once, naturally\n"
        )

        message_count = len(bots)

    else:
        bot_lines = []

        for b in bots:
            bot_lines.append(
                f"- {b['name']}: level "
                f"{b['level']} {b['class']}"
            )

        bot_block = '\n'.join(bot_lines)

        message_count = random.randint(
            1, min(3, len(bots) + 1))

        prompt = (
            "These are real WoW players controlling "
            "characters in the same party:\n"
            f"{bot_block}\n\n"
            f"Current location: {context_str}\n"
            "The game view currently shows:\n"
            f"{observation}\n\n"
            f"Generate {message_count} short party-chat "
            "message"
            f"{'s' if message_count != 1 else ''}. "
            "Treat the screenshot only as context for "
            "what the players are currently seeing. "
            "They do not all need to comment on it. "
            "A player can ignore the scenery and say "
            "something practical, confused, mundane, "
            "annoyed, funny, or unrelated-but-plausible "
            "for the immediate situation.\n"
            "Do not require every available speaker "
            "to talk. A speaker may talk more than once "
            "if that is the natural exchange. "
            "Do not force acknowledgement or agreement. "
            "Do not build a beginning-middle-end "
            "conversation. It can stop abruptly.\n"
            "Messages should usually be 1-10 words. "
            "One-word messages and fragments are fine. "
            "Lowercase, shorthand, missing punctuation, "
            "and occasional typos are fine. "
            "Normal WoW/game terminology is fine. "
            "Occasional casual internet slang is fine "
            "when natural, but do not force it.\n"
            "Do not narrate the scene. "
            "Do not describe actions. "
            "Do not recite lore. "
            "Do not write fantasy dialogue. "
            "Do not make every message clever, funny, "
            "helpful, or observant.\n"
        )

    if chat_block:
        prompt += chat_block + '\n'
    if anti_rep:
        prompt += anti_rep + '\n'
    if travel_context:
        prompt += travel_context + '\n'

    prompt = append_conversation_json_instruction(
        prompt,
        bot_names,
        message_count,
        allow_action=is_rp,
        require_all_speakers=is_rp,
    )

    max_tokens = min(
        80 * message_count, 400)

    response = call_llm(
        client, prompt, config,
        max_tokens_override=max_tokens,
        context=(
            f"screenshot-conv:"
            f"{','.join(bot_names)}"
        ),
        label='screenshot_vision',
        metadata=travel_meta,
    )

    if not response:
        _mark_event(db, event_id, 'skipped')
        return False

    messages = parse_conversation_response(
        response, bot_names)

    if not messages:
        _mark_event(db, event_id, 'skipped')
        return False

    strip_conversation_actions(
        messages,
        label='screenshot_conv',
    )

    cumulative_delay = 0
    prev_len = 0

    for msg in messages:
        speaker_guid = bot_guids.get(
            msg['name'], 0)

        if not speaker_guid:
            continue

        text = cleanup_message(
            strip_speaker_prefix(
                msg.get('message', ''),
                msg['name'],
            )
        )

        if not text:
            continue

        cumulative_delay += calculate_dynamic_delay(
            prev_len,
            text,
            config,
        )

        insert_chat_message(
            db,
            bot_guid=speaker_guid,
            bot_name=msg['name'],
            message=text,
            channel='party',
            delay_seconds=cumulative_delay,
            event_id=event_id,
            config=config,
            group_id=group_id,
            delivery_policy='filler',
            delivery_reason=(
                'bot_group_screenshot_observation'
            ),
        )

        _store_chat(
            db, group_id, speaker_guid,
            msg['name'], True, text,
        )
        prev_len = len(text)

    _mark_event(db, event_id, 'completed')
    return True