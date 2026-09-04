"""Natural-language PlayerBot intent classification.

This module ONLY interprets player speech into a constrained
canonical intent. It does not enqueue requests and does not
execute PlayerBots actions.

The existing chatter "action" field is cosmetic/RP behavior.
Gameplay intent is deliberately represented separately here.
"""

import json
import logging
import re

from chatter_db import (
    PLAYERBOT_ACTION_KEYS,
    PLAYERBOT_ACTION_TARGET_TYPES,
    enqueue_playerbot_action,
)
from chatter_llm import quick_llm_analyze


logger = logging.getLogger(__name__)


_NO_ACTION = {
    'is_action_request': False,
    'action_key': None,
    'action_arg': None,
    'target_type': None,
    'target_name': None,
    'bot_hint': None,
    'confidence': 0.0,
}


LIVE_BUFF_SPELLS = frozenset({
    'Power Word: Fortitude',
    'Arcane Intellect',
    'Mark of the Wild',
    'Blessing of Kings',
    'Blessing of Wisdom',
    'Blessing of Might',
    'Water Breathing',
    'Unending Breath',
})


_CANONICAL_BUFF_PATTERNS = (
    (
        'Power Word: Fortitude',
        (
            r'\bpower word:\s*fortitude\b',
            r'\bfort(?:itude)?\b',
        ),
    ),
    (
        'Arcane Intellect',
        (
            r'\barcane intellect\b',
            r'(?<![a-z0-9])int(?![a-z0-9])',
            r'\bintellect\b',
        ),
    ),
    (
        'Mark of the Wild',
        (
            r'\bmark of the wild\b',
            r'\bmotw\b',
        ),
    ),
    (
        'Blessing of Kings',
        (
            r'\bblessing of kings\b',
            r'\bkings\b',
        ),
    ),
    (
        'Blessing of Wisdom',
        (
            r'\bblessing of wisdom\b',
            r'\bwisdom\b',
        ),
    ),
    (
        'Blessing of Might',
        (
            r'\bblessing of might\b',
            r'\bmight\b',
            r'\bbom\b',
        ),
    ),
    (
        'Water Breathing',
        (
            r'\bwater breathing\b',
            r'(?<![a-z0-9])wb(?![a-z0-9])',
        ),
    ),
    (
        'Unending Breath',
        (
            r'\bunending breath\b',
            r'\bunending\b',
        ),
    ),
)


def canonicalize_specific_buff(
    action_arg,
    player_message=None,
):
    """Return one allowlisted canonical buff name or None.

    Prefer the classifier's requested object. Fall back to
    the player message only when action_arg is absent. This
    prevents unrelated message text from overriding a
    different specifically classified spell.
    """
    action_raw = str(
        action_arg or ''
    ).strip()

    message_raw = str(
        player_message or ''
    ).strip()

    candidate_raw = (
        action_raw
        if action_raw
        else message_raw
    )

    if not candidate_raw:
        return None

    # Preserve capitalization for the ambiguous WoW shorthand
    # "BoW" (Blessing of Wisdom). Lowercase "bow" is a weapon
    # and must never be promoted to a buff solely by spelling.
    if re.search(
        r'(?<![A-Za-z0-9])(?:BoW|BOW)(?![A-Za-z0-9])',
        candidate_raw,
    ):
        return 'Blessing of Wisdom'

    candidate_text = candidate_raw.casefold()

    matches = [
        canonical
        for canonical, patterns
        in _CANONICAL_BUFF_PATTERNS
        if any(
            re.search(pattern, candidate_text)
            for pattern in patterns
        )
    ]

    if len(matches) != 1:
        return None

    canonical = matches[0]

    if canonical not in LIVE_BUFF_SPELLS:
        return None

    return canonical


def _clean_optional_text(value, max_len):
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    if len(value) > max_len:
        return None

    return value


def _normalize_confidence(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0

    return max(0.0, min(1.0, value))


def _looks_like_non_request(player_message):
    """Reject a few high-risk non-command constructions.

    This is intentionally narrow. The LLM still handles
    natural phrasing, but statements/questions that merely
    discuss an action must not trigger gameplay.
    """
    message = str(
        player_message or ''
    ).strip().casefold()

    if not message:
        return True

    patterns = (
        r"^(?:who|what|where|when|why|how)\b",
        r"^[\w'-]+\s+are\s+you\s+\w+ing\b",
        r"^are\s+you\s+\w+ing\b",
        r"^do\s+you\s+\w+\b",
        r"^should\s+we\b",
        r"\bprobably\s+need\b",
        r"\bneeds?\s+to\s+(?:die|be\s+killed)\b",
    )

    return any(
        re.search(pattern, message)
        for pattern in patterns
    )


def _extract_json_object(text):
    """Extract one JSON object from an LLM response."""
    if not text:
        return None

    text = str(text).strip()

    # Accept normal fenced JSON without trusting anything
    # outside the object.
    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\s*```$",
            "",
            text,
        ).strip()

    try:
        value = json.loads(text)

        if isinstance(value, dict):
            return value
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    # Last-resort extraction for models that prepend/append
    # a short sentence despite the JSON-only instruction.
    start = text.find("{")
    end = text.rfind("}")

    if start < 0 or end <= start:
        return None

    try:
        value = json.loads(
            text[start:end + 1]
        )
    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None

    return value if isinstance(value, dict) else None


def parse_playerbot_intent_response(
    response,
    *,
    available_bot_names=None,
    player_message=None,
):
    """Parse and strictly validate a classifier response."""
    raw = _extract_json_object(response)

    if not raw:
        return dict(_NO_ACTION)

    requested = raw.get(
        'is_action_request',
        False,
    )

    # Do not accept truthy strings like "yes".
    if requested is not True:
        return dict(_NO_ACTION)

    # Gameplay false positives are more costly than chatter
    # false negatives. Reject a narrow set of constructions
    # that are clearly status questions, planning, or
    # observations rather than instructions.
    if (
        player_message is not None
        and _looks_like_non_request(player_message)
    ):
        logger.info(
            "Rejected non-request PlayerBot phrasing: %r",
            player_message,
        )
        return dict(_NO_ACTION)

    action_key = str(
        raw.get('action_key') or ''
    ).strip().lower()

    if action_key not in PLAYERBOT_ACTION_KEYS:
        logger.warning(
            "Rejected PlayerBot intent action_key=%r",
            action_key,
        )
        return dict(_NO_ACTION)

    target_type = str(
        raw.get('target_type') or ''
    ).strip().lower()

    if target_type not in PLAYERBOT_ACTION_TARGET_TYPES:
        logger.warning(
            "Rejected PlayerBot intent target_type=%r",
            target_type,
        )
        return dict(_NO_ACTION)

    action_arg = _clean_optional_text(
        raw.get('action_arg'),
        255,
    )

    # Only an explicitly recognized buff may cross from the
    # classifier's free-form action_arg into the live buff
    # pipeline. Broad rebuff requests deliberately keep no
    # individual spell list; native Playerbots owns that.
    if action_key in {'cast_spell', 'rebuff'}:
        canonical_buff = canonicalize_specific_buff(
            action_arg,
            player_message,
        )

        if canonical_buff:
            action_key = 'cast_spell'
            action_arg = canonical_buff
        elif action_key == 'rebuff':
            action_arg = None

    target_name = _clean_optional_text(
        raw.get('target_name'),
        64,
    )
    bot_hint = _clean_optional_text(
        raw.get('bot_hint'),
        64,
    )

    # If the model supplied a specific bot name, accept it
    # only when it matches one of the candidates given by the
    # caller. Class hints such as "priest" or "mage" remain
    # valid because later resolution may intentionally use
    # class rather than name.
    if bot_hint and available_bot_names:
        names = {
            str(name).strip().casefold()
            for name in available_bot_names
            if str(name).strip()
        }

        class_words = {
            'warrior',
            'paladin',
            'hunter',
            'rogue',
            'priest',
            'death knight',
            'shaman',
            'mage',
            'warlock',
            'druid',
        }

        if (
            bot_hint.casefold() not in names
            and bot_hint.casefold() not in class_words
        ):
            logger.warning(
                "Discarded unknown PlayerBot bot_hint=%r",
                bot_hint,
            )
            bot_hint = None

    return {
        'is_action_request': True,
        'action_key': action_key,
        'action_arg': action_arg,
        'target_type': target_type or None,
        'target_name': target_name,
        'bot_hint': bot_hint,
        'confidence': _normalize_confidence(
            raw.get('confidence')
        ),
    }


def build_playerbot_intent_prompt(
    *,
    player_message,
    player_name,
    source_channel,
    bots,
):
    """Build the constrained classifier prompt."""
    bot_lines = []

    for bot in bots or []:
        name = str(
            bot.get('name')
            or bot.get('bot_name')
            or ''
        ).strip()
        bot_class = str(
            bot.get('class')
            or bot.get('bot_class')
            or ''
        ).strip()

        if not name:
            continue

        if bot_class:
            bot_lines.append(
                f"- {name}: {bot_class}"
            )
        else:
            bot_lines.append(
                f"- {name}"
            )

    bot_context = (
        "\n".join(bot_lines)
        if bot_lines
        else "- none supplied"
    )

    allowed_actions = ", ".join(
        sorted(PLAYERBOT_ACTION_KEYS)
    )

    return f"""You classify natural-language requests made by a real
World of Warcraft player to PlayerBots.

You DO NOT execute commands.
You DO NOT invent PlayerBots command syntax.
You only identify whether the player is asking a bot to
perform a gameplay action.

Player: {player_name}
Chat channel: {source_channel}

Available bots:
{bot_context}

Player message:
{player_message}

Allowed action_key values:
{allowed_actions}

Canonical meanings:
- cast_spell: cast a specifically requested spell/buff
- give_item: provide requested food, water, ammo, item, etc.
- trade: open/perform a requested trade interaction
- heal: player asks to be healed
- resurrect: player asks for a resurrection
- dispel: player asks for a cleanse/dispel/remove effect
- crowd_control: sheep, sap, fear, CC, etc.
- follow: follow/come with the player
- stay: stay/wait/hold position
- attack: attack/assist a requested target
- rebuff: generally buff/rebuff the group
- give_leader: give/pass group leadership

Interpret normal WoW shorthand:
"fort" can mean Power Word: Fortitude.
"fort me" means cast that buff on the requesting player.
"BoW" or "BOW" can mean Blessing of Wisdom in a buff request.
Lowercase "bow" means the weapon and must NOT mean Blessing of Wisdom.
"mage water" / "water pls" can be give_item.
"water breathing" / "wb pls" means the Shaman Water Breathing buff.
"unending breath" / "unending pls" means the Warlock Unending Breath buff.
The word "water" by itself is NOT Water Breathing.
"rez" means resurrect.
"sheep" commonly means mage polymorph.
"lead" can mean group leadership when context is clear.

A gameplay request must be an instruction, request, or
ordinary WoW imperative shorthand.

Imperative shorthand IS actionable when clear:
- "sheep skull" -> crowd_control
- "fort me" -> cast_spell
- "rez Ross" -> resurrect
- "follow me" -> follow

Do NOT classify discussion, planning, status questions,
predictions, wishes, or observations as gameplay requests:
- "fort is a good buff" -> not a request
- "should we sheep that?" -> not a request
- "we probably need buffs soon" -> not a request
- "Grim are you following me?" -> not a request
- "that caster needs to die" -> not a request
- "i died lol" -> not a request

A specific named buff/spell must use cast_spell.

Use rebuff for broad requests to make the group properly
buffed. PlayerBots will determine which normal class buffs
are actually missing and appropriate. Examples:
- "rebuff" -> rebuff
- "buff us" -> rebuff
- "buff the group" -> rebuff
- "we need buffs" -> rebuff
- "can we get buffs" -> rebuff

Do NOT invent or enumerate individual spells for a broad
rebuff request. If the player names one specific buff or
spell, use cast_spell instead.

bot_hint:
- Use a bot's exact name if the player addressed one.
- Otherwise a class such as "priest" or "mage" may be used
  when the request clearly selects that class.
- Return exactly ONE exact bot name OR ONE class value.
- Never combine a bot name with a class, description, comma,
  title, or other text.
- Otherwise null.

target_type:
- "player" when targeting a named/identified player
- "self" when the requesting player means themselves
- "unit" for an enemy/NPC target
- "bot" when targeting another bot
- "none" when no target is needed or known

action_arg:
Describe ONLY the requested gameplay object:
spell name, item category/name, or concise target/action
qualifier as appropriate.
Never put a raw PlayerBots command string here.

Return exactly one JSON object and nothing else:
{{
  "is_action_request": true or false,
  "action_key": "allowed value or null",
  "action_arg": "short value or null",
  "target_type": "self|player|bot|unit|none|null",
  "target_name": "name or null",
  "bot_hint": "bot name, class, or null",
  "confidence": 0.0
}}

If this is not clearly a gameplay request, return:
{{
  "is_action_request": false,
  "action_key": null,
  "action_arg": null,
  "target_type": null,
  "target_name": null,
  "bot_hint": null,
  "confidence": 0.0
}}"""


def classify_playerbot_intent(
    client,
    config,
    *,
    player_message,
    player_name,
    source_channel,
    bots,
):
    """Classify one message. Has NO gameplay side effects."""

    # Common WoW buff shorthand should not depend on an LLM
    # deciding whether it is actionable. "int?" is a standard
    # Arcane Intellect request.
    raw_message = str(player_message or '').strip()
    folded_message = raw_message.casefold()

    # If the message starts with an exact or unique >=3-character
    # prefix of a supplied bot name, remove that address only for
    # deterministic shorthand recognition.
    shorthand_message = raw_message

    first_match = re.match(
        r'^\s*([A-Za-z0-9_-]+)[,\s]+(.+)$',
        raw_message,
    )

    if first_match:
        prefix = first_match.group(1).casefold()
        remainder = first_match.group(2).strip()

        if len(prefix) >= 3:
            matching_names = [
                str(
                    bot.get('name')
                    or bot.get('bot_name')
                    or ''
                ).strip()
                for bot in (bots or [])
                if str(
                    bot.get('name')
                    or bot.get('bot_name')
                    or ''
                ).strip().casefold().startswith(prefix)
            ]

            if len(matching_names) == 1:
                shorthand_message = remainder

    shorthand_folded = shorthand_message.casefold().strip()

    terse_int = bool(
        re.fullmatch(
            r'int\s*[?!]*',
            shorthand_folded,
        )
    )

    explicit_int_request = bool(
        re.search(
            r'(?<![a-z0-9])int(?![a-z0-9])',
            shorthand_folded,
        )
        and re.search(
            r'\b(?:'
            r'can i (?:get|have)|'
            r'could i (?:get|have)|'
            r'can you (?:give|cast|buff)|'
            r'could you (?:give|cast|buff)|'
            r'would you (?:give|cast|buff)|'
            r'give me|cast|buff me|'
            r'please|pls|plz'
            r')\b',
            shorthand_folded,
        )
    )

    # Utility breathing buffs overlap lexically with Mage
    # conjured water, so resolve them deterministically before
    # asking the LLM. Bare "water pls" remains give_item.
    shorthand_folded = str(
        shorthand_message or ''
    ).strip().casefold()

    request_cue = bool(
        re.search(
            r'\b(?:can i|get|give|cast|buff|please|pls|plz)\b',
            shorthand_folded,
        )
        or shorthand_folded in {
            'wb',
            'wb?',
            'water breathing',
            'water breathing?',
            'unending',
            'unending?',
            'unending breath',
            'unending breath?',
        }
    )

    water_breathing_request = bool(
        request_cue
        and (
            re.search(
                r'\bwater breathing\b',
                shorthand_folded,
            )
            or re.search(
                r'(?<![a-z0-9])wb(?![a-z0-9])',
                shorthand_folded,
            )
        )
    )

    unending_breath_request = bool(
        request_cue
        and (
            re.search(
                r'\bunending breath\b',
                shorthand_folded,
            )
            or re.search(
                r'\bunending\b',
                shorthand_folded,
            )
        )
    )

    if water_breathing_request:
        parsed = {
            'is_action_request': True,
            'action_key': 'cast_spell',
            'action_arg': 'Water Breathing',
            'target_type': 'player',
            'target_name': player_name,
            'bot_hint': 'shaman',
            'confidence': 1.0,
        }

        logger.info(
            "[PLAYERBOT-INTENT] player=%s channel=%s "
            "message=%r result=%r "
            "deterministic=water_breathing",
            player_name,
            source_channel,
            raw_message[:160],
            parsed,
        )

        return parsed

    if unending_breath_request:
        parsed = {
            'is_action_request': True,
            'action_key': 'cast_spell',
            'action_arg': 'Unending Breath',
            'target_type': 'player',
            'target_name': player_name,
            'bot_hint': 'warlock',
            'confidence': 1.0,
        }

        logger.info(
            "[PLAYERBOT-INTENT] player=%s channel=%s "
            "message=%r result=%r "
            "deterministic=unending_breath",
            player_name,
            source_channel,
            raw_message[:160],
            parsed,
        )

        return parsed

    if terse_int or explicit_int_request:
        parsed = {
            'is_action_request': True,
            'action_key': 'cast_spell',
            'action_arg': 'Arcane Intellect',
            'target_type': 'player',
            'target_name': player_name,
            'bot_hint': 'mage',
            'confidence': 1.0,
        }

        logger.info(
            "[PLAYERBOT-INTENT] player=%s channel=%s "
            "message=%r result=%r deterministic=arcane_intellect",
            player_name,
            source_channel,
            raw_message[:160],
            parsed,
        )

        return parsed

    prompt = build_playerbot_intent_prompt(
        player_message=player_message,
        player_name=player_name,
        source_channel=source_channel,
        bots=bots,
    )

    response = quick_llm_analyze(
        client,
        config,
        prompt,
        max_tokens=160,
        label='playerbot_intent',
        metadata={
            'channel': source_channel,
            'player_name': player_name,
        },
    )

    names = [
        str(
            bot.get('name')
            or bot.get('bot_name')
            or ''
        ).strip()
        for bot in (bots or [])
    ]

    parsed = parse_playerbot_intent_response(
        response,
        available_bot_names=names,
        player_message=player_message,
    )

    if parsed.get('is_action_request') is True:
        required_class = _infer_required_class_hint(
            player_message,
            action_key=parsed.get('action_key'),
            action_arg=parsed.get('action_arg'),
        )

        if required_class:
            # Never substitute a merely available bot for a
            # class the player's request clearly requires.
            parsed['bot_hint'] = required_class

        elif parsed.get('action_key') == 'rebuff':
            # Broad rebuff should fan out unless the player
            # explicitly addressed a character by name.
            message_folded = str(
                player_message or ''
            ).casefold()

            explicit_name = any(
                name
                and re.search(
                    r'(?<![a-z0-9])'
                    + re.escape(name.casefold())
                    + r'(?![a-z0-9])',
                    message_folded,
                )
                for name in names
            )

            if not explicit_name:
                parsed['bot_hint'] = None

    logger.info(
        "[PLAYERBOT-INTENT] player=%s channel=%s "
        "message=%r result=%r",
        player_name,
        source_channel,
        player_message[:160],
        parsed,
    )

    return parsed


def should_analyze_playerbot_intent(
    player_message,
):
    """Cheap request-likelihood gate before QuickAnalyze.

    This does NOT determine whether an action should happen.
    It only decides whether a player message is plausible
    enough to justify an LLM intent-classification call.

    False positives here are harmless apart from one cheap
    classification call. The classifier/parser/resolver and
    PlayerBots execution layer remain authoritative.
    """
    message = str(
        player_message or ''
    ).strip().casefold()

    if not message:
        return False

    # A plain acknowledgement is not a fresh gameplay request.
    # Requests appended to the acknowledgement still pass.
    if (
        re.search(
            r'^(?:thanks|thank you|ty)\b',
            message,
        )
        and not re.search(
            r'\b(?:can you|could you|would you|'
            r'can i|get me|give me|please|pls|plz)\b',
            message,
        )
    ):
        return False

    # Strong WoW action vocabulary and common shorthand.
    action_terms = (
        r'\bbuffs?\b',
        r'\brebuff\b',
        r'\bfort(?:itude)?\b',
        r'\bint(?:ellect)?\b',
        r'\bmotw\b',
        r'\bmark\b',
        r'\bkings\b',
        r'\bwisdom\b',
        r'\bmight\b',

        r'\bwater\b',
        r'\bfood\b',
        r'\bunending\b',
        r'\bbreath(?:ing)?\b',

        r'\bheal(?:s|ing)?\b',
        r'\brez\b',
        r'\bres\b',
        r'\bresurrect\b',
        r'\bcleanse\b',
        r'\bdispel\b',

        r'\bsheep\b',
        r'\bpoly\b',
        r'\bpolymorph\b',
        r'\bhex\b',
        r'\bsap\b',
        r'\bcc\b',

        r'\bfollow\b',
        r'\bstay\b',
        r'\bwait here\b',

        r'\battack\b',
        r'\bkill\b',

        r'\btrade\b',

        r'\bpass (?:me )?(?:lead|leader)\b',
        r'\bgive (?:me )?(?:lead|leader)\b',
    )

    if any(
        re.search(pattern, message)
        for pattern in action_terms
    ):
        return True

    # Natural request language catches specifically requested
    # spells/items we cannot realistically enumerate here.
    #
    # Examples:
    #   "can you cast X"
    #   "could you give me X"
    #   "can I get X"
    #   "X please"
    request_patterns = (
        r'\bcan you\b',
        r'\bcould you\b',
        r'\bwould you\b',
        r'\bcan i (?:get|have)\b',
        r'\bcould i (?:get|have)\b',
        r'\bplease\b',
        r'\bpls\b',
        r'\bplz\b',
    )

    return any(
        re.search(pattern, message)
        for pattern in request_patterns
    )


WOW_CLASS_NAMES = {
    1: 'warrior',
    2: 'paladin',
    3: 'hunter',
    4: 'rogue',
    5: 'priest',
    6: 'death knight',
    7: 'shaman',
    8: 'mage',
    9: 'warlock',
    11: 'druid',
}


def _infer_required_class_hint(
    player_message,
    action_key=None,
    action_arg=None,
):
    """Infer narrow, obvious WoW class requirements."""
    message = str(
        player_message or ''
    ).strip().casefold()

    arg = str(
        action_arg or ''
    ).strip().casefold()

    # Explicitly addressed class takes priority.
    class_names = (
        'death knight',
        'warrior',
        'paladin',
        'hunter',
        'rogue',
        'priest',
        'shaman',
        'mage',
        'warlock',
        'druid',
    )

    for class_name in class_names:
        if re.search(
            r'(?<![a-z])'
            + re.escape(class_name)
            + r'(?![a-z])',
            message,
        ):
            return class_name

    combined = f"{message} {arg}"

    ability_classes = (
        (
            'mage',
            (
                r'\bsheep\b',
                r'\bpoly(?:morph)?\b',
                r'\barcane intellect\b',
                r'\bintellect\b',
            ),
        ),
        (
            'priest',
            (
                r'\bfort(?:itude)?\b',
                r'\bpower word:\s*fortitude\b',
            ),
        ),
        (
            'druid',
            (
                r'\bmotw\b',
                r'\bmark of the wild\b',
            ),
        ),
        (
            'paladin',
            (
                r'\bblessing of kings\b',
                r'\bkings\b',
                r'\bblessing of wisdom\b',
                r'\bwisdom\b',
                r'\bblessing of might\b',
                r'\bmight\b',
                r'\bbom\b',
            ),
        ),
        (
            'shaman',
            (
                r'\bhex\b',
                r'\bwater breathing\b',
                r'(?<![a-z0-9])wb(?![a-z0-9])',
            ),
        ),
        (
            'warlock',
            (
                r'\bunending breath\b',
                r'\bunending\b',
            ),
        ),
        (
            'rogue',
            (
                r'\bsap\b',
            ),
        ),
    )

    for class_name, patterns in ability_classes:
        if any(
            re.search(pattern, combined)
            for pattern in patterns
        ):
            return class_name

    # Conjured water/food is a Mage service request.
    if (
        str(action_key or '').strip().casefold()
        == 'give_item'
        and re.search(
            r'\b(?:water|food)\b',
            combined,
        )
    ):
        return 'mage'

    return None


def _normalize_playerbot_candidate(candidate):
    """Normalize one candidate for deterministic resolution."""
    if not isinstance(candidate, dict):
        return None

    try:
        guid = int(
            candidate.get('guid')
            or candidate.get('bot_guid')
            or 0
        )
        class_id = int(
            candidate.get('class_id')
            or candidate.get('bot_class')
            or candidate.get('class')
            or 0
        )
    except (TypeError, ValueError):
        return None

    name = str(
        candidate.get('name')
        or candidate.get('bot_name')
        or ''
    ).strip()

    if (
        guid <= 0
        or not name
        or class_id not in WOW_CLASS_NAMES
    ):
        return None

    result = dict(candidate)
    result['guid'] = guid
    result['name'] = name
    result['class_id'] = class_id
    result['class_name'] = WOW_CLASS_NAMES[
        class_id
    ]

    return result


def enqueue_live_playerbot_buff_actions(
    db,
    action_result,
    *,
    player_guid,
    player_name,
    source_channel,
    group_id=None,
    event_id=None,
):
    """Queue only explicitly allowlisted buff actions.

    All other PlayerBot intents remain classification-only.
    C++ independently revalidates the action, spell, channel,
    player, bot, target, map, range, and native result.
    """
    if not isinstance(action_result, dict):
        return action_result

    action_result['queue_attempted'] = False
    action_result['queue_accepted'] = False
    action_result['queued'] = []

    intent = action_result.get('intent') or {}
    resolved = action_result.get('resolved') or []

    if intent.get('is_action_request') is not True:
        return action_result

    action_key = str(
        intent.get('action_key') or ''
    ).strip().casefold()

    action_arg = str(
        intent.get('action_arg') or ''
    ).strip()

    channel = str(
        source_channel or ''
    ).strip().casefold()

    if action_key not in {'cast_spell', 'rebuff'}:
        return action_result

    if channel not in {'say', 'whisper', 'party', 'raid'}:
        logger.warning(
            "Refused live buff queue for channel=%r",
            channel,
        )
        return action_result

    if (
        action_key == 'cast_spell'
        and action_arg not in LIVE_BUFF_SPELLS
    ):
        logger.warning(
            "Refused non-allowlisted live buff spell=%r",
            action_arg,
        )
        return action_result

    if action_key == 'rebuff' and action_arg:
        logger.warning(
            "Refused rebuff with unexpected action_arg=%r",
            action_arg,
        )
        return action_result

    if not resolved:
        return action_result

    try:
        player_guid = int(player_guid or 0)
        group_id = int(group_id or 0)
        event_id = int(event_id or 0)
    except (TypeError, ValueError):
        return action_result

    player_name = str(
        player_name or ''
    ).strip()

    if player_guid <= 0 or not player_name:
        return action_result

    action_result['queue_attempted'] = True
    queued = []

    for bot in resolved:
        try:
            bot_guid = int(
                bot.get('guid')
                or bot.get('bot_guid')
                or 0
            )
        except (AttributeError, TypeError, ValueError):
            bot_guid = 0

        if bot_guid <= 0:
            continue

        try:
            action_id = enqueue_playerbot_action(
                db,
                player_guid=player_guid,
                bot_guid=bot_guid,
                source_channel=channel,
                action_key=action_key,
                action_arg=(
                    action_arg
                    if action_key == 'cast_spell'
                    else None
                ),
                target_type=(
                    'player'
                    if action_key == 'cast_spell'
                    else 'none'
                ),
                target_guid=(
                    player_guid
                    if action_key == 'cast_spell'
                    else None
                ),
                target_name=(
                    player_name
                    if action_key == 'cast_spell'
                    else None
                ),
                group_id=group_id or None,
                event_id=event_id or None,
                expires_seconds=45,
            )
        except Exception:
            logger.error(
                "[PLAYERBOT-BUFF] queue failed "
                "event=%s player=%s bot_guid=%s "
                "channel=%s action=%s arg=%r",
                event_id,
                player_name,
                bot_guid,
                channel,
                action_key,
                action_arg,
                exc_info=True,
            )
            continue

        queued.append({
            'action_id': int(action_id),
            'bot_guid': bot_guid,
            'bot_name': str(
                bot.get('name')
                or bot.get('bot_name')
                or ''
            ).strip(),
        })

    action_result['queued'] = queued
    action_result['queue_accepted'] = bool(queued)

    logger.info(
        "[PLAYERBOT-BUFF] event=%s player=%s "
        "channel=%s action=%s arg=%r queued=%s",
        event_id,
        player_name,
        channel,
        action_key,
        action_arg,
        queued,
    )

    return action_result


def build_playerbot_action_speech_context(
    action_result,
    *,
    dry_run=True,
    scope_label="current chat context",
):
    """Build authoritative speech guidance for an action result.

    This is channel-agnostic. Candidate discovery and routing remain
    the responsibility of the caller.
    """
    if not isinstance(action_result, dict):
        return ""

    parsed_intent = action_result.get('intent') or {}

    if parsed_intent.get('is_action_request') is not True:
        return ""

    resolved = action_result.get('resolved') or []

    action_key = str(
        parsed_intent.get('action_key') or ''
    ).strip()

    action_arg = str(
        parsed_intent.get('action_arg') or ''
    ).strip()

    lines = [
        "<playerbot_action_status>",
        (
            "AUTHORITATIVE ACTION STATUS: The player's message "
            "was recognized as a gameplay action request."
        ),
    ]

    queued = action_result.get('queued') or []

    if dry_run:
        lines.append(
            "This system is currently in DRY-RUN mode. "
            "No gameplay action was queued, accepted, or executed."
        )
        lines.append(
            "Your reply MUST clearly decline the requested "
            "gameplay action because it was not accepted."
        )
        lines.append(
            "Do NOT say or imply that you are casting, giving, "
            "trading, following, buffing, crowd-controlling, "
            "or otherwise performing the requested action."
        )
        lines.append(
            "Never answer with agreement or future-action wording "
            "such as 'sure', 'okay', 'give me a sec', or 'I will'."
        )
    elif queued:
        lines.append(
            "The allowlisted buff request was accepted into the "
            "gameplay queue for native Playerbots execution."
        )
        lines.append(
            "This confirms only queue acceptance, not successful "
            "casting or completion. Native validation may still "
            "reject the request."
        )
        lines.append(
            "A brief acknowledgement is allowed, but queue acceptance "
            "does NOT mean the cast is happening now. Keep it tentative "
            "and natural, such as 'yeah, sec' or 'sure, after this'. "
            "Do NOT say or imply 'casting now', 'I'll cast it now', "
            "'done', or otherwise claim the buff has started, succeeded, "
            "or completed."
        )
    else:
        lines.append(
            "No gameplay action was accepted into the queue."
        )
        lines.append(
            "Do NOT promise or imply that the requested action "
            "will happen."
        )

    if action_key:
        detail = f"Recognized action: {action_key}"

        if action_arg:
            detail += f" ({action_arg})"

        lines.append(detail + ".")

    if resolved:
        names = [
            str(bot.get('name') or '').strip()
            for bot in resolved
            if str(bot.get('name') or '').strip()
        ]

        if names:
            lines.append(
                "Eligible bot resolution: "
                + ", ".join(names)
                + "."
            )

        if dry_run:
            lines.append(
                "Even an eligible bot must NOT promise that the "
                "action is happening, because execution is disabled."
            )
    else:
        scope = str(
            scope_label or "current chat context"
        ).strip()

        lines.append(
            f"No eligible bot resolved in the {scope}."
        )
        lines.append(
            "Do NOT volunteer that you or another available bot "
            "can perform it. A brief response that the proper "
            "class or capability is needed is allowed, or simply "
            "avoid making a promise."
        )

    lines.append("</playerbot_action_status>")

    return "\n".join(lines)


def resolve_playerbot_action_candidates(
    intent,
    candidates,
    *,
    source_channel='party',
    whisper_bot_guid=None,
    addressed_name=None,
):
    """Resolve a canonical intent to concrete candidate bot(s).

    Resolution is deterministic and side-effect free.

    Rules:
    - exact bot-name hint wins;
    - class hint selects one matching bot deterministically;
    - broad untargeted rebuff fans out to all eligible bots;
    - whisper may be constrained to its actual receiver;
    - untargeted non-rebuff actions remain unresolved.

    Mechanical capability checks still belong to PlayerBots
    at execution time.
    """
    if (
        not isinstance(intent, dict)
        or intent.get('is_action_request') is not True
    ):
        return []

    action_key = str(
        intent.get('action_key') or ''
    ).strip().casefold()

    if action_key not in PLAYERBOT_ACTION_KEYS:
        return []

    normalized = []

    seen_guids = set()

    for raw in candidates or []:
        candidate = _normalize_playerbot_candidate(
            raw
        )

        if candidate is None:
            continue

        guid = candidate['guid']

        if guid in seen_guids:
            continue

        seen_guids.add(guid)
        normalized.append(candidate)

    if not normalized:
        return []

    normalized.sort(
        key=lambda b: (
            b['guid'],
            b['name'].casefold(),
        )
    )

    channel = str(
        source_channel or ''
    ).strip().casefold()

    # C++ proximity routing already resolves explicit full names
    # and unique partial names to one authoritative nearby bot.
    # Preserve that ownership for /say gameplay requests.
    if channel == 'say':
        addressed = str(
            addressed_name or ''
        ).strip().casefold()

        if addressed:
            addressed_bot = next(
                (
                    bot
                    for bot in normalized
                    if bot['name'].casefold() == addressed
                ),
                None,
            )

            if addressed_bot is None:
                return []

            hint = str(
                intent.get('bot_hint') or ''
            ).strip().casefold()

            class_names = {
                str(name).casefold()
                for name in WOW_CLASS_NAMES.values()
            }

            # If the request itself authoritatively requires a
            # different class, do not silently redirect the named
            # request to somebody else.
            if (
                hint in class_names
                and addressed_bot['class_name'] != hint
            ):
                return []

            return [addressed_bot]

    # A whisper is inherently addressed to its actual
    # receiver. Never redirect it to a different bot merely
    # because the LLM produced another hint.
    if channel == 'whisper':
        try:
            forced_guid = int(
                whisper_bot_guid or 0
            )
        except (TypeError, ValueError):
            forced_guid = 0

        if forced_guid <= 0:
            return []

        receiver = next(
            (
                bot
                for bot in normalized
                if bot['guid'] == forced_guid
            ),
            None,
        )

        if receiver is None:
            return []

        hint = str(
            intent.get('bot_hint') or ''
        ).strip().casefold()

        if hint:
            valid_receiver_hints = {
                receiver['name'].casefold(),
                receiver['class_name'],
            }

            if hint not in valid_receiver_hints:
                return []

        return [receiver]

    hint = str(
        intent.get('bot_hint') or ''
    ).strip().casefold()

    if hint:
        # Exact character name has highest priority.
        name_matches = [
            bot
            for bot in normalized
            if bot['name'].casefold() == hint
        ]

        if name_matches:
            return [name_matches[0]]

        # Otherwise treat an allowed class name as the
        # selector. Local /say uses physical proximity; other
        # channels retain deterministic GUID ordering.
        class_matches = [
            bot
            for bot in normalized
            if bot['class_name'] == hint
        ]

        if class_matches:
            if channel == 'say':
                def _say_distance(bot):
                    try:
                        return float(bot.get('distance'))
                    except (TypeError, ValueError):
                        return float('inf')

                class_matches.sort(
                    key=lambda bot: (
                        _say_distance(bot),
                        bot['guid'],
                        bot['name'].casefold(),
                    )
                )

            return [class_matches[0]]

        return []

    # A broad rebuff request means each eligible bot should
    # evaluate its own normal class-buff responsibilities.
    if action_key == 'rebuff':
        return normalized

    # Do not guess which bot should perform an otherwise
    # untargeted gameplay action. Later capability-aware
    # routing may safely resolve these cases.
    return []
