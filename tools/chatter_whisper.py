"""
Private player <-> PlayerBot whisper conversations.

Each conversation is scoped to one real player GUID and
one bot GUID. The current bot state comes from the C++
producer via BuildBotStateJson(), so replies can be
grounded in authoritative live PlayerBots state.
"""

import json
import logging

from chatter_db import (
    insert_chat_message,
    mark_event,
)

from chatter_shared import (
    call_llm,
    parse_extra_data,
    parse_single_response,
    strip_speaker_prefix,
    cleanup_message,
    calculate_dynamic_delay,
)

from chatter_playerbot_intent import (
    WOW_CLASS_NAMES,
    build_playerbot_action_speech_context,
    classify_playerbot_intent,
    enqueue_live_playerbot_buff_actions,
    resolve_playerbot_action_candidates,
    should_analyze_playerbot_intent,
)

logger = logging.getLogger(__name__)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dry_run_whisper_playerbot_intent(
    db,
    client,
    config,
    *,
    event_id,
    player_name,
    player_message,
    bot_guid,
    bot_name,
):
    """Interpret a natural-language request sent to one Playerbot.

    Whisper ownership is deterministic: the actual receiver is
    the only candidate. No action is enqueued or executed here.
    """
    if not should_analyze_playerbot_intent(
        player_message
    ):
        return None

    cursor = db.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT class
            FROM characters
            WHERE guid = %s
            LIMIT 1
            """,
            (bot_guid,),
        )

        row = cursor.fetchone()

    finally:
        try:
            cursor.close()
        except Exception:
            pass

    if not row:
        logger.info(
            "[PLAYERBOT-DRYRUN] event=%s player=%s "
            "channel=whisper bot=%s message=%r "
            "result=no_candidate_state",
            event_id,
            player_name,
            bot_name,
            player_message,
        )
        return None

    class_id = int(
        row.get('class') or 0
    )

    class_name = WOW_CLASS_NAMES.get(
        class_id,
        '',
    )

    candidate = {
        'guid': int(bot_guid),
        'name': bot_name,
        'class_id': class_id,
        'class_name': class_name,
    }

    intent = classify_playerbot_intent(
        client,
        config,
        player_message=player_message,
        player_name=player_name,
        source_channel='whisper',
        bots=[
            {
                'name': bot_name,
                'class': class_name,
            },
        ],
    )

    if not intent.get('is_action_request'):
        logger.info(
            "[PLAYERBOT-DRYRUN] event=%s player=%s "
            "channel=whisper bot=%s message=%r "
            "result=no_action",
            event_id,
            player_name,
            bot_name,
            player_message,
        )

        return {
            'intent': intent,
            'resolved': [],
        }

    resolved = resolve_playerbot_action_candidates(
        intent,
        [candidate],
        source_channel='whisper',
        whisper_bot_guid=bot_guid,
    )

    resolved_summary = [
        {
            'guid': int(bot['guid']),
            'name': bot['name'],
            'class': bot.get(
                'class_name',
                '',
            ),
        }
        for bot in resolved
    ]

    logger.info(
        "[PLAYERBOT-DRYRUN] event=%s player=%s "
        "channel=whisper bot=%s message=%r "
        "action=%s arg=%r hint=%r "
        "target_type=%r target_name=%r "
        "confidence=%.2f resolved=%s",
        event_id,
        player_name,
        bot_name,
        player_message,
        intent.get('action_key'),
        intent.get('action_arg'),
        intent.get('bot_hint'),
        intent.get('target_type'),
        intent.get('target_name'),
        float(
            intent.get('confidence') or 0.0
        ),
        resolved_summary,
    )

    return {
        'intent': intent,
        'resolved': resolved,
    }


def _load_bot_identity(db, bot_guid):
    """
    Load stable personality traits without assuming
    optional identity-table columns exist.
    """
    cursor = db.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT bot_name, trait1, trait2, trait3
            FROM llm_bot_identities
            WHERE bot_guid = %s
            LIMIT 1
            """,
            (bot_guid,),
        )

        row = cursor.fetchone()

    except Exception:
        logger.debug(
            "Whisper identity lookup failed bot=%s",
            bot_guid,
            exc_info=True,
        )
        return {}

    return row or {}


def _load_dm_history(
    db,
    current_event_id,
    player_guid,
    bot_guid,
    limit=6,
):
    """
    Reconstruct recent DM history from existing chatter
    events/messages. This deliberately avoids adding a
    new database table.

    Returns oldest -> newest conversation turns.
    """
    cursor = db.cursor(dictionary=True)

    cursor.execute(
        """
        SELECT id, extra_data
        FROM llm_chatter_events
        WHERE event_type = 'bot_player_whisper'
          AND id < %s
        ORDER BY id DESC
        LIMIT 40
        """,
        (current_event_id,),
    )

    rows = cursor.fetchall() or []
    matched = []

    for row in rows:
        raw = row.get('extra_data')

        try:
            if isinstance(raw, str):
                extra = json.loads(raw)
            elif isinstance(raw, dict):
                extra = raw
            else:
                continue
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

        if (
            _safe_int(extra.get('player_guid')) != player_guid
            or _safe_int(extra.get('bot_guid')) != bot_guid
        ):
            continue

        player_message = str(
            extra.get('player_message') or ''
        ).strip()

        if not player_message:
            continue

        reply_cursor = db.cursor(dictionary=True)

        reply_cursor.execute(
            """
            SELECT message
            FROM llm_chatter_messages
            WHERE event_id = %s
              AND bot_guid = %s
              AND channel = 'whisper'
            ORDER BY id ASC
            LIMIT 1
            """,
            (row['id'], bot_guid),
        )

        reply_row = reply_cursor.fetchone()
        bot_message = ''

        if reply_row:
            bot_message = str(
                reply_row.get('message') or ''
            ).strip()

        matched.append({
            'player': player_message,
            'bot': bot_message,
        })

        if len(matched) >= limit:
            break

    matched.reverse()
    return matched


def _format_history(history, player_name, bot_name):
    if not history:
        return "(No earlier private conversation.)"

    lines = []

    for turn in history:
        player_line = turn.get('player', '')
        bot_line = turn.get('bot', '')

        if player_line:
            lines.append(
                f"{player_name}: {player_line}"
            )

        if bot_line:
            lines.append(
                f"{bot_name}: {bot_line}"
            )

    if not lines:
        return "(No earlier private conversation.)"

    return "\n".join(lines)


def _format_traits(identity):
    traits = []

    for key in ('trait1', 'trait2', 'trait3'):
        value = str(identity.get(key) or '').strip()
        if value:
            traits.append(value)

    if not traits:
        return "(No stored personality traits.)"

    return ", ".join(traits)


def _format_bot_state(bot_state):
    if not isinstance(bot_state, dict) or not bot_state:
        return "(No authoritative live state available.)"

    try:
        return json.dumps(
            bot_state,
            ensure_ascii=False,
            separators=(',', ':'),
        )
    except (TypeError, ValueError):
        return "(Live state could not be serialized.)"


def _build_whisper_prompt(
    player_name,
    player_message,
    bot_name,
    identity,
    bot_state,
    history,
    playerbot_action_context="",
):
    traits = _format_traits(identity)
    history_text = _format_history(
        history,
        player_name,
        bot_name,
    )
    state_text = _format_bot_state(bot_state)

    return f"""
You are roleplaying {bot_name}, a real player character
on a Wrath of the Lich King-era World of Warcraft server.

This is a PRIVATE WHISPER conversation between you and
{player_name}. Nobody else can see the conversation.

Stay in character as an ordinary WoW player. Write like
someone actually typing in WoW whispers, not like an NPC,
assistant, narrator, or customer-service bot.

PERSONALITY TRAITS:
{traits}

AUTHORITATIVE LIVE CHARACTER STATE:
{state_text}

{playerbot_action_context}

The live state above is factual and authoritative for CURRENT
observable/mechanical character state. If it conflicts with prior
conversation about current level, exact quest progress, current
inventory/equipment/money, current location/activity, travel state,
or actual spell/service capability, use the live state.

General Wrath-era WoW knowledge is allowed even when it is not
listed in live state. You may accurately discuss quests, zones,
dungeons, mobs, NPCs, items, professions, class knowledge, leveling,
and common mechanics.

Plausible level/class-appropriate personal history, profession
history/plans, quests previously done, preferences, and future plans
may be improvised naturally and should remain consistent. Do not
turn them into unsupported CURRENT progress, possessions, or
observable local-world facts.

Harmless opinions, jokes, real-world topics, and conversational
personality may also be improvised naturally. Do not fall back to
"idk" merely because general knowledge or harmless conversation is
absent from live state.

RECENT PRIVATE CONVERSATION:
{history_text}

NEW WHISPER FROM {player_name}:
{player_message}

Rules:
- Respond as {bot_name}.
- Match the amount of detail to the message. A greeting or simple
  acknowledgement may be very short. Ordinary questions can use a full
  sentence, and sustained or genuinely complex conversation may use one
  or two natural sentences when useful.
- Keep it natural for WoW whisper chat.
- Different players type differently. Normal capitalization, complete
  sentences, and punctuation are common; lowercase, shorthand,
  fragments, abbreviations, missing punctuation, and occasional typos
  are also possible. Do not force any one texting style into every reply.
- Continue the existing conversation when there is one.
- Treat recent conversation as authoritative for what you and
  the player have already said; do not casually contradict it.
- Keep factual answers you already established consistent unless
  authoritative live state actually changes.
- When the player asks for clarification, become more specific when
  supported instead of retreating to a vaguer answer.
- Recognize and build on jokes, puns, references, corrections,
  and explanations introduced by the player.
- You can joke, disagree, ask a question, or be casual.
- Very short replies are fine when natural, but do not repeatedly
  default to lol, idk, or similar filler.
- Do not mention being an AI, bot, prompt, simulation,
  database, JSON, or language model.
- Do not claim to perform game actions that were not
  actually performed.
- Guild invitations and PlayerBots control commands are
  handled by the game server, not by this response.
- Do not prefix the message with "{bot_name}:".
- No quotation marks around the spoken message.

Return exactly one JSON object:
{{"message":"your whisper reply"}}
""".strip()


def process_player_bot_whisper_event(
    db,
    client,
    config,
    event,
):
    event_id = _safe_int(event.get('id'))

    extra = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'bot_player_whisper',
    )

    if not extra:
        mark_event(db, event_id, 'skipped')
        return False

    player_guid = _safe_int(
        extra.get('player_guid')
    )
    bot_guid = _safe_int(
        extra.get('bot_guid')
    )

    player_name = str(
        extra.get('player_name') or ''
    ).strip()

    bot_name = str(
        extra.get('bot_name')
        or event.get('subject_name')
        or ''
    ).strip()

    player_message = str(
        extra.get('player_message') or ''
    ).strip()

    bot_state = extra.get('bot_state')

    if (
        not event_id
        or not player_guid
        or not bot_guid
        or not player_name
        or not bot_name
        or not player_message
    ):
        mark_event(db, event_id, 'skipped')
        return False

    try:
        mark_event(db, event_id, 'processing')

        playerbot_action_result = None

        try:
            playerbot_action_result = (
                _dry_run_whisper_playerbot_intent(
                    db,
                    client,
                    config,
                    event_id=event_id,
                    player_name=player_name,
                    player_message=player_message,
                    bot_guid=bot_guid,
                    bot_name=bot_name,
                )
            )

            playerbot_action_result = (
                enqueue_live_playerbot_buff_actions(
                    db,
                    playerbot_action_result,
                    player_guid=player_guid,
                    player_name=player_name,
                    source_channel='whisper',
                    event_id=event_id,
                )
            )
        except Exception:
            logger.error(
                "[PLAYERBOT-DRYRUN] failed "
                "event=%s channel=whisper",
                event_id,
                exc_info=True,
            )

        playerbot_action_context = (
            build_playerbot_action_speech_context(
                playerbot_action_result,
                dry_run=not bool(
                    isinstance(
                        playerbot_action_result,
                        dict,
                    )
                    and playerbot_action_result.get(
                        'queue_accepted'
                    ) is True
                ),
                scope_label="private whisper",
            )
        )

        identity = _load_bot_identity(
            db,
            bot_guid,
        )

        history = _load_dm_history(
            db,
            event_id,
            player_guid,
            bot_guid,
            limit=6,
        )

        prompt = _build_whisper_prompt(
            player_name,
            player_message,
            bot_name,
            identity,
            bot_state,
            history,
            playerbot_action_context=(
                playerbot_action_context
            ),
        )

        from chatter_memory import (
            get_relationship_memory_context,
        )

        relationship_context = (
            get_relationship_memory_context(
                db,
                bot_guid,
                player_guid,
                player_name,
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
            max_tokens_override=160,
            context=(
                f"whisper:{player_guid}:"
                f"{bot_guid}:{event_id}"
            ),
            label='bot_player_whisper',
            metadata={
                'player_guid': player_guid,
                'bot_guid': bot_guid,
                'private_whisper': True,
            },
        )

        if not response:
            mark_event(db, event_id, 'skipped')
            return False

        parsed = parse_single_response(response)

        message = strip_speaker_prefix(
            parsed.get('message', ''),
            bot_name,
        )

        message = cleanup_message(
            message,
            action=parsed.get('action'),
        )

        if not message:
            mark_event(db, event_id, 'skipped')
            return False

        if len(message) > 255:
            message = message[:252] + '...'

        reply_delay = calculate_dynamic_delay(
            len(message),
            config,
            prev_message_length=len(
                player_message
            ),
            responsive=True,
        )

        insert_chat_message(
            db,
            bot_guid,
            bot_name,
            message,
            channel='whisper',
            delay_seconds=reply_delay,
            event_id=event_id,
            player_guid=player_guid,
            config=config,
            delivery_policy='responsive',
            delivery_reason='bot_player_whisper',
            owner_subsystem='whisper',
        )

        mark_event(db, event_id, 'completed')

        # Build a compact multi-turn transcript for durable
        # relationship memory. The shared memory helper applies
        # its own generation chance and cooldown.
        memory_lines = []

        for turn in history[-4:]:
            prior_player = str(
                turn.get('player') or ''
            ).strip()
            prior_bot = str(
                turn.get('bot') or ''
            ).strip()

            if prior_player:
                memory_lines.append(
                    f"{player_name}: {prior_player[:180]}"
                )
            if prior_bot:
                memory_lines.append(
                    f"{bot_name}: {prior_bot[:180]}"
                )

        memory_lines.append(
            f"{player_name}: {player_message[:220]}"
        )
        memory_lines.append(
            f"{bot_name}: {message[:220]}"
        )

        from chatter_memory import (
            queue_relationship_memory,
        )

        queue_relationship_memory(
            config,
            bot_guid,
            player_guid,
            event_context="\n".join(memory_lines),
            source='whisper_player',
            bot_name=bot_name,
            player_name=player_name,
        )

        logger.info(
            "bot_player_whisper player=%s bot=%s "
            "event=%s",
            player_name,
            bot_name,
            event_id,
        )

        return True

    except Exception:
        logger.error(
            "bot_player_whisper failed event=%s "
            "player=%s bot=%s",
            event_id,
            player_guid,
            bot_guid,
            exc_info=True,
        )

        try:
            mark_event(db, event_id, 'skipped')
        except Exception:
            pass

        return False
