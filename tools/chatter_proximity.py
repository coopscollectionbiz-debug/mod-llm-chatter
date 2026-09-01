"""Proximity chatter event handlers."""

import logging
import random
from typing import Dict, List, Optional

from chatter_constants import PROXIMITY_CHAT_TOPICS
from chatter_db import insert_chat_message
from chatter_llm import call_llm
from chatter_shared import (
    PromptParts,
    append_json_instruction,
    append_conversation_json_instruction,
    parse_conversation_response,
    parse_extra_data,
    get_class_name,
    get_gender_label,
    get_race_name,
    get_chatter_mode,
    build_bot_state_context,
    strip_conversation_actions,
)
from chatter_text import (
    cleanup_message,
    parse_single_response,
    strip_speaker_prefix,
)

logger = logging.getLogger(__name__)


def _get_proximity_int(
    config: Dict, name: str, default: int
) -> int:
    return int(config.get(
        f'LLMChatter.ProximityChatter.{name}',
        default,
    ))


def _mark_event(db, event_id: int, status: str) -> None:
    cursor = db.cursor()
    cursor.execute(
        "UPDATE llm_chatter_events SET status = %s "
        "WHERE id = %s",
        (status, event_id),
    )
    db.commit()


def _query_bot_identity(
    db, bot_guid: int
) -> Dict[str, str]:
    if not bot_guid:
        return {}

    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            "SELECT class, race, gender FROM characters "
            "WHERE guid = %s",
            (bot_guid,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        return {
            'class': get_class_name(
                int(row.get('class', 0) or 0)
            ),
            'race': get_race_name(
                int(row.get('race', 0) or 0)
            ),
            'gender': get_gender_label(
                int(row.get('gender', 0) or 0)
            ),
        }
    except Exception:
        logger.error(
            "query bot identity failed",
            exc_info=True,
        )
        return {}


def _query_bot_traits(
    db, bot_guid: int
) -> Dict[str, object]:
    if not bot_guid:
        return {}

    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            "SELECT trait1, trait2, trait3,"
            "       tone, backstory "
            "FROM llm_group_bot_traits "
            "WHERE bot_guid = %s LIMIT 1",
            (bot_guid,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        return {
            'traits': [
                trait for trait in (
                    row.get('trait1'),
                    row.get('trait2'),
                    row.get('trait3'),
                )
                if trait
            ],
            'tone': row.get('tone') or '',
            'backstory': row.get('backstory') or '',
        }
    except Exception:
        logger.error(
            "query bot traits failed",
            exc_info=True,
        )
        return {}


def _describe_speaker(
    db, speaker: Dict
) -> str:
    if speaker.get('is_npc'):
        role = speaker.get('role') or 'NPC'
        sub_name = speaker.get('sub_name') or ''
        parts = [speaker.get('name', 'NPC'), role]
        if sub_name:
            parts.append(sub_name)
        return " | ".join(part for part in parts if part)

    bot_guid = int(speaker.get('bot_guid', 0) or 0)
    info = _query_bot_identity(db, bot_guid)
    class_name = speaker.get('class') or info.get(
        'class', 'Adventurer'
    )
    race_name = speaker.get('race') or info.get(
        'race', 'Unknown'
    )
    gender = speaker.get('gender') or info.get(
        'gender', ''
    )
    gender_prefix = f"{gender} " if gender else ""
    return (
        f"{speaker.get('name', 'Bot')} | "
        f"{gender_prefix}{race_name} {class_name}"
    )


def _speaker_channel(speaker: Dict) -> str:
    return 'msay' if speaker.get('is_npc') else 'say'


def _insert_proximity_line(
    db,
    event_id: int,
    speaker: Dict,
    player_guid: int,
    sequence: int,
    delay_seconds: int,
    parsed: Dict,
) -> bool:
    raw_message = parsed.get('message', '')
    message = strip_speaker_prefix(
        raw_message, speaker.get('name', '')
    )
    message = cleanup_message(
        message, action=parsed.get('action')
    )
    if not message:
        return False
    if len(message) > 255:
        message = message[:252] + "..."

    bot_guid = int(speaker.get('bot_guid', 0) or 0)
    npc_spawn_id = int(
        speaker.get('npc_spawn_id', 0) or 0
    )

    insert_chat_message(
        db,
        bot_guid=bot_guid,
        bot_name=speaker.get('name', 'Unknown'),
        message=message,
        channel=_speaker_channel(speaker),
        delay_seconds=delay_seconds,
        event_id=event_id,
        sequence=sequence,
        emote=parsed.get('emote'),
        npc_spawn_id=npc_spawn_id or None,
        player_guid=player_guid or None,
    )
    return True


def _single_prompt(
    db,
    extra: Dict,
    speaker: Dict,
    topic: str,
    player_message: Optional[str] = None,
    last_message: Optional[str] = None,
    config: Optional[Dict] = None,
) -> PromptParts:
    mode = get_chatter_mode(config or {})
    is_rp = mode == 'roleplay'
    is_npc = bool(speaker.get('is_npc'))

    zone_name = extra.get('zone_name', 'the area')
    subzone_name = extra.get('subzone_name', '')
    player_name = extra.get('player_name', 'the player')

    player_addressed = bool(
        extra.get('player_addressed', False)
    )

    speaker_desc = _describe_speaker(db, speaker)
    nearby_names = extra.get('nearby_names') or []

    speaker_traits = []
    speaker_tone = ''
    speaker_backstory = ''

    if not speaker.get('is_npc'):
        profile = _query_bot_traits(
            db,
            int(speaker.get('bot_guid', 0) or 0),
        )

        speaker_traits = profile.get('traits', [])
        speaker_tone = profile.get('tone', '')
        speaker_backstory = profile.get(
            'backstory', ''
        )

    if is_rp or is_npc:
        lines = [
            "You write extremely short, immersive World of "
            "Warcraft in-world /say lines.",
            "Message must be 8-15 words, grounded, local, "
            "and low-stakes.",
            "Keep it lore-friendly. No modern memes, no AI "
            "talk, no markdown.",
            "",
            f"Speaker: {speaker_desc}",
            f"Zone: {zone_name}",
        ]
    else:
        lines = [
            "Write one /say message like a real World of "
            "Warcraft player casually typing while playing.",
            "The speaker is the PLAYER controlling this "
            "character, not the character roleplaying.",
            "",
            "Keep it casual and low-effort.",
            "Very short messages are preferred.",
            "A one-word reply or sentence fragment is fine.",
            "Lowercase, shorthand, missing punctuation, and "
            "occasional typos are normal.",
            "Normal WoW shorthand like lol, gz, ty, np, mb, "
            "brb, afk, oom, inv, sec, omw, and lfg is fine "
            "when it naturally fits.",
            "Occasional casual internet slang is fine, but "
            "do not force slang or memes.",
            "",
            "Do not try to make the message interesting, "
            "clever, funny, useful, or memorable.",
            "Do not narrate gameplay.",
            "Do not describe scenery, weather, surroundings, "
            "or atmosphere.",
            "Do not turn the zone into a conversation topic "
            "just because its name is provided.",
            "Do not use fantasy dialogue.",
            "Do not explain ordinary WoW terminology.",
            "No AI talk or markdown.",
            "",
            f"Speaker: {speaker_desc}",
            f"Zone for factual context only: {zone_name}",
        ]

    if speaker_traits and (is_rp or is_npc):
        lines.append(
            "Speaker personality: "
            + ", ".join(speaker_traits)
        )

    if speaker_tone and (is_rp or is_npc):
        lines.append(
            f"Speaker tone: {speaker_tone}"
        )

    # RNG-gate backstory injection.
    if (
        speaker_backstory
        and config
        and (is_rp or is_npc)
    ):
        bs_enabled = int(config.get(
            'LLMChatter.Backstory.Enable', 1
        ))

        prox_chance = int(config.get(
            'LLMChatter.Backstory.ProximityChance',
            15,
        )) / 100.0

        if (
            bs_enabled
            and random.random() < prox_chance
        ):
            lines.append(
                f"Speaker background: "
                f"{speaker_backstory}"
            )

    if subzone_name:
        if is_rp or is_npc:
            lines.append(
                f"Subzone: {subzone_name}"
            )
        else:
            lines.append(
                f"Current subzone for factual context only: "
                f"{subzone_name}"
            )

    # Topic seeds are useful for RP, but forcing one into
    # every Normal-mode line makes ambient chat feel authored.
    if is_rp or is_npc:
        lines.append(
            f"Topic seed: {topic}"
        )

    if player_message:
        lines.append("")
        lines.append(
            f"Nearby player said: {player_message}"
        )

        if not is_rp and not is_npc:
            lines.extend([
                "Respond like an ordinary player who heard "
                "that message.",
                "Answer it if an answer makes sense, but you "
                "may also be brief, uncertain, confused, or "
                "only partially helpful.",
                "Do not invent specific game facts just to "
                "provide an answer.",
            ])

    elif last_message:
        lines.append("")
        lines.append(
            f"Most recent nearby /say: {last_message}"
        )

        if not is_rp and not is_npc:
            lines.extend([
                "This may be a continuation of the local "
                "conversation.",
                "React naturally if a response makes sense.",
                "Do not restate or summarize what was said.",
            ])

    elif not is_rp and not is_npc:
        lines.extend([
            "",
            "This is unsolicited ambient /say.",
            "There is no required subject.",
            "The player may make a mundane comment, ask a "
            "small question, complain briefly, say something "
            "practical, or say almost nothing.",
            "It does not need to start a conversation.",
        ])

    addressable = list(nearby_names)

    if player_addressed:
        addressable.insert(0, player_name)

    if addressable:
        if is_rp or is_npc:
            lines.append(
                "Nearby people you may address by name: "
                + ", ".join(addressable[:5]) + "."
            )
        else:
            lines.append(
                "Nearby players: "
                + ", ".join(addressable[:5])
                + ". Mention one only if it naturally fits."
            )

    return append_json_instruction(
        "\n".join(lines) + "\n",
        allow_action=(is_rp or is_npc),
        skip_emote=False,
    )

def _conversation_prompt(
    db, extra: Dict, participants: List[Dict],
    config: Optional[Dict] = None,
) -> PromptParts:
    mode = get_chatter_mode(config or {})
    is_rp = mode == 'roleplay'

    zone_name = extra.get('zone_name', 'the area')
    subzone_name = extra.get('subzone_name', '')

    max_lines = max(
        2, min(
            int(extra.get('max_lines', 3) or 3),
            len(participants) + 1,
        ),
    )

    if not is_rp:
        # Real /say exchanges are often extremely short.
        # Sometimes a player says one thing and nobody
        # really develops it into a conversation.
        max_lines = random.randint(1, max_lines)

    # RP mode may still use a topic seed.
    topic = (
        random.choice(PROXIMITY_CHAT_TOPICS)
        if is_rp
        else None
    )

    # Check backstory config once.
    _bs_enabled = False
    _bs_chance = 0.0

    if config:
        _bs_enabled = int(config.get(
            'LLMChatter.Backstory.Enable', 1
        )) == 1

        _bs_chance = int(config.get(
            'LLMChatter.Backstory.ProximityChance',
            15,
        )) / 100.0

    roster_lines = []

    for speaker in participants:
        line = f"- {_describe_speaker(db, speaker)}"

        if not speaker.get('is_npc') and is_rp:
            profile = _query_bot_traits(
                db,
                int(speaker.get('bot_guid', 0) or 0),
            )

            traits = profile.get('traits', [])
            tone = profile.get('tone', '')
            backstory = profile.get('backstory', '')

            if traits:
                line += (
                    "; personality: "
                    + ", ".join(traits)
                )

            if tone:
                line += f"; tone: {tone}"

            if (
                backstory
                and _bs_enabled
                and random.random() < _bs_chance
            ):
                line += (
                    f"; background: {backstory}"
                )

        roster_lines.append(line)

    roster = "\n".join(roster_lines)

    nearby_names = extra.get('nearby_names') or []
    player_name = extra.get('player_name', '')
    player_addressed = bool(
        extra.get('player_addressed', False)
    )

    if is_rp:
        lines = [
            "You write short World of Warcraft ambient "
            "overheard /say conversations.",
            "Use only the provided speaker names.",
            "Each message must be 6-14 words, natural, and "
            "grounded in the immediate place.",
            "Keep the exchange brief and immersive.",
            "",
            f"Zone: {zone_name}",
            f"Topic seed: {topic}",
            f"Write EXACTLY {max_lines} messages.",
            "Speakers may address each other by name.",
        ]
    else:
        lines = [
            "Simulate an overheard /say exchange between "
            "real World of Warcraft players.",
            "These are players at their keyboards controlling "
            "characters, not characters roleplaying.",
            "Use only the provided speaker names.",
            "",
            "Write like actual players who happen to be near "
            "each other in the game.",
            "Most messages should be very short and low-effort.",
            "One-word replies and sentence fragments are normal.",
            "Lowercase, shorthand, missing punctuation, and "
            "occasional typos are normal.",
            "Normal WoW shorthand like lol, gz, ty, np, mb, "
            "brb, afk, oom, inv, sec, omw, and lfg is fine "
            "when it naturally fits.",
            "",
            "Do not try to create an interesting conversation.",
            "Do not give the exchange a story arc, setup, "
            "punchline, lesson, or conclusion.",
            "Do not make every message witty, useful, friendly, "
            "or responsive.",
            "Players may misunderstand each other, disagree, "
            "give a useless answer, change the subject, or "
            "barely respond.",
            "A player may speak twice.",
            "Not every available speaker has to participate.",
            "The exchange may stop abruptly.",
            "",
            "Do not narrate gameplay, scenery, weather, "
            "surroundings, or atmosphere.",
            "Do not describe the zone just because its name "
            "is provided.",
            "Do not use fantasy dialogue or immersive "
            "character speech.",
            "Do not explain ordinary WoW terminology to the "
            "other players.",
            "Do not force memes, jokes, sarcasm, or slang.",
            "",
            "Possible reasons players might happen to talk "
            "include quests, mobs, loot, bags, professions, "
            "travel, grouping, directions, waiting, mistakes, "
            "or something completely trivial.",
            "You do NOT need to choose from that list.",
            "",
            f"Zone: {zone_name}",
            f"Write EXACTLY {max_lines} messages.",
        ]

    if subzone_name:
        if is_rp:
            lines.append(
                f"Subzone: {subzone_name}"
            )
        else:
            lines.append(
                f"Current subzone for factual context only: "
                f"{subzone_name}"
            )

    addressable = list(nearby_names)

    if player_addressed and player_name:
        addressable.insert(0, player_name)

    if addressable:
        if is_rp:
            lines.append(
                "Also nearby: "
                + ", ".join(addressable[:5])
                + ". A speaker may address one of them."
            )
        else:
            lines.append(
                "Other nearby players: "
                + ", ".join(addressable[:5])
                + ". They are background context only. "
                "Do not involve them unless it feels natural."
            )

    lines.append("Speakers:")
    lines.append(roster)

    speaker_names = [
        s.get('name', '') for s in participants
    ]

    return append_conversation_json_instruction(
        "\n".join(lines) + "\n",
        speaker_names,
        max_lines,
        allow_action=is_rp,
        require_all_speakers=is_rp,
    )

def _generate_single_line(
    db,
    client,
    config,
    event_id: int,
    extra: Dict,
    speaker: Dict,
    *,
    message_event_id: Optional[int] = None,
    topic: Optional[str] = None,
    player_message: Optional[str] = None,
    last_message: Optional[str] = None,
    sequence: int = 0,
    delay_seconds: int = 0,
    label: str = 'proximity_say',
) -> bool:
    prompt = _single_prompt(
        db,
        extra,
        speaker,
        topic or random.choice(
            PROXIMITY_CHAT_TOPICS
        ),
        player_message=player_message,
        last_message=last_message,
        config=config,
    )
    response = call_llm(
        client,
        prompt,
        config,
        max_tokens_override=_get_proximity_int(
            config, 'MaxTokensPerLine', 120
        ),
        label=label,
        metadata={
            'zone_name': extra.get('zone_name', ''),
            'speaker_name': speaker.get('name', ''),
        },
    )
    if not response:
        return False

    parsed = parse_single_response(response)
    return _insert_proximity_line(
        db,
        message_event_id or event_id,
        speaker,
        int(extra.get('player_guid', 0) or 0),
        sequence,
        delay_seconds,
        parsed,
    )


def handle_proximity_say(db, client, config, event):
    event_id = int(event['id'])
    extra = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'proximity_say',
    )
    participants = extra.get('participants') or []
    if not participants:
        _mark_event(db, event_id, 'skipped')
        return False

    ok = _generate_single_line(
        db,
        client,
        config,
        event_id,
        extra,
        participants[0],
        label='proximity_say',
    )
    _mark_event(
        db, event_id,
        'completed' if ok else 'skipped',
    )
    return ok


def handle_proximity_conversation(
    db, client, config, event
):
    event_id = int(event['id'])
    extra = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'proximity_conversation',
    )
    participants = extra.get('participants') or []
    if len(participants) < 2:
        _mark_event(db, event_id, 'skipped')
        return False

    prompt = _conversation_prompt(
        db, extra, participants, config=config,
    )
    max_lines = int(extra.get('max_lines', 3) or 3)
    # Each line needs ~60-80 tokens for JSON structure
    # (speaker, message, emote, action keys + values).
    # The per-line config controls message brevity in the
    # prompt, but the token budget must cover full JSON.
    max_tokens = 80 * max_lines
    response = call_llm(
        client,
        prompt,
        config,
        max_tokens_override=max_tokens,
        label='proximity_conversation',
        metadata={
            'zone_name': extra.get('zone_name', ''),
            'speaker_count': len(participants),
        },
    )
    if not response:
        _mark_event(db, event_id, 'skipped')
        return False

    names = [
        speaker.get('name', '')
        for speaker in participants
    ]
    parsed = parse_conversation_response(
        response, names
    )
    line_delay = max(0, int(
        extra.get('line_delay_seconds', 4) or 4
    ))
    player_guid = int(
        extra.get('player_guid', 0) or 0
    )
    speaker_by_name = {
        speaker.get('name', ''): speaker
        for speaker in participants
    }

    # Strip actions per-message based on
    # ActionChance — LLM always provides them,
    # Python enforces randomness post-parse.
    strip_conversation_actions(
        parsed, label='proximity_conversation'
    )

    inserted = 0
    cumulative_delay = 0
    for index, line in enumerate(parsed):
        speaker = speaker_by_name.get(
            line.get('name', '')
        )
        if not speaker:
            continue
        if index > 0:
            cumulative_delay += line_delay
        ok = _insert_proximity_line(
            db,
            event_id,
            speaker,
            player_guid,
            index,
            cumulative_delay,
            line,
        )
        if ok:
            inserted += 1

    if inserted == 0:
        logger.warning(
            "proximity_conversation event %s fell back "
            "to single-line output after parse failure",
            event_id,
        )
        fallback = _generate_single_line(
            db,
            client,
            config,
            event_id,
            extra,
            participants[0],
            label='proximity_conversation_fallback',
        )
        _mark_event(
            db, event_id,
            'completed' if fallback else 'skipped',
        )
        return fallback

    _mark_event(db, event_id, 'completed')
    return True


def handle_proximity_reply(db, client, config, event):
    event_id = int(event['id'])

    extra = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'proximity_reply',
    )

    mode = get_chatter_mode(config or {})
    is_rp = mode == 'roleplay'

    responder = {
        'name': extra.get(
            'responder_name', 'Nearby'
        ),
        'is_npc': bool(
            extra.get('responder_is_npc', False)
        ),
        'bot_guid': int(
            extra.get(
                'responder_bot_guid', 0
            ) or 0
        ),
        'npc_spawn_id': int(
            extra.get(
                'responder_npc_spawn_id', 0
            ) or 0
        ),
    }

    if (
        not responder['bot_guid']
        and not responder['npc_spawn_id']
    ):
        _mark_event(db, event_id, 'skipped')
        return False

    # Normal-mode proximity conversation is PlayerBot
    # chat. NPC scene replies are RP-only.
    if not is_rp and responder['is_npc']:
        logger.debug(
            "Skipping Normal-mode NPC proximity reply "
            "for event %s",
            event_id,
        )
        _mark_event(db, event_id, 'skipped')
        return False

    # Topic steering is retained for RP. Normal mode
    # relies on the actual previous/player message and
    # should not be forced into a scripted exit.
    if is_rp:
        topic = "brief local reply"

        if int(
            extra.get('turn_count', 0) or 0
        ) >= (
            _get_proximity_int(
                config, 'ReplyMaxTurns', 5
            ) - 1
        ):
            topic = (
                "brief reply with a graceful exit"
            )
    else:
        topic = "brief local reply"

    ok = _generate_single_line(
        db,
        client,
        config,
        event_id,
        extra,
        responder,
        message_event_id=int(
            extra.get('scene_id', 0) or 0
        ) or event_id,
        topic=topic,
        player_message=extra.get(
            'player_message', ''
        ),
        last_message=extra.get(
            'last_message', ''
        ),
        label='proximity_reply',
    )

    _mark_event(
        db,
        event_id,
        'completed' if ok else 'skipped',
    )

    return ok


def _fetch_proximity_history(
    db, player_guid: int, zone_id: int,
    limit: int = 10,
) -> List[Dict]:
    """Fetch recent proximity messages for context."""
    if not player_guid or not zone_id:
        return []
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            "SELECT t.bot_name, t.message FROM ("
            "  SELECT m.bot_name, m.message,"
            "         m.delivered_at"
            "  FROM llm_chatter_messages m"
            "  JOIN llm_chatter_events e"
            "    ON m.event_id = e.id"
            "  WHERE m.delivered = 1"
            "    AND m.channel IN ('say', 'msay')"
            "    AND e.zone_id = %s"
            "    AND m.player_guid = %s"
            "    AND m.delivered_at"
            "        > DATE_SUB(NOW(),"
            "          INTERVAL 5 MINUTE)"
            "  ORDER BY m.delivered_at DESC"
            "  LIMIT %s"
            ") t ORDER BY t.delivered_at ASC",
            (zone_id, player_guid, limit),
        )
        rows = cursor.fetchall()
        return [
            {
                'name': r['bot_name'],
                'message': r['message'],
            }
            for r in rows
        ]
    except Exception:
        logger.error(
            "fetch proximity history failed",
            exc_info=True,
        )
        return []


def _format_history_block(
    history: List[Dict],
) -> str:
    if not history:
        return ""
    lines = [
        f"{h['name']}: {h['message']}"
        for h in history
    ]
    return (
        "Recent nearby conversation:\n"
        + "\n".join(lines)
    )


def _player_say_single_prompt(
    db,
    extra: Dict,
    speaker: Dict,
    player_message: str,
    history: List[Dict],
    config: Optional[Dict] = None,
) -> PromptParts:
    mode = get_chatter_mode(config or {})
    is_rp = mode == 'roleplay'
    is_npc = bool(speaker.get('is_npc'))
    zone_name = extra.get('zone_name', 'the area')
    subzone_name = extra.get('subzone_name', '')
    player_name = extra.get(
        'player_name', 'the player'
    )
    speaker_desc = _describe_speaker(db, speaker)

    bot_state = {}
    bot_states = extra.get('bot_states') or {}

    if (
        not is_npc
        and isinstance(bot_states, dict)
    ):
        speaker_guid = speaker.get('id')
        if speaker_guid is not None:
            bot_state = bot_states.get(
                str(speaker_guid), {}
            )

    factual_context = build_bot_state_context(
        bot_state
    )
    nearby_names = extra.get('nearby_names') or []

    if is_rp or is_npc:
        lines = [
            "You write extremely short, immersive World "
            "of Warcraft in-world /say lines.",
            "Message must be 8-15 words, grounded, "
            "local, and low-stakes.",
            "Keep it lore-friendly. No modern memes, no "
            "AI talk, no markdown.",
            "",
            f"Speaker: {speaker_desc}",
            f"Zone: {zone_name}",
        ]
    else:
        lines = [
            "Reply to the nearby player like a real WoW "
            "player casually typing in /say while playing.",
            "You are the PLAYER controlling this character, "
            "not an NPC roleplaying the character.",
            "Keep the reply casual and usually short. "
            "Fragments, lowercase, shorthand, missing "
            "punctuation, and occasional typos are normal.",
            "WoW shorthand and occasional internet slang are "
            "fine when natural. Do not force memes, jokes, "
            "sarcasm, or cleverness.",
            "Answer what the player actually said and follow "
            "the conversational context naturally.",
            "Very short replies are fine when they genuinely fit, "
            "but do not default to lol, idk, or other filler.",
            "Do not narrate the scenery or make the response "
            "sound like fantasy dialogue.",
            "No AI talk or markdown.",
            "",
            f"Speaker: {speaker_desc}",
            f"Zone: {zone_name}",
        ]
    if subzone_name:
        lines.append(f"Subzone: {subzone_name}")

    if factual_context:
        lines.append("")
        lines.append(factual_context)

    if not is_rp and not is_npc:
        lines.extend([
            "",
            "FACTUAL RULES:",
            "- The authoritative live bot state above "
            "overrides chat history and previous messages "
            "for specific factual claims.",
            "- Use only that state for facts about your "
            "level, quests, objectives, counts, inventory, "
            "gear, professions, money, location, or activity.",
            "- Never invent a quest name, quest objective, "
            "mob, item, NPC, number, destination, or other "
            "specific game-state fact.",
            "- The live-state restriction applies only to factual "
            "WoW game-state claims such as quests, items, levels, "
            "objective counts, NPCs, locations, inventory, gear, "
            "professions, money, and current activity.",
            "- Harmless social details, opinions, jokes, preferences, "
            "real-world topics, and conversational personality may "
            "be improvised naturally when they do not contradict "
            "established conversation or character context.",
            "- Do not say you don't know merely because a harmless "
            "social or real-world answer is absent from live game state.",
            "- Supplied [[quest:...]] and [[item:...]] "
            "tokens are exact opaque strings. Copy one "
            "exactly when relevant or omit it. Never create "
            "or modify a token.",
        ])

    addressed = extra.get('addressed_name', '')
    if addressed:
        lines.append(
            f"The player ({player_name}) is "
            f"addressing {addressed} directly."
        )
    lines.append(
        f"A nearby player ({player_name}) said: "
        f"{player_message}"
    )
    lines.append(
        "Respond naturally to the player's words."
    )

    # In Normal mode, recent proximity chat is only
    # background context. Simple greetings should not get
    # dragged into whatever ambient topic happened earlier.
    normalized_player_message = (
        player_message.strip().lower().rstrip("!?.,")
    )

    simple_greetings = {
        "hi",
        "hey",
        "hello",
        "yo",
        "sup",
        "hiya",
        "hey guys",
        "hi guys",
        "hello guys",
        "yo guys",
        "hey there",
        "hello there",
        "hi there",
    }

    include_history = True

    if (
        not is_rp
        and not is_npc
        and normalized_player_message in simple_greetings
    ):
        include_history = False

    history_block = (
        _format_history_block(history)
        if include_history
        else ""
    )

    if history_block:
        lines.extend([
            "",
            "RECENT CHAT CONTEXT:",
            "- The player's CURRENT message is the primary "
            "thing you are responding to.",
            "- Treat the recent conversation as authoritative "
            "conversation history when the player's current message "
            "refers to the same topic, joke, person, question, or idea.",
            "- Maintain continuity with things you already said. "
            "Do not contradict your own recent replies without a "
            "natural correction or change of mind.",
            "- Especially do not continue topics about "
            "scenery, sunsets, sunrises, weather, the sky, "
            "lighting, atmosphere, views, or how the area "
            "looks unless the player explicitly brings one "
            "of those things up.",
            "- A new greeting, question, or subject can "
            "completely replace the previous topic.",
            "",
            history_block,
        ])

    addressable = list(nearby_names)
    addressable.insert(0, player_name)
    if addressable:
        lines.append(
            "Nearby people you may address by "
            "name: "
            + ", ".join(addressable[:5]) + "."
        )

    return append_json_instruction(
        "\n".join(lines) + "\n",
        allow_action=is_rp or is_npc,
        skip_emote=False,
    )


def _player_say_conversation_prompt(
    db,
    extra: Dict,
    participants: List[Dict],
    player_message: str,
    history: List[Dict],
    config: Optional[Dict] = None,
) -> PromptParts:
    mode = get_chatter_mode(config or {})
    is_rp = mode == 'roleplay'

    zone_name = extra.get('zone_name', 'the area')
    subzone_name = extra.get('subzone_name', '')
    player_name = extra.get(
        'player_name', 'the player'
    )

    max_lines = max(
        2, min(
            int(extra.get('max_lines', 3) or 3),
            len(participants) + 1,
        ),
    )

    if not is_rp:
        # A real player's /say does not need to trigger
        # a whole group conversation. One reply is normal.
        max_lines = random.randint(1, max_lines)

    roster = "\n".join(
        f"- {_describe_speaker(db, speaker)}"
        for speaker in participants
    )

    nearby_names = extra.get('nearby_names') or []

    bot_states = extra.get('bot_states') or {}
    if not isinstance(bot_states, dict):
        bot_states = {}

    if is_rp:
        lines = [
            "You write short World of Warcraft "
            "overheard /say conversations.",
            "Use only the provided speaker names.",
            "Each message must be 6-14 words, natural, "
            "and grounded in the immediate place.",
            "Keep the exchange brief and immersive.",
            "",
            f"Zone: {zone_name}",
        ]
    else:
        lines = [
            "Simulate nearby real World of Warcraft players "
            "responding to another player's /say message.",
            "Use only the provided speaker names.",
            "The speakers are PLAYERS at their keyboards "
            "controlling their characters. They are not "
            "NPCs or characters roleplaying.",
            "",
            "Treat this like ordinary WoW player chat.",
            "Most replies should be very short and low-effort.",
            "One-word replies and fragments are normal.",
            "Lowercase, shorthand, missing punctuation, and "
            "occasional typos are normal.",
            "Normal WoW shorthand like lol, gz, ty, np, mb, "
            "brb, afk, oom, inv, sec, omw, and lfg is fine "
            "when it naturally fits.",
            "Occasional casual internet slang is fine, but "
            "do not force slang or memes.",
            "",
            "At least one speaker should react to what the "
            "player actually said.",
            "One player answering is completely sufficient.",
            "Do NOT turn a simple player message into a group "
            "discussion just because several bots are nearby.",
            "Other speakers may ignore the player entirely.",
            "A speaker may talk more than once.",
            "Do not make everyone agree, acknowledge each "
            "other, or stay on the same subject.",
            "Replies can be brief, distracted, mundane, or imperfect "
            "when appropriate. When the player is clearly sustaining a "
            "conversation, follow the thread coherently instead of "
            "becoming randomly uncertain or evasive.",
            "",
            "Do not try to make the exchange interesting, "
            "clever, funny, wholesome, or memorable.",
            "Do not give it a story arc or conclusion.",
            "Do not narrate gameplay, scenery, weather, "
            "surroundings, or atmosphere.",
            "Do not discuss the zone merely because its name "
            "is provided.",
            "Do not use fantasy dialogue.",
            "Do not explain ordinary WoW terminology.",
            "",
            f"Zone for factual context only: {zone_name}",
        ]

    if subzone_name:
        if is_rp:
            lines.append(
                f"Subzone: {subzone_name}"
            )
        else:
            lines.append(
                f"Current subzone for factual context only: "
                f"{subzone_name}"
            )

    factual_states_added = False

    for speaker in participants:
        if speaker.get('is_npc'):
            continue

        speaker_guid = speaker.get('id')
        if speaker_guid is None:
            continue

        bot_state = bot_states.get(
            str(speaker_guid), {}
        )

        factual_context = build_bot_state_context(
            bot_state
        )

        if not factual_context:
            continue

        if not factual_states_added:
            lines.append("")
            lines.append(
                "AUTHORITATIVE LIVE BOT STATES:"
            )
            factual_states_added = True

        speaker_name = speaker.get(
            'name', 'Unknown'
        )

        lines.append("")
        lines.append(
            f"Live state for {speaker_name}:"
        )
        lines.append(factual_context)

    if not is_rp:
        lines.extend([
            "",
            "FACTUAL RULES:",
            "- The authoritative live bot states above "
            "override chat history and previous bot "
            "messages for specific factual claims.",
            "- Each PlayerBot may use ONLY the live state "
            "listed under their own name for facts about "
            "themselves.",
            "- Never borrow another bot's level, quests, "
            "objective counts, inventory, gear, "
            "professions, money, location, or activity.",
            "- Never invent a quest name, quest objective, "
            "mob, item, NPC, number, destination, or other "
            "specific game-state fact.",
            "- The live-state restriction applies only to factual "
            "WoW game-state claims. Harmless social details, opinions, "
            "jokes, preferences, real-world topics, and conversational "
            "personality may be improvised naturally when consistent "
            "with prior chat.",
            "- Do not make a speaker say they don't know merely because "
            "a harmless conversational answer is absent from live state.",
            "- Supplied [[quest:...]] and [[item:...]] "
            "tokens are exact opaque strings. Copy one "
            "exactly when relevant or omit it. Never "
            "create or modify a token.",
        ])

    addressed = extra.get('addressed_name', '')

    lines.append("")
    lines.append(
        f"A nearby player ({player_name}) said: "
        f"{player_message}"
    )

    if addressed:
        lines.append(
            f"The player is directly addressing "
            f"{addressed}."
        )
        lines.append(
            f"The FIRST message MUST be spoken by "
            f"{addressed}."
        )

        if not is_rp:
            lines.append(
                f"{addressed} should respond to the player "
                f"before anyone else says anything."
            )

    if is_rp:
        lines.append(
            "Speakers should react to or acknowledge "
            "the player's words."
        )
    else:
        lines.extend([
            "At least one generated message must respond "
            "naturally to the player's words.",
            "Do not merely use the player's message as a "
            "topic for the bots to discuss with each other.",
            "If the player's message only needs a tiny reply, "
            "a tiny reply is preferable.",
        ])

    lines.append(
        f"Write EXACTLY {max_lines} messages."
    )

    if is_rp:
        lines.append(
            "Speakers may address each other or the "
            "player by name."
        )
    else:
        lines.append(
            "Speakers may address the player or each other "
            "when natural, but do not force names into replies."
        )

    history_block = _format_history_block(history)

    if history_block:
        lines.append("")
        lines.append(history_block)

        if not is_rp:
            lines.append(
                "Use recent conversation whenever it is relevant to "
                "the player's current message. Preserve established "
                "facts, jokes, opinions, references, and conversational "
                "context. Do not summarize it or force an unrelated topic."
            )

    addressable = list(nearby_names)
    addressable.insert(0, player_name)

    if addressable:
        if is_rp:
            lines.append(
                "Also nearby: "
                + ", ".join(addressable[:5])
                + ". A speaker may address one of them."
            )
        else:
            lines.append(
                "Other nearby players: "
                + ", ".join(addressable[:5])
                + ". Mention one only when it naturally fits."
            )

    lines.append("Speakers:")
    lines.append(roster)

    speaker_names = [
        s.get('name', '') for s in participants
    ]

    return append_conversation_json_instruction(
        "\n".join(lines) + "\n",
        speaker_names,
        max_lines,
        allow_action=is_rp,
        require_all_speakers=is_rp,
    )


def handle_proximity_player_say(
    db, client, config, event
):
    event_id = int(event['id'])
    extra = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'proximity_player_say',
    )
    participants = extra.get('participants') or []
    if not participants:
        _mark_event(db, event_id, 'skipped')
        return False

    player_message = extra.get(
        'player_message', ''
    )
    if not player_message:
        _mark_event(db, event_id, 'skipped')
        return False

    player_guid = int(
        extra.get('player_guid', 0) or 0
    )
    zone_id = int(
        extra.get('zone_id', 0) or 0
    )
    history = _fetch_proximity_history(
        db, player_guid, zone_id
    )

    speaker = participants[0]
    prompt = _player_say_single_prompt(
        db,
        extra,
        speaker,
        player_message,
        history,
        config=config,
    )

    # Persistent bot <-> player memory is available during
    # direct conversation regardless of where it was created.
    bot_guid = int(
        speaker.get('bot_guid', 0) or 0
    )
    if player_guid and bot_guid:
        from chatter_memory import (
            get_relationship_memory_context,
        )

        relationship_context = (
            get_relationship_memory_context(
                db,
                bot_guid,
                player_guid,
                str(
                    extra.get('player_name')
                    or 'the player'
                ),
                count=4,
            )
        )

        if relationship_context:
            prompt = (
                relationship_context
                + "\n\n"
                + prompt
            )
    response = call_llm(
        client,
        prompt,
        config,
        max_tokens_override=_get_proximity_int(
            config, 'MaxTokensPerLine', 120
        ),
        label='proximity_player_say',
        metadata={
            'zone_name': extra.get(
                'zone_name', ''
            ),
            'speaker_name': speaker.get(
                'name', ''
            ),
        },
    )
    if not response:
        _mark_event(db, event_id, 'skipped')
        return False

    parsed = parse_single_response(response)
    ok = _insert_proximity_line(
        db,
        event_id,
        speaker,
        player_guid,
        0,
        0,
        parsed,
    )
    _mark_event(
        db, event_id,
        'completed' if ok else 'skipped',
    )

    if ok and player_guid:
        bot_guid = int(
            speaker.get('bot_guid', 0) or 0
        )
        bot_reply = str(
            parsed.get('message') or ''
        ).strip()

        if bot_guid and bot_reply:
            from chatter_memory import (
                queue_relationship_memory,
            )

            queue_relationship_memory(
                config,
                bot_guid,
                player_guid,
                event_context=(
                    f"{extra.get('player_name', 'Player')} "
                    f"said: {player_message[:250]}\n"
                    f"{speaker.get('name', 'Bot')} "
                    f"replied: {bot_reply[:250]}"
                ),
                source='proximity_player',
                bot_name=str(
                    speaker.get('name') or ''
                ),
                player_name=str(
                    extra.get('player_name') or ''
                ),
            )

    return ok


def handle_proximity_player_conversation(
    db, client, config, event
):
    event_id = int(event['id'])
    extra = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'proximity_player_conversation',
    )
    participants = extra.get('participants') or []
    if len(participants) < 2:
        _mark_event(db, event_id, 'skipped')
        return False

    player_message = extra.get(
        'player_message', ''
    )
    if not player_message:
        _mark_event(db, event_id, 'skipped')
        return False

    player_guid = int(
        extra.get('player_guid', 0) or 0
    )
    zone_id = int(
        extra.get('zone_id', 0) or 0
    )
    history = _fetch_proximity_history(
        db, player_guid, zone_id
    )

    prompt = _player_say_conversation_prompt(
        db,
        extra,
        participants,
        player_message,
        history,
        config=config,
    )

    if player_guid:
        from chatter_memory import (
            get_relationship_memory_context,
        )

        memory_blocks = []
        player_name = str(
            extra.get('player_name') or 'the player'
        )

        for participant in participants:
            participant_guid = int(
                participant.get('bot_guid', 0) or 0
            )
            if not participant_guid:
                continue

            participant_memories = []

            # This bot's private relationship with the real player.
            relationship_context = (
                get_relationship_memory_context(
                    db,
                    participant_guid,
                    player_guid,
                    player_name,
                    count=3,
                )
            )
            if relationship_context:
                participant_memories.append(
                    relationship_context
                )

            # This bot's directional memories of the other
            # PlayerBots in the same conversation.
            for other in participants:
                other_guid = int(
                    other.get('bot_guid', 0) or 0
                )
                if (
                    not other_guid
                    or other_guid == participant_guid
                ):
                    continue

                other_name = str(
                    other.get('name') or 'the other bot'
                )

                bot_memory = (
                    get_relationship_memory_context(
                        db,
                        participant_guid,
                        other_guid,
                        other_name,
                        count=2,
                    )
                )
                if bot_memory:
                    participant_memories.append(
                        bot_memory
                    )

            if participant_memories:
                memory_blocks.append(
                    f"PRIVATE MEMORY FOR "
                    f"{participant.get('name', 'Bot')} ONLY:\n"
                    + "\n".join(participant_memories)
                )

        if memory_blocks:
            prompt = (
                "\n\n".join(memory_blocks)
                + "\n\n"
                + "MEMORY ISOLATION RULE: Each speaker may use "
                + "ONLY the private memories labeled for that "
                + "speaker. Never transfer, reveal, or infer another "
                + "bot's private memories as if this speaker knew "
                + "them.\n\n"
                + prompt
            )
    max_lines = int(
        extra.get('max_lines', 3) or 3
    )
    max_tokens = 80 * max_lines
    response = call_llm(
        client,
        prompt,
        config,
        max_tokens_override=max_tokens,
        label='proximity_player_conversation',
        metadata={
            'zone_name': extra.get(
                'zone_name', ''
            ),
            'speaker_count': len(participants),
        },
    )
    if not response:
        _mark_event(db, event_id, 'skipped')
        return False

    names = [
        speaker.get('name', '')
        for speaker in participants
    ]
    parsed = parse_conversation_response(
        response, names
    )
    line_delay = max(0, int(
        extra.get('line_delay_seconds', 4) or 4
    ))
    speaker_by_name = {
        speaker.get('name', ''): speaker
        for speaker in participants
    }

    strip_conversation_actions(
        parsed,
        label='proximity_player_conversation',
    )

    inserted = 0
    successful_lines = []
    cumulative_delay = 0

    for index, line in enumerate(parsed):
        speaker = speaker_by_name.get(
            line.get('name', '')
        )
        if not speaker:
            continue

        if index > 0:
            cumulative_delay += line_delay

        ok = _insert_proximity_line(
            db,
            event_id,
            speaker,
            player_guid,
            index,
            cumulative_delay,
            line,
        )

        if ok:
            inserted += 1

            bot_reply = str(
                line.get('message') or ''
            ).strip()

            successful_lines.append({
                'speaker': speaker,
                'message': bot_reply,
            })

            # This bot may form a memory with the real player.
            if player_guid:
                speaker_guid = int(
                    speaker.get('bot_guid', 0) or 0
                )

                if speaker_guid and bot_reply:
                    from chatter_memory import (
                        queue_relationship_memory,
                    )

                    queue_relationship_memory(
                        config,
                        speaker_guid,
                        player_guid,
                        event_context=(
                            f"{extra.get('player_name', 'Player')} "
                            f"said: {player_message[:250]}\n"
                            f"{speaker.get('name', 'Bot')} "
                            f"replied: {bot_reply[:250]}"
                        ),
                        source='proximity_player',
                        bot_name=str(
                            speaker.get('name') or ''
                        ),
                        player_name=str(
                            extra.get('player_name') or ''
                        ),
                    )

    # Bots that actually spoke together may independently
    # remember one another. Memories are directional:
    # A -> B is distinct from B -> A.
    if len(successful_lines) >= 2:
        from chatter_memory import (
            queue_relationship_memory,
        )

        seen_pairs = set()

        for source in successful_lines:
            source_speaker = source['speaker']
            source_guid = int(
                source_speaker.get('bot_guid', 0) or 0
            )

            if not source_guid:
                continue

            for target in successful_lines:
                target_speaker = target['speaker']
                target_guid = int(
                    target_speaker.get('bot_guid', 0) or 0
                )

                if (
                    not target_guid
                    or target_guid == source_guid
                ):
                    continue

                pair = (source_guid, target_guid)

                if pair in seen_pairs:
                    continue

                seen_pairs.add(pair)

                # Keep this relationship memory pair-focused:
                # player + source bot + target bot only.
                pair_lines = [
                    (
                        f"{extra.get('player_name', 'Player')}: "
                        f"{player_message[:180]}"
                    )
                ]

                for entry in successful_lines:
                    entry_speaker = entry['speaker']
                    entry_guid = int(
                        entry_speaker.get(
                            'bot_guid', 0
                        ) or 0
                    )

                    if entry_guid not in (
                        source_guid,
                        target_guid,
                    ):
                        continue

                    entry_message = str(
                        entry.get('message') or ''
                    )

                    if not entry_message:
                        continue

                    pair_lines.append(
                        f"{entry_speaker.get('name', 'Bot')}: "
                        f"{entry_message[:180]}"
                    )

                pair_context = "\n".join(
                    pair_lines[-5:]
                )

                queue_relationship_memory(
                    config,
                    source_guid,
                    target_guid,
                    event_context=pair_context,
                    source='proximity_bot',
                    bot_name=str(
                        source_speaker.get('name') or ''
                    ),
                    player_name=str(
                        target_speaker.get('name') or ''
                    ),
                )

    if inserted == 0:
        logger.warning(
            "proximity_player_conversation "
            "event %s fell back to single-line",
            event_id,
        )

        # Prefer the explicitly addressed PlayerBot for
        # fallback instead of blindly using participants[0].
        fallback_speaker = participants[0]
        addressed_name = extra.get(
            'addressed_name', ''
        )

        if addressed_name:
            for candidate in participants:
                if (
                    candidate.get('name', '').lower()
                    == addressed_name.lower()
                ):
                    fallback_speaker = candidate
                    break

        fallback = _generate_single_line(
            db,
            client,
            config,
            event_id,
            extra,
            fallback_speaker,
            player_message=player_message,
            label=(
                'proximity_player_conversation'
                '_fallback'
            ),
        )
        _mark_event(
            db, event_id,
            'completed' if fallback else 'skipped',
        )
        return fallback

    _mark_event(db, event_id, 'completed')
    return True
