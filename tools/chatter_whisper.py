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

logger = logging.getLogger(__name__)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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

The live state above is factual. If the player asks about
your guild, level, location, activity, quests, equipment,
money, professions, inventory, travel, or similar current
facts, use that state and do not invent conflicting facts.

If a fact is not available, respond naturally without
inventing precise numbers or specific possessions.

RECENT PRIVATE CONVERSATION:
{history_text}

NEW WHISPER FROM {player_name}:
{player_message}

Rules:
- Respond as {bot_name}.
- Usually write 1 short sentence; occasionally 2.
- Keep it natural for WoW whisper chat.
- Continue the existing conversation when there is one.
- You can joke, disagree, ask a question, or be casual.
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
