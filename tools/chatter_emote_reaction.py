"""Emote reaction handler -- THIS bot was targeted
directly by a player emote. Personal verbal response
after the C++ mirror emote."""

import random

from chatter_constants import (
    EMOTE_CATEGORIES,
    EMOTE_NAME_TO_ID,
    REACTION_TONES,
    CLASS_NAMES,
    RACE_NAMES,
)
from chatter_shared import (
    parse_extra_data,
    run_single_reaction,
    build_bot_identity,
    append_json_instruction,
    get_gender_label,
    get_chatter_mode,
)
from chatter_group_state import (
    _mark_event,
    _store_chat,
    get_bot_traits,
)

_DEFAULT_TONES = [
    "with dry wit", "with humor",
    "with curiosity", "briefly",
]


def _pick_tone(category: str) -> str:
    pool = REACTION_TONES.get(
        category, _DEFAULT_TONES
    )
    return random.choice(pool)


def handle_emote_reaction(db, client, config, event):
    """THIS bot was targeted directly -- personal
    verbal response after the C++ mirror emote."""
    event_id = event['id']
    extra = parse_extra_data(
        event.get('extra_data'),
        event_id,
        'bot_group_emote_reaction',
    )
    if not extra:
        _mark_event(db, event_id, 'skipped')
        return False

    emote = extra.get('emote_name', 'wave')
    p_name = extra.get('player_name', 'someone')
    bot_name = extra.get('bot_name', 'Bot')
    group_id = int(extra.get('group_id') or 0)
    bot_guid = int(extra.get('bot_guid') or 0)
    bot_class = CLASS_NAMES.get(
        int(extra.get('bot_class') or 0), ''
    )
    bot_race = RACE_NAMES.get(
        int(extra.get('bot_race') or 0), ''
    )
    bot_gender = get_gender_label(
        int(extra.get('bot_gender') or 0)
    )

    emote_id = EMOTE_NAME_TO_ID.get(emote, 0)
    category = EMOTE_CATEGORIES.get(
        emote_id, 'greeting'
    )
    trait_data = get_bot_traits(
        db, group_id, bot_guid
    ) if group_id and bot_guid else None
    traits = (
        trait_data.get('traits', [])
        if trait_data else []
    )
    stored_tone = (
        trait_data.get('tone')
        if trait_data else None
    )

    prompt = _build_reaction_prompt(
        bot_name, bot_race, bot_class,
        bot_gender,
        p_name, emote, category,
        traits=traits,
        stored_tone=stored_tone,
        config=config,
    )

    result = run_single_reaction(
        db, client, config,
        prompt=prompt,
        speaker_name=bot_name,
        bot_guid=bot_guid,
        channel='party',
        delay_seconds=2,
        event_id=event_id,
        allow_emote_fallback=True,
        context=(
            f"emote-react:#{event_id}:{bot_name}"
        ),
        bypass_speaker_cooldown=True,
        label='reaction_emote',
        group_id=group_id,
        delivery_policy='responsive',
        delivery_reason='bot_group_emote_reaction',
    )
    if not result['ok']:
        _mark_event(db, event_id, 'skipped')
        return False

    _store_chat(
        db, group_id, bot_guid,
        bot_name, True, result['message'],
    )
    return True


def _build_reaction_prompt(
    bot_name, bot_race, bot_class, bot_gender,
    p_name, emote, category,
    traits=None,
    stored_tone=None,
    config=None,
):
    mode = (
        get_chatter_mode(config)
        if config else 'normal'
    )
    is_rp = (mode == 'roleplay')

    if is_rp:
        tone = stored_tone or _pick_tone(category)

        prompt = build_bot_identity(
            bot_name, bot_race,
            bot_class, bot_gender,
        )

        if traits:
            prompt += (
                " Your personality: "
                f"{', '.join(traits)}."
            )

        prompt += (
            f" Your tone: {tone}. "
            f"Your party member {p_name} "
            f"just /{emote} at you. "
            f"React {tone}. "
            "1-2 sentences. "
            "NEVER put /slash commands in your "
            "response."
        )

    else:
        prompt = (
            f"You are {bot_name}, a real WoW player. "
            f"Your party member {p_name} just used "
            f"/{emote} directly at you. "
            "Reply like a real player casually reacting "
            "in party chat. Keep it very short, usually "
            "1-6 words. One word is completely fine. "
            "Use the meaning of the emote naturally. "
            "A wave might get 'hey', 'yo', or 'sup'. "
            "A thank might get 'np'. "
            "A cheer might get 'lol ty' or 'haha'. "
            "A rude or silly emote might get 'bruh', "
            "'lol', '???', or mild annoyance. "
            "These are examples, not required phrases. "
            "Do not force slang, humor, or a clever response. "
            "Do not roleplay or narrate. "
            "Do not explain what the emote means. "
            "Do not put /slash commands in the response."
        )

    return append_json_instruction(prompt)