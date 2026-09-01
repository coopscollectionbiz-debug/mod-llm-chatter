"""Ambient chatter runtime processors.

N9/N10 moved statement and conversation
processing from the bridge.
"""

import logging
import random
import time
from typing import List
import json

from chatter_constants import (
    CAPITAL_CITY_ZONES,
    AMBIENT_CHAT_TOPICS,
    AMBIENT_CHAT_TOPICS_RP,
)
from chatter_shared import (
    zone_cache,
    parse_single_response,
    parse_conversation_response,
    can_class_use_item,
    query_zone_quests,
    query_zone_loot,
    query_zone_mobs,
    query_bot_spells,
    replace_placeholders,
    cleanup_message,
    strip_speaker_prefix,
    call_llm,
    insert_chat_message,
    get_recent_zone_messages,
    is_too_similar,
    select_message_type,
    calculate_dynamic_delay,
    get_chatter_mode,
    _zone_last_delivery,
    _zone_delivery_delay,
    get_zone_name,
    get_zone_flavor,
    get_subzone_name,
    get_subzone_lore,
    build_conversation_json_repair_prompt,
)
from chatter_shared import (
    build_talent_context,
    build_zone_metadata,
    should_include_action,
    format_item_link,
)
from chatter_db import (
    query_zone_bot_gossip_targets,
    query_zone_npcs,
    query_live_auction_market,
    query_city_trade_items,
    query_profession_services,
)
from chatter_group_general_reaction import (
    maybe_queue_group_general_reaction,
)
from chatter_text import pick_statement_length
from chatter_prompts import (
    build_plain_statement_prompt,
    build_quest_statement_prompt,
    build_loot_statement_prompt,
    build_quest_reward_statement_prompt,
    build_spell_statement_prompt,
    build_trade_statement_prompt,
    build_plain_conversation_prompt,
    build_quest_conversation_prompt,
    build_loot_conversation_prompt,
    build_trade_conversation_prompt,
    build_spell_conversation_prompt,
    build_gossip_statement_prompt,
    build_gossip_conversation_prompt,
)

logger = logging.getLogger(__name__)

_gossip_target_cooldowns = {}

# Ambient public-chat personas.
#
# These are intentionally atmospheric rather than transactional.
# A repeated guild/raid advertisement should remain internally
# consistent for a while even though it is not an actionable
# gameplay offer.
_city_guild_ad_personas = {}
_city_lfg_ad_personas = {}
_city_boost_ad_personas = {}
_city_profession_ad_personas = {}
_city_social_recent_lines = []


def _city_bot_persona_key(bot):
    guid = str(bot.get('guid') or '').strip()

    if guid:
        return f"guid:{guid}"

    name = str(bot.get('name') or '').strip()

    if name:
        return f"name:{name.lower()}"

    return f"anon:{id(bot)}"


def _get_cached_city_ad(cache, bot):
    key = _city_bot_persona_key(bot)
    now = time.time()
    state = cache.get(key)

    if (
        isinstance(state, dict)
        and float(state.get('expires_at') or 0) > now
        and state.get('message')
    ):
        return state['message']

    if state:
        cache.pop(key, None)

    return None


def _cache_city_ad(cache, bot, message):
    key = _city_bot_persona_key(bot)

    # Real advertisers commonly repeat the same exact copy for
    # tens of minutes instead of rewriting it every time.
    lifetime = random.randint(20 * 60, 60 * 60)

    cache[key] = {
        'message': message,
        'expires_at': time.time() + lifetime,
    }

    return message


def _build_zone_metadata(zone_id, area_id=0):
    """Build zone metadata dict for request logging.

    Thin wrapper around build_zone_metadata() that
    resolves zone/subzone names from IDs first.
    """
    return build_zone_metadata(
        zone_name=get_zone_name(zone_id) or '',
        zone_flavor=get_zone_flavor(zone_id) or '',
        subzone_name=(
            get_subzone_name(zone_id, area_id) or ''
        ),
        subzone_lore=(
            get_subzone_lore(zone_id, area_id) or ''
        ),
    )


def _fetch_loot_data(config, zone_id, level):
    """Fetch and select a loot item for the zone.

    Handles: query_zone_loot, cooldown filter,
    quality weights, random.choices, mark_loot_seen.

    Returns item_data dict or None if no loot found.
    """
    loot = query_zone_loot(config, zone_id, level)
    if not loot:
        return None
    cooldown = int(config.get(
        'LLMChatter.LootRecentCooldownSeconds', 0
    ))
    if cooldown > 0:
        recent_ids = (
            zone_cache.get_recent_loot_ids(
                zone_id, cooldown
            )
        )
        filtered = [
            item for item in loot
            if item.get('item_id')
            not in recent_ids
        ]
        if filtered:
            loot = filtered
    quality_weights = {
        0: 35, 1: 30, 2: 22, 3: 10, 4: 3
    }
    weights = [
        quality_weights.get(
            item.get('item_quality', 2), 10
        )
        for item in loot
    ]
    item_data = random.choices(
        loot, weights=weights, k=1
    )[0]
    if cooldown > 0 and item_data.get('item_id'):
        zone_cache.mark_loot_seen(
            zone_id, item_data['item_id']
        )
    return item_data


CAPITAL_CITY_ZONE_NAMES = {
    'Stormwind City',
    'Ironforge',
    'Darnassus',
    'The Exodar',
    'Orgrimmar',
    'Undercity',
    'Thunder Bluff',
    'Silvermoon City',
    'Shattrath City',
    'Dalaran',
}


def _is_capital_city_name(zone_name):
    """Return True when the ambient request is from a capital city."""
    return str(zone_name or '').strip() in CAPITAL_CITY_ZONE_NAMES



def _is_alliance_bot(bot):
    """Best-effort faction check using the bot's authoritative race."""
    race = str(bot.get('race', '')).strip().lower()
    return race in {
        'human',
        'dwarf',
        'night elf',
        'gnome',
        'draenei',
    }


def _city_template_item(config):
    """
    Pick a dynamically discovered direct-trade-worthy item.

    Returns the structured item row plus its clickable link.
    """
    items = query_city_trade_items(
        config,
        limit=300,
    )

    if not items:
        return None

    item = random.choice(items)

    try:
        item_entry = int(
            item.get('item_entry') or 0
        )
        item_quality = int(
            item.get('item_quality') or 1
        )
    except (TypeError, ValueError):
        return None

    name = str(
        item.get('item_name') or ''
    ).strip()

    if not item_entry or not name:
        return None

    result = dict(item)

    result['link'] = format_item_link(
        item_entry,
        item_quality,
        name,
    )

    return result


def _city_lfg_content(bot):
    """Choose specific level-appropriate dungeon/raid content."""
    try:
        level = int(bot.get('level') or 1)
    except (TypeError, ValueError):
        level = 1

    alliance = _is_alliance_bot(bot)

    if level < 18:
        content = (
            ['Deadmines']
            if alliance
            else ['Ragefire Chasm', 'Wailing Caverns']
        )
    elif level < 25:
        content = (
            ['Deadmines', 'Stockades']
            if alliance
            else ['Wailing Caverns', 'Shadowfang Keep']
        )
    elif level < 40:
        content = [
            'Scarlet Monastery',
            'Razorfen Kraul',
            'Gnomeregan',
        ]
    elif level < 50:
        content = [
            "Zul'Farrak",
            'Maraudon',
            'Uldaman',
        ]
    elif level < 60:
        content = [
            'BRD',
            'LBRS',
            'Stratholme',
            'Scholomance',
        ]
    elif level < 68:
        content = [
            'Ramparts',
            'Blood Furnace',
            'Slave Pens',
            'Underbog',
            'Mana-Tombs',
        ]
    elif level < 73:
        content = [
            'UK',
            'Nexus',
            'Azjol-Nerub',
            'Ahnkahet',
        ]
    elif level < 78:
        content = [
            "Drak'Tharon",
            'Violet Hold',
            'Gundrak',
        ]
    elif level < 80:
        content = [
            'Halls of Stone',
            'Halls of Lightning',
            'Oculus',
            'Culling of Stratholme',
        ]
    else:
        content = [
            'H UK',
            'H Nexus',
            'H Gundrak',
            'H HoL',
            'H CoS',
            'Naxx10',
            'Naxx25',
            'VoA10',
            'VoA25',
            'OS10',
            'Ulduar10',
            'Ulduar25',
            'ToC10',
            'ToC25',
            'ICC10',
            'ICC25',
        ]

    return random.choice(content)


def _build_city_lfg(bot):
    """
    Build Blizzard-style ambient LFG/LFM chatter.

    This is atmospheric public chat, not an actionable group
    contract, so plausible open slots, role needs, raid times,
    and loot-rule shorthand are allowed.
    """
    content = _city_lfg_content(bot)

    raid_tokens = (
        'Naxx',
        'VoA',
        'OS',
        'Uld',
        'ToC',
        'ICC',
    )

    is_raid = any(
        token.lower() in content.lower()
        for token in raid_tokens
    )

    # Some organizer-style posts persist and are reposted.
    cached = _get_cached_city_ad(
        _city_lfg_ad_personas,
        bot,
    )

    if cached and random.randint(1, 100) <= 45:
        return cached

    if is_raid:
        need = random.choice([
            "need 1 tank + heals",
            "need tank/heals",
            "LF heals + ranged",
            "LF 2 heals",
            "LF tank",
            "need ranged dps",
            "LF lock/mage",
            "LF sham + heals",
            "need a few dps",
            "LF1M heals",
        ])

        time_text = random.choice([
            "",
            "",
            " @ 7pm ST",
            " @ 8pm ST",
            " tonight 8 ST",
            " tonight",
            " in 30",
        ])

        loot = random.choice([
            "",
            "",
            "",
            " 2SR",
            " 2SR MS>OS",
            " MS>OS",
            " MS>OS +1",
        ])

        message = random.choice([
            f"LFM {content}{time_text} {need}{loot}",
            f"lfm {content}{time_text} {need}{loot}",
            f"LFM {content} {need}{loot}",
            f"{content}{time_text} {need}{loot}",
            f"forming {content}{time_text} {need}{loot}",
        ])

        message = " ".join(message.split())

        if random.randint(1, 100) <= 55:
            return _cache_city_ad(
                _city_lfg_ad_personas,
                bot,
                message,
            )

        return message

    role_need = random.choice([
        "need tank",
        "need heals",
        "need tank heals",
        "LF tank",
        "LF heals",
        "LF1M tank",
        "LF1M heals",
        "need 1",
        "need 2 dps",
    ])

    roll = random.randint(1, 100)

    if roll <= 55:
        return random.choice([
            f"LFM {content} {role_need}",
            f"lfm {content} {role_need}",
            f"{content} {role_need}",
        ])

    if roll <= 78:
        return random.choice([
            f"LFG {content}",
            f"lfg {content}",
            f"LF {content}",
            f"lf {content}",
        ])

    if roll <= 90:
        return random.choice([
            f"{content} anyone",
            f"{content} anyone?",
            f"any {content}",
        ])

    return random.choice([
        f"forming {content}",
        f"{content} go?",
        f"LFM {content} pst",
    ])


def _build_city_portal_request(bot):
    """Generate a short player-style portal request."""
    alliance = _is_alliance_bot(bot)

    if alliance:
        destinations = [
            'IF',
            'IF',
            'Darn',
            'Exodar',
            'Theramore',
            'Shatt',
            'Shatt',
            'Dala',
            'Dala',
            'Dalaran',
        ]
    else:
        destinations = [
            'Org',
            'Org',
            'UC',
            'TB',
            'Silvermoon',
            'Stonard',
            'Shatt',
            'Shatt',
            'Dala',
            'Dala',
            'Dalaran',
        ]

    destination = random.choice(destinations)

    # Real portal requests frequently include a small offered tip,
    # but plenty do not specify one.
    tip = random.choice([
        '',
        '',
        '',
        '',
        ' 5g',
        ' 10g',
        ' tip',
        ' tipping',
    ])

    ending = random.choice([
        '',
        '',
        '',
        ' pst',
        ' PST',
        ' pls',
        ' plz',
    ])

    return random.choice([
        f"lf port {destination}{tip}{ending}",
        f"LF port {destination}{tip}{ending}",
        f"wtb port {destination}{tip}{ending}",
        f"WTB port {destination}{tip}{ending}",
        f"need port {destination}{tip}{ending}",
        f"port {destination}?{ending}",
        f"mage port {destination}?{ending}",
        f"lf mage {destination}{tip}{ending}",
    ])


def _build_city_portal(bot):
    bot_class = str(
        bot.get('class', '')
    ).strip().lower()

    if bot_class != 'mage':
        return _build_city_portal_request(bot)

    if random.randint(1, 100) <= 50:
        return _build_city_portal_request(bot)

    alliance = _is_alliance_bot(bot)

    if alliance:
        destinations = [
            'IF',
            'Darn',
            'Exodar',
            'Theramore',
            'Shatt',
            'Dala',
        ]
    else:
        destinations = [
            'Org',
            'UC',
            'TB',
            'Silvermoon',
            'Stonard',
            'Shatt',
            'Dala',
        ]

    destination = random.choice(destinations)

    return random.choice([
        "WTS ports pst",
        "wts ports",
        "ports pst",
        "ports up",
        "WTS Dala/Shatt ports",
        "wts dala/shatt",
        f"WTS {destination} port",
        f"wts port {destination}",
        f"{destination} ports pst",
        f"port {destination} pst",
    ])


def _build_city_trade(config, bot):
    """
    Build ambient direct-trade chatter around a dynamically
    selected valuable/rare database item.
    """
    item = _city_template_item(config)

    if not item:
        return "WTS misc stuff pst"

    link = item['link']

    try:
        item_class = int(
            item.get('item_class') or 0
        )
    except (TypeError, ValueError):
        item_class = 0

    # Equipment/recipes normally advertise singular copies.
    if item_class in (2, 4, 9, 15):
        amount = random.choice([
            '',
            '',
            '',
            ' x1',
        ])
    else:
        amount = random.choice([
            '',
            '',
            ' x2',
            ' x5',
            ' x10',
            ' x20',
        ])

    ending = random.choice([
        '',
        '',
        ' pst',
        ' pst',
        ' PST',
        ' /w me',
    ])

    # Actual Blizzard Trade contains plenty of sellers.
    selling = (
        random.randint(1, 100) <= 58
    )

    if selling:
        verb = random.choice([
            'WTS',
            'WTS',
            'WTS',
            'wts',
            'wts',
        ])

        return random.choice([
            f"{verb} {link}{amount}{ending}",
            f"{verb} {link}{amount} pst",
            f"{link}{amount} for sale{ending}",
            f"wts {link}{amount} offer{ending}",
        ])

    verb = random.choice([
        'WTB',
        'WTB',
        'wtb',
        'wtb',
        'Wtb',
    ])

    return random.choice([
        f"{verb} {link}{amount}{ending}",
        f"{verb} {link}{amount} pst",
        f"buying {link}{amount}{ending}",
        f"lf {link}{amount}{ending}",
    ])


def _profession_service_link(row):
    try:
        entry = int(
            row.get('item_entry') or 0
        )
        quality = int(
            row.get('item_quality') or 1
        )
    except (TypeError, ValueError):
        return None

    name = str(
        row.get('item_name') or ''
    ).strip()

    if not entry or not name:
        return None

    return format_item_link(
        entry,
        quality,
        name,
    )


def _clean_profession_display_name(
    profession,
    name,
):
    name = str(name or '').strip()

    if profession == 'enchanting':
        prefix = 'Scroll of '
        if name.startswith(prefix):
            return name[len(prefix):]

    return name


def _choose_profession_services(
    rows,
    count=1,
):
    """
    Choose profession services with a strong bias toward
    current/high-skill crafts while retaining some mid-tier
    and legacy variety.

    Approximate distribution:
      70% high/current
      20% mid-tier
      10% legacy
    """
    if not rows:
        return []

    ranked = []

    for row in rows:
        try:
            rank = int(
                row.get('service_rank') or 0
            )
        except (TypeError, ValueError):
            rank = 0

        ranked.append(
            (row, rank)
        )

    max_rank = max(
        rank
        for _, rank in ranked
    )

    # Pools without meaningful skill-rank metadata, such as
    # the current glyph pool, remain normally randomized.
    if max_rank <= 0:
        amount = min(
            int(count),
            len(rows),
        )
        return random.sample(
            rows,
            amount,
        )

    high_cutoff = max_rank * 0.70
    mid_cutoff = max_rank * 0.35

    high = [
        row
        for row, rank in ranked
        if rank >= high_cutoff
    ]

    mid = [
        row
        for row, rank in ranked
        if (
            rank >= mid_cutoff
            and rank < high_cutoff
        )
    ]

    legacy = [
        row
        for row, rank in ranked
        if rank < mid_cutoff
    ]

    roll = random.randint(
        1,
        100,
    )

    if roll <= 85:
        primary = high
        secondary = mid
        tertiary = legacy
    elif roll <= 97:
        primary = mid
        secondary = high
        tertiary = legacy
    else:
        primary = legacy
        secondary = mid
        tertiary = high

    candidates = (
        list(primary)
        + list(secondary)
        + list(tertiary)
    )

    # Preserve order of preference while removing duplicates.
    unique = []
    seen = set()

    for row in candidates:
        try:
            entry = int(
                row.get('item_entry') or 0
            )
        except (TypeError, ValueError):
            entry = 0

        key = (
            entry
            if entry
            else id(row)
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(row)

    amount = min(
        int(count),
        len(unique),
    )

    # Pick primarily from the selected skill band so one seller
    # tends to advertise a coherent group of similarly relevant
    # crafts rather than a completely random recipe-history mix.
    preferred = list(primary)

    if preferred:
        amount = min(
            amount,
            len(preferred),
        )
        return random.sample(
            preferred,
            amount,
        )

    chosen = []
    chosen_ids = {
        int(
            row.get('item_entry') or 0
        )
        for row in chosen
    }

    remainder = [
        row
        for row in unique
        if int(
            row.get('item_entry') or 0
        ) not in chosen_ids
    ]

    need = amount - len(chosen)

    if need > 0:
        chosen.extend(
            random.sample(
                remainder,
                min(
                    need,
                    len(remainder),
                ),
            )
        )

    return chosen


def _choose_city_profession(
    available,
):
    """
    Weight profession chatter toward the services players most
    commonly advertise/request in Trade.

    Enchanting and Jewelcrafting are intentionally prominent.
    """
    weights = {
        'enchanting': 24,
        'jewelcrafting': 19,
        'tailoring': 13,
        'blacksmithing': 12,
        'alchemy': 12,
        'leatherworking': 8,
        'engineering': 7,
        'inscription': 5,
    }

    choices = [
        profession
        for profession in available
        if profession in weights
    ]

    if not choices:
        return random.choice(
            available
        )

    return random.choices(
        choices,
        weights=[
            weights[profession]
            for profession in choices
        ],
        k=1,
    )[0]


def _build_city_profession(config, bot):
    """
    Build Blizzard-style profession Trade chatter.

    Most messages are persistent seller advertisements with
    several real clickable service/output links. Buyer requests
    usually ask for one specific linked service.
    """
    pools = query_profession_services(
        config
    )

    available = [
        profession
        for profession, rows
        in pools.items()
        if rows
        and profession != 'engineering'
    ]

    if not available:
        return random.choice([
            "LF enchanter pst",
            "LF JC pst",
            "LF tailor pst",
            "LF BS pst",
        ])

    # --------------------------------------------------------
    # SELLER / LFW ADVERTISEMENT: 65%
    # --------------------------------------------------------
    if random.randint(1, 100) <= 65:
        cached = _get_cached_city_ad(
            _city_profession_ad_personas,
            bot,
        )

        if cached:
            return cached

        profession = _choose_city_profession(
            available
        )

        rows = pools[profession]

        count = min(
            len(rows),
            random.choice([
                2, 2, 3, 3, 3, 4
            ]),
        )

        chosen = _choose_profession_services(
            rows,
            count,
        )

        links = []

        for row in chosen:
            link = _profession_service_link(
                row
            )

            if link:
                links.append(link)

        if not links:
            return "LFW professions pst"

        labels = {
            'blacksmithing': [
                'BS LFW',
                'Blacksmith LFW',
                'BS crafting',
            ],
            'leatherworking': [
                'LW LFW',
                'Leatherworker LFW',
                'LW crafting',
            ],
            'alchemy': [
                'Alch LFW',
                'Alchemist LFW',
                'Alch crafting',
            ],
            'tailoring': [
                'Tailor LFW',
                'Tailoring LFW',
                'Tailor crafting',
            ],
            'engineering': [
                'Engi LFW',
                'Engineer LFW',
                'ENGI LFW',
            ],
            'enchanting': [
                'Ench LFW',
                'Enchanter LFW',
                'ENCH LFW',
            ],
            'jewelcrafting': [
                'JC LFW',
                'Jewelcrafter LFW',
                'JC cuts',
            ],
            'inscription': [
                'Scribe LFW',
                'Inscription LFW',
                'Glyphs LFW',
            ],
        }

        prefix = random.choice(
            labels[profession]
        )

        ending = random.choice([
            ' pst',
            ' PST',
            ' your mats',
            ' pst!',
            ' tips appreciated',
            '',
        ])

        message = (
            f"{prefix} "
            + " ".join(links)
            + ending
        )

        return _cache_city_ad(
            _city_profession_ad_personas,
            bot,
            message,
        )

    # --------------------------------------------------------
    # BUYER / SERVICE REQUEST: 35%
    # --------------------------------------------------------
    profession = _choose_city_profession(
        available
    )

    chosen = _choose_profession_services(
        pools[profession],
        1,
    )

    if not chosen:
        return "LF crafter pst"

    row = chosen[0]

    link = _profession_service_link(
        row
    )

    if not link:
        return "LF crafter pst"

    request_prefix = {
        'blacksmithing': [
            'LF BS for',
            'need BS for',
            'LF blacksmith',
        ],
        'leatherworking': [
            'LF LW for',
            'need LW for',
            'LF leatherworker',
        ],
        'alchemy': [
            'LF alch for',
            'need alch for',
            'LF alchemist',
        ],
        'tailoring': [
            'LF tailor for',
            'need tailor for',
            'LF tailor',
        ],
        'engineering': [
            'LF engi for',
            'need engineer for',
            'LF engi',
        ],
        'enchanting': [
            'LF ench',
            'need enchanter',
            'LF enchanter',
        ],
        'jewelcrafting': [
            'LF JC',
            'need JC',
            'LF gem cutter',
        ],
        'inscription': [
            'LF scribe for',
            'need scribe for',
            'LF inscription',
        ],
    }

    prefix = random.choice(
        request_prefix[profession]
    )

    ending = random.choice([
        ' have mats tip',
        ' have mats',
        ' tipping',
        ' pst',
        ' PST',
        '',
    ])

    return (
        f"{prefix} {link}{ending}"
    )


def _attach_city_guild_state(request, bots):
    """
    Attach authoritative guild state from C++ bot_states_json.

    The city template system must never infer or invent guild
    membership. Missing/malformed state means not recruitable.
    """
    if not bots:
        return

    raw = request.get('bot_states_json')

    if not raw:
        return

    try:
        if isinstance(raw, str):
            states = json.loads(raw)
        elif isinstance(raw, dict):
            states = raw
        else:
            return
    except (TypeError, ValueError, json.JSONDecodeError):
        return

    if not isinstance(states, dict):
        return

    for bot in bots:
        guid = str(bot.get('guid') or '')
        if not guid:
            continue

        state_wrapper = states.get(guid)

        if not isinstance(state_wrapper, dict):
            continue

        # BuildBotStateJson() is wrapped as:
        #
        # {
        #   "bot_state": {
        #       ...
        #       "guild": {...}
        #   }
        # }
        #
        bot_state = state_wrapper.get('bot_state')

        if not isinstance(bot_state, dict):
            continue

        guild = bot_state.get('guild')

        if not isinstance(guild, dict):
            continue

        # Copy only the authoritative guild object.
        bot['guild'] = guild


def _get_recruitable_city_guild(bot):
    """
    Return the authoritative guild object only when this bot
    may truthfully advertise that guild.
    """
    guild = bot.get('guild')

    if not isinstance(guild, dict):
        return None

    try:
        guild_id = int(guild.get('id') or 0)
    except (TypeError, ValueError):
        guild_id = 0

    guild_name = str(
        guild.get('name') or ''
    ).strip()

    has_space = guild.get('has_space') is True

    if (
        guild_id <= 0
        or not guild_name
        or not has_space
    ):
        return None

    return guild


def _generate_ambient_guild_name():
    first = random.choice([
        'Ashen',
        'Crimson',
        'Silver',
        'Iron',
        'Fallen',
        'Last',
        'Dark',
        'Northern',
        'Sacred',
        'Midnight',
        'Eternal',
        'Golden',
        'Broken',
        'Royal',
        'Silent',
        'Rising',
        'Ancient',
        'Lost',
        'Burning',
        'Frozen',
    ])

    second = random.choice([
        'Dawn',
        'Order',
        'Guard',
        'Legion',
        'Vanguard',
        'Oath',
        'Council',
        'Company',
        'Requiem',
        'Legacy',
        'Knights',
        'Watch',
        'Fury',
        'Revenant',
        'Empire',
        'Circle',
        'Hand',
        'Wolves',
        'Heroes',
        'Misfits',
    ])

    return f"{first} {second}"


def _build_ambient_guild_ad(bot):
    guild_name = _generate_ambient_guild_name()

    archetype = random.randint(1, 100)

    # -----------------------------------------------------
    # RAID / PROGRESSION GUILD: 65%
    # -----------------------------------------------------
    if archetype <= 65:
        raid = random.choice([
            'ICC10',
            'ICC10',
            'ICC25',
            'ICC25',
            'ToC25',
            'ToGC25',
            'Uld25',
            'Uld10',
            'Naxx25',
        ])

        schedule = random.choice([
            'Tue/Thu 8-11 ST',
            'Tues/Thurs 8pm ST',
            'Wed/Sun 8-11 ST',
            'Wed/Sun 7:30-10:30 ST',
            'Fri/Sat 8pm ST',
            'Sat/Sun 7-10 ST',
            'Sun/Mon 8pm ST',
            'Tue/Wed 8:30 ST',
        ])

        identity = random.choice([
            'Semi-HC',
            'semi-hc',
            'casual',
            'casual progression',
            'progression',
            'social raid guild',
            'laid back',
            'core raid team',
        ])

        needs = random.choice([
            'LF hpal/resto sham + ranged',
            'LF heals + ranged dps',
            'need holy pally + lock',
            'LF resto sham/mage/hunter',
            'need tank + 2 heals',
            'LF disc priest + ranged',
            'LF boomie/ele/lock',
            'need hunter/lock/mage',
            'LF reliable heals + dps',
            'LF enh sham/hpal/lock',
            'LF tank/heals',
            'all strong players considered',
        ])

        extra = random.choice([
            '',
            '',
            '',
            ' 2SR',
            ' MS>OS',
            ' loot council',
            ' 2SR MS>OS',
            ' bench welcome',
        ])

        return random.choice([
            f"<{guild_name}> {identity} {raid} | {schedule} | {needs}{extra}",
            f"<{guild_name}> recruiting for {raid} {schedule} {needs}{extra}",
            f"<{guild_name}> {raid} team recruiting | {schedule} | {needs}{extra}",
            f"<{guild_name}> {identity}, raids {schedule}, {needs}{extra}",
            f"<{guild_name}> recruiting core raiders {schedule} {needs}{extra}",
            f"<{guild_name}> {raid} progression {schedule} {needs}{extra}",
        ])

    # -----------------------------------------------------
    # CASUAL / SOCIAL / LEVELING: 22%
    # -----------------------------------------------------
    if archetype <= 87:
        focus = random.choice([
            'social/casual',
            'leveling + heroics',
            'casual PvE',
            'social PvE',
            'alts + heroics',
            'dungeons/raids',
            'new players + vets',
        ])

        detail = random.choice([
            'all lvls welcome',
            'active nightly',
            'weekend raids',
            'helpful members',
            'discord active',
            'no attendance req',
            'all classes welcome',
        ])

        return random.choice([
            f"<{guild_name}> {focus} guild recruiting, {detail} pst",
            f"<{guild_name}> recruiting | {focus} | {detail}",
            f"<{guild_name}> casual guild recruiting, {detail}",
            f"<{guild_name}> LF more, {focus}, {detail}",
        ])

    # -----------------------------------------------------
    # PVP: 13%
    # -----------------------------------------------------
    need = random.choice([
        'LF healers + ranged',
        'LF active PvPers',
        'LF arena/BG players',
        'all classes considered',
        'LF heals',
    ])

    return random.choice([
        f"<{guild_name}> PvP guild recruiting {need}",
        f"<{guild_name}> BG/arena guild LF more, {need}",
        f"<{guild_name}> PvP recruiting, {need} pst",
    ])


def _build_city_guild(bot):
    """
    Build atmospheric guild-channel chatter.

    Ambient guild advertisements are deliberately decoupled from
    the speaker's actual guild membership and level. Public chat
    is non-transactional atmosphere; consistency is provided by
    persistent advertiser personas instead.
    """
    # Approximately 80% recruitment ads, 20% guild seekers.
    if random.randint(1, 100) <= 80:
        cached = _get_cached_city_ad(
            _city_guild_ad_personas,
            bot,
        )

        if cached:
            return cached

        return _cache_city_ad(
            _city_guild_ad_personas,
            bot,
            _build_ambient_guild_ad(bot),
        )

    try:
        level = int(bot.get('level') or 1)
    except (TypeError, ValueError):
        level = 1

    bot_class = str(
        bot.get('class', 'player')
    ).strip().lower()

    # Seeking is about the speaker personally, so their real
    # level/class remain useful flavor here.
    if level >= 70:
        return random.choice([
            f"{level} {bot_class} lf raid guild",
            f"{bot_class} lf weekend raid guild",
            f"{level} {bot_class} lf active guild",
            "lf casual raid guild",
            "LF guild doing ICC10",
            "lf late night guild",
            "lf 10m raid guild",
            "lf social/pve guild",
        ])

    return random.choice([
        f"lvl {level} {bot_class} lf guild",
        f"{bot_class} lf leveling guild",
        f"lvl {level} lf active guild",
        "lf leveling guild",
        "lf social guild",
        "any active guild recruiting?",
    ])


def _format_ah_price(copper):
    """Format copper as natural player-style AH price text."""
    try:
        copper = max(0, int(copper))
    except (TypeError, ValueError):
        return "?"

    gold = copper // 10000
    silver = (copper % 10000) // 100

    # Avoid fake precision. Players usually round AH prices.
    if gold >= 100:
        rounded = int(round(gold / 5.0) * 5)
        return f"around {rounded}g"

    if gold >= 10:
        return f"around {gold}g"

    if gold >= 1:
        if silver >= 50:
            return f"around {gold + 1}g"
        return f"around {gold}g"

    if silver > 0:
        return f"around {silver}s"

    return "under 1s"


def _build_city_ah(config, bot):
    """
    Build a grounded AH opener from the live auction market.

    Returns:
        (message, market_data)
    """
    market_rows = query_live_auction_market(
        config,
        limit=40,
    )

    if not market_rows:
        # Safe fallback: generic AH observation with no fake price.
        return random.choice([
            "AH kinda dead right now",
            "not much good on AH",
            "AH looking weird today",
            "anyone else checking AH?",
        ]), None

    market = random.choice(market_rows)

    name = str(market.get('name') or '').strip()
    item_entry = int(market.get('item_entry') or 0)
    item_quality = int(market.get('item_quality') or 1)
    listings = int(market.get('listings') or 0)
    lowest = int(market.get('lowest_each') or 0)

    if not name or not item_entry:
        return "anyone checking AH?", None

    item_link = format_item_link(
        item_entry,
        item_quality,
        name,
    )

    opener_type = random.choice([
        'price_check',
        'availability',
        'general_check',
    ])

    if opener_type == 'price_check':
        message = random.choice([
            f"pc {item_link}?",
            f"price check {item_link}?",
            f"what's {item_link} going for?",
            f"anyone know price on {item_link}?",
        ])

    elif opener_type == 'availability':
        message = random.choice([
            f"much {item_link} on AH?",
            f"any {item_link} up?",
            f"is there much {item_link} listed?",
        ])

    else:
        message = random.choice([
            f"any market for {item_link}?",
            f"is {item_link} selling?",
            f"anyone watching {item_link} prices?",
        ])

    market['_opener_type'] = opener_type
    market['_formatted_lowest'] = _format_ah_price(lowest)
    market['_listings_int'] = listings

    return message, market


def _build_city_social(bots):
    """
    Build occasional zero-token public city General chatter.

    General should feel like a quiet public city channel:
    questions, navigation, game-system questions, and occasional
    server observations.

    Do not manufacture replies, private-status chatter, or random
    reaction lines with no conversation to react to.
    """
    if not bots:
        return []

    speaker = bots[0]

    try:
        level = int(
            speaker.get('level') or 1
        )
    except (AttributeError, TypeError, ValueError):
        level = 1

    # --------------------------------------------------------
    # Universal city/public questions.
    # --------------------------------------------------------
    lines = [
        "lag for anyone else?",
        "anyone else lagging?",
        "server lagging?",
        "anyone else getting lag?",
        "anyone getting dc'd?",
        "ah laggy for anyone else?",

        "wheres ah",
        "where is ah?",
        "where bank",
        "wheres the bank",
        "where fm",
        "where flight master",
        "where is the inn?",
        "where barber",
        "anyone know where the barber is?",
        "where do i train riding?",
        "where is riding trainer?",
        "where weapon trainer?",

        "how much is dual spec?",
        "what lvl is dual spec?",
        "can you queue bg from anywhere?",
        "where do you turn in marks?",
        "how do i link an item?",
        "how do i leave a channel?",
        "can you reset talents here?",
        "where do i buy reagents?",
        "any repair nearby?",
        "where is mailbox?",
        "wheres mailbox",
    ]

    # --------------------------------------------------------
    # Higher-level Wrath questions. Keep these out of low-level
    # speakers so a level 18 character doesn't constantly sound
    # like an endgame veteran.
    # --------------------------------------------------------
    if level >= 68:
        lines.extend([
            "where do i learn cold weather flying?",
            "cold weather flying trainer where?",
            "how much is cold weather flying?",
            "what time does wg start?",
            "when is next wg?",
            "who has wg?",
            "is wg up?",
            "where do you buy heirlooms?",
            "where is badge vendor?",
            "where do i spend emblems?",
            "can you queue heroic right at 80?",
            "what ilvl for heroics?",
            "is dalaran laggy for anyone else?",
        ])

    # --------------------------------------------------------
    # Lower/mid-level questions.
    # --------------------------------------------------------
    if level < 68:
        lines.extend([
            "what lvl can you get epic riding?",
            "where do i learn first aid?",
            "anyone know where cooking trainer is?",
            "where do i train professions?",
            "when do mounts get faster?",
            "what lvl can you fly?",
            "where do i change talents?",
        ])

    # Avoid obvious bot repetition across different speakers.
    available = [
        line
        for line in lines
        if line not in _city_social_recent_lines
    ]

    if not available:
        available = lines

    message = random.choice(
        available
    )

    _city_social_recent_lines.append(
        message
    )

    # Remember enough history that short utility lines do not
    # noticeably repeat during the same city-chat sample.
    del _city_social_recent_lines[:-14]

    return [
        (speaker, message),
    ]



def _build_city_boost(bot):
    """
    Build persistent Trade-channel dungeon boost advertisements.

    Boost ads intentionally reuse exact copy for a while. Real
    service advertisers commonly repeat the same advertisement
    rather than generating fresh wording every message.
    """
    cached = _get_cached_city_ad(
        _city_boost_ad_personas,
        bot,
    )

    if cached:
        return cached

    service = random.choice([
        {
            'name': 'UK',
            'level': '70-77',
            'detail': [
                'fast runs',
                '10-12 min runs',
                'big pulls',
                '3 spots',
                '4 spots',
            ],
        },
        {
            'name': 'Nexus',
            'level': '70-77',
            'detail': [
                'fast runs',
                '10-12 min',
                '3 spots',
                '4 spots',
                'xp runs',
            ],
        },
        {
            'name': 'DTK',
            'level': '73-79',
            'detail': [
                'fast runs',
                'xp runs',
                '3 spots',
                '4 spots',
                'quick resets',
            ],
        },
        {
            'name': 'Gundrak',
            'level': '75-79',
            'detail': [
                'fast runs',
                'xp runs',
                '3 spots',
                '4 spots',
                'quick runs',
            ],
        },
        {
            'name': 'HoS',
            'level': '75-79',
            'detail': [
                'fast runs',
                'xp runs',
                '3 spots',
                '4 spots',
                '10-12 min runs',
            ],
        },
        {
            'name': 'HoL',
            'level': '77-79',
            'detail': [
                'fast runs',
                'xp runs',
                '3 spots',
                '4 spots',
                'quick runs',
            ],
        },
    ])

    dungeon = service['name']
    level_range = service['level']
    detail = random.choice(
        service['detail']
    )

    price = random.choice([
        '',
        '',
        '',
        ' 20g/run',
        ' 25g/run',
        ' 30g/run',
    ])

    message = random.choice([
        f"WTS {dungeon} BOOST {level_range} | {detail}{price} | pst",
        f"WTS {dungeon} boost {level_range} {detail}{price} pst",
        f"{dungeon} BOOST {level_range} | {detail} | pst inv",
        f"WTS {dungeon} XP RUNS | {level_range} | {detail}{price} pst",
        f"selling {dungeon} boost {level_range} {detail}{price} pst",
    ])

    return _cache_city_ad(
        _city_boost_ad_personas,
        bot,
        message,
    )

def _build_city_template_exchange(config, bots):
    """
    Build zero-token capital-city public chatter.

    Distribution intentionally reflects the messy mixed-use
    character of real Blizzard city channels rather than strict
    category purity.
    """
    if not bots:
        return 'plain', []

    speaker = bots[0]
    roll = random.randint(1, 100)

    # PUBLIC / GENERAL: 10%
    if roll <= 10:
        return 'social', _build_city_social(bots)

    # LFG / LFM: 23%
    if roll <= 33:
        return 'lfg', [
            (speaker, _build_city_lfg(speaker)),
        ]

    # DIRECT ITEM TRADE: 15%
    if roll <= 48:
        return 'trade', [
            (
                speaker,
                _build_city_trade(
                    config,
                    speaker,
                ),
            ),
        ]

    # DUNGEON BOOSTING / SERVICES: 10%
    if roll <= 58:
        return 'boost', [
            (
                speaker,
                _build_city_boost(speaker),
            ),
        ]

    # PORTALS: 9%
    if roll <= 67:
        return 'portal', [
            (
                speaker,
                _build_city_portal_request(
                    speaker
                ),
            ),
        ]

    # PROFESSIONS: 18%
    if roll <= 85:
        return 'profession', [
            (
                speaker,
                _build_city_profession(
                    config,
                    speaker
                ),
            ),
        ]

    # GUILDS: 15%
    return 'guild', [
        (
            speaker,
            _build_city_guild(speaker),
        ),
    ]


def _maybe_emit_city_template_conversation(
    db,
    config,
    request,
    bots,
    zone_id,
):
    """Emit a multi-bot city exchange without calling the LLM."""
    if not bots:
        return False

    try:
        chance = int(config.get(
            'LLMChatter.CapitalTemplateChance',
            '100',
        ))
    except (TypeError, ValueError):
        chance = 100

    chance = max(0, min(100, chance))

    if random.randint(1, 100) > chance:
        return False

    _attach_city_guild_state(
        request,
        bots,
    )

    category, messages = (
        _build_city_template_exchange(
            config,
            bots,
        )
    )

    # City conversations may overlap other General traffic.
    # A populated capital should scroll rather than serialize
    # every conversation behind the previous one.
    base_delay = min(
        _zone_delivery_delay(
            zone_id,
            config,
        ),
        random.uniform(0.5, 2.0),
    )

    try:
        max_queue_delay = float(config.get(
            'LLMChatter.CapitalTemplateMaxQueueDelay',
            '35',
        ))
    except (TypeError, ValueError):
        max_queue_delay = 35.0

    if base_delay > max_queue_delay:
        logger.info(
            "[GEN-FLOW] city template conv dropped | "
            "backlog=%.1fs max=%.1fs",
            base_delay,
            max_queue_delay,
        )
        return True

    cumulative_delay = base_delay
    previous_length = 0

    channel_by_category = {
        'social': 'general',
        'lfg': 'general',
        'trade': 'trade',
        'boost': 'trade',
        'portal': 'trade',
        'profession': 'trade',
        'guild': 'guild_recruitment',
    }

    delivery_channel = channel_by_category.get(
        category,
        'general',
    )

    for sequence, (bot, message) in enumerate(messages):
        if sequence > 0:
            delay = random.uniform(2.0, 5.0)
            cumulative_delay += delay

        previous_length = len(message)

        logger.info(
            "[GEN-FLOW] city template conv | "
            "type=%s channel=%s bot=%s delay=%.1fs seq=%d/%d",
            category,
            delivery_channel,
            bot['name'],
            cumulative_delay,
            sequence,
            len(messages),
        )

        insert_chat_message(
            db,
            bot['guid'],
            bot['name'],
            message,
            channel=delivery_channel,
            delay_seconds=cumulative_delay,
            queue_id=request['id'],
            sequence=sequence,
        )

    # Do NOT reserve all General chat until this conversation
    # finishes. Other city speakers may naturally overlap it.
    # Keep only a very small spacing reservation.
    _zone_last_delivery[zone_id] = (
        time.monotonic() + random.uniform(1.0, 3.0)
    )

    # Deliberately do NOT call the LLM reaction system.
    db.commit()

    return True


def _maybe_emit_city_template(
    db,
    config,
    request,
    bot,
    zone_id,
):
    """
    Emit a single-bot zero-token capital template.

    Reuse the authoritative city-template conversation path with
    a one-bot list so statement and conversation requests share
    the same chance, grounding, timing, and delivery behavior.
    """
    if not bot:
        return False

    return _maybe_emit_city_template_conversation(
        db,
        config,
        request,
        [bot],
        zone_id,
    )


def _select_gossip_or_existing_type(
    config,
    conversation=False,
    is_capital=False,
):
    """Select ambient type with a separate capital-city mix."""
    npc_chance = max(0, int(config.get(
        'LLMChatter.AmbientNpcGossipChance', 5
    )))
    bot_chance = max(0, int(config.get(
        'LLMChatter.AmbientBotGossipChance', 5
    )))

    roll = random.randint(1, 100)
    if roll <= npc_chance:
        return 'npc'
    if roll <= npc_chance + bot_chance:
        return 'bot'

    if not conversation:
        return select_message_type()

    roll = random.randint(1, 100)

    # Capitals should sound like population centers:
    # trade, services, grouping, guilds, portals and AH chatter.
    if is_capital:
        if roll <= 50:
            return 'plain'
        if roll <= 75:
            return 'trade'
        if roll <= 85:
            return 'quest'
        if roll <= 92:
            return 'loot'
        return 'spell'

    # Existing outdoor-zone mix remains unchanged.
    if roll <= 45:
        return 'plain'
    if roll <= 65:
        return 'quest'
    if roll <= 80:
        return 'loot'
    if roll <= 90:
        return 'trade'
    return 'spell'


def _get_gossip_target_cooldown(config):
    """Return gossip target cooldown in seconds."""
    try:
        return max(0, int(config.get(
            'LLMChatter.AmbientGossipTargetCooldownSeconds',
            1800,
        )))
    except (TypeError, ValueError):
        return 1800


def _gossip_target_key(target_type, zone_id, target):
    """Build the recent-subject key for an NPC or bot."""
    if target_type == "bot":
        target_id = target.get('guid') or target.get('name', '')
    else:
        target_id = target.get('entry') or target.get('name', '')
    return (target_type, int(zone_id or 0), target_id)


def _filter_recent_gossip_targets(
    targets, target_type, zone_id, cooldown
):
    """Remove recently used gossip subjects."""
    if cooldown <= 0:
        return targets

    now = time.time()
    expired = [
        key for key, expires_at
        in _gossip_target_cooldowns.items()
        if expires_at <= now
    ]
    for key in expired:
        _gossip_target_cooldowns.pop(key, None)

    return [
        target for target in targets
        if _gossip_target_cooldowns.get(
            _gossip_target_key(target_type, zone_id, target),
            0,
        ) <= now
    ]


def _mark_gossip_target_seen(
    target, target_type, zone_id, cooldown
):
    """Mark a gossip subject as recently used."""
    if cooldown <= 0 or not target:
        return
    _gossip_target_cooldowns[
        _gossip_target_key(target_type, zone_id, target)
    ] = time.time() + cooldown


def _pick_npc_gossip_target(config, zone_id):
    """Pick a random NPC gossip subject for this zone."""
    targets = query_zone_npcs(config, zone_id)
    cooldown = _get_gossip_target_cooldown(config)
    targets = _filter_recent_gossip_targets(
        targets, "npc", zone_id, cooldown
    )
    target = random.choice(targets) if targets else None
    _mark_gossip_target_seen(
        target, "npc", zone_id, cooldown
    )
    return target


def _pick_bot_gossip_target(config, cursor, zone_id, speaker_guids):
    """Pick a random bot gossip subject for this zone."""
    targets = query_zone_bot_gossip_targets(
        cursor, zone_id, exclude_guids=speaker_guids
    )
    cooldown = _get_gossip_target_cooldown(config)
    targets = _filter_recent_gossip_targets(
        targets, "bot", zone_id, cooldown
    )
    target = random.choice(targets) if targets else None
    _mark_gossip_target_seen(
        target, "bot", zone_id, cooldown
    )
    return target

def _get_bot_active_quests(bot):
    """Return authoritative active quests from bot_state."""
    state = bot.get('bot_state')

    if not isinstance(state, dict):
        return []

    quests = state.get('quests')
    if not isinstance(quests, list):
        return []

    active = []

    for quest in quests:
        if not isinstance(quest, dict):
            continue

        if quest.get('rewarded'):
            continue

        active.append(quest)

    return active


def _quest_identity(quest):
    """Return best-effort stable id/title identity for a quest."""
    if not isinstance(quest, dict):
        return None, ''

    quest_id = None

    for key in (
        'quest_id',
        'id',
        'entry',
        'questId',
        'questID',
    ):
        value = quest.get(key)

        try:
            value = int(value)
        except (TypeError, ValueError):
            continue

        if value > 0:
            quest_id = value
            break

    title = ''

    for key in (
        'title',
        'name',
        'quest_name',
        'questTitle',
    ):
        value = str(quest.get(key) or '').strip()

        if value:
            title = value.casefold()
            break

    return quest_id, title


def _get_bot_active_zone_quests(bot, config, zone_id):
    """
    Return only real active quests that are relevant to the
    bot's current authoritative zone.

    A quest must exist both in live bot_state and in the
    zone-query result. This prevents a bot in Westfall from
    discussing an active Redridge quest in General.
    """
    active = _get_bot_active_quests(bot)

    if not active:
        return []

    try:
        level = int(bot.get('level') or 1)
    except (TypeError, ValueError):
        level = 1

    zone_quests = query_zone_quests(
        config,
        zone_id,
        level,
    )

    if not zone_quests:
        return []

    zone_ids = set()
    zone_titles = set()

    for quest in zone_quests:
        quest_id, title = _quest_identity(quest)

        if quest_id:
            zone_ids.add(quest_id)

        if title:
            zone_titles.add(title)

    relevant = []

    for quest in active:
        quest_id, title = _quest_identity(quest)

        if quest_id and quest_id in zone_ids:
            relevant.append(quest)
            continue

        if title and title in zone_titles:
            relevant.append(quest)

    return relevant


def process_statement(
    db, cursor, client, config, request, bot: dict
):
    """Process a single statement request."""
    channel = 'general'

    # Select message type
    zone_id = request.get('zone_id', 0)
    area_id = request.get('area_id', zone_id)
    current_weather = request.get('weather') or None
    mode = get_chatter_mode(config)

    # Zone metadata for request logging
    zone_meta = _build_zone_metadata(
        zone_id, area_id
    )
    msg_type = _select_gossip_or_existing_type(config)

    # Skip loot/trade in capital cities (no zone
    # creatures to reference, causes empty queries)
    if (
        msg_type in ("loot", "trade")
        and zone_id in CAPITAL_CITY_ZONES
    ):
        msg_type = "plain"


    # Get zone data if needed
    quest_data = None
    item_data = None
    item_can_use = False
    spell_data = None
    gossip_target = None
    gossip_target_type = None

    if msg_type == "quest":
        quests = _get_bot_active_zone_quests(
            bot,
            config,
            zone_id,
        )
        if quests:
            quest_data = random.choice(quests)
        else:
            msg_type = "plain"  # Fallback

    if msg_type == "quest_reward":
        # Reward chatter still uses static quest-template
        # data for now because this path expects reward
        # item fields that are not guaranteed in bot_state.
        quests = query_zone_quests(
            config, zone_id, bot['level']
        )
        if quests:
            quest_data = random.choice(quests)
        else:
            msg_type = "plain"  # Fallback

    if msg_type == "loot":
        item_data = _fetch_loot_data(
            config, zone_id, bot['level']
        )
        if item_data:
            # Check if bot's class can use the item
            item_can_use = can_class_use_item(
                bot['class'],
                item_data.get('allowable_class', -1)
            )
            quality_names = {
                0: "gray", 1: "white", 2: "green",
                3: "blue", 4: "epic"
            }
        else:
            msg_type = "plain"  # Fallback

    if msg_type == "trade":
        item_data = _fetch_loot_data(
            config, zone_id, bot['level']
        )
        if not item_data:
            msg_type = "plain"  # Fallback

    if msg_type == "spell":
        spells = query_bot_spells(
            config, bot['class'], bot['level']
        )
        if spells:
            spell_data = random.choice(spells)
        else:
            msg_type = "plain"  # Fallback

    if msg_type == "npc":
        gossip_target = _pick_npc_gossip_target(
            config, zone_id
        )
        if gossip_target:
            gossip_target_type = "npc"
        else:
            msg_type = "plain"  # Fallback

    if msg_type == "bot":
        gossip_target = _pick_bot_gossip_target(
            config, cursor, zone_id, [bot['guid']]
        )
        if gossip_target:
            gossip_target_type = "bot"
        else:
            msg_type = "plain"  # Fallback

    # Fetch recent zone messages for anti-repetition
    recent_msgs = get_recent_zone_messages(
        db, zone_id
    )

    # Talent context injection (speaker only)
    speaker_talent = None
    talent_chance = int(config.get(
        'LLMChatter.TalentInjectionChance', '40',
    ))
    if (
        talent_chance > 0
        and random.randint(1, 100)
        <= talent_chance
    ):
        speaker_talent = build_talent_context(
            db, bot['guid'], bot['class'],
            bot['name'], perspective='speaker',
        )

    # Determine whether this statement is being generated
    # inside a capital city.
    is_capital = _is_capital_city_name(
        bot.get('zone', '')
    )

    # ZERO-TOKEN CAPITAL TEMPLATE - STATEMENT
    if (
        is_capital
        and mode != 'roleplay'
        and _maybe_emit_city_template(
            db,
            config,
            request,
            bot,
            zone_id,
        )
    ):
        return True

    # Pick RNG length for plain statements
    # (link types use default pool to avoid
    # forcing short on messages with WoW links)
    _, _, rng_length = pick_statement_length()

    # Build appropriate prompt
    chosen_topic = ""
    if msg_type == "plain":
        # Get zone mobs for context
        zone_mobs = []
        mobs = query_zone_mobs(
            config, zone_id, bot['level']
        )
        if mobs:
            zone_mobs = random.sample(
                mobs, min(10, len(mobs))
            )
        if is_capital and mode != 'roleplay':
            city_topics = [
                (
                    "Buying or selling something in city General chat. "
                    "Use natural WTB/WTS-style shorthand when appropriate. "
                    "Do not invent a specific item unless factual item data "
                    "was provided."
                ),
                (
                    "Specific portal chatter. NEVER say only 'need port' or "
                    "'any mages?'. Name a destination whenever possible. Use "
                    "natural Wrath-era shorthand such as 'LF port Dala tipping', "
                    "'WTB port IF', 'WTS ports SW/IF/Dala', or 'mage port to "
                    "Theramore?'. A speaker may only claim to personally sell "
                    "or cast a portal if their authoritative live class is Mage."
                ),
                (
                    "Specific profession or crafting chatter. NEVER say only "
                    "'any enchanters?' or 'need crafter'. Name the profession "
                    "and, when possible, the exact service, recipe, cut, enchant, "
                    "cooldown, or item type being requested. Examples of STYLE: "
                    "'LF enchanter +10 stats chest', 'need JC for runed scarlet "
                    "ruby', 'WTB titansteel cd', 'LFW BS pst', or 'LF alch for "
                    "transmute'. A speaker may only advertise a profession or "
                    "service they personally have if supported by authoritative "
                    "live state."
                ),
                (
                    "Specific LFG/LFM recruitment. NEVER say vague things like "
                    "'anyone lfg for raid', 'anyone want to run a dungeon', or "
                    "'looking for group'. Name a specific level-appropriate "
                    "dungeon or raid and, when natural, state exactly what is "
                    "needed: tank, heals, DPS, or number of players. Examples of "
                    "the STYLE: 'LF1M tank H UK', 'LFM Naxx10 need heals', "
                    "'need 2 dps for H Nexus', 'LF heals VoA10', or "
                    "'LFM Ulduar10 need tank'. These are style examples only; "
                    "choose content appropriate to the speaker's authoritative "
                    "live level. Do not claim raid clears, achievements, gearscore, "
                    "boss progress, or lockout state unless provided by live data."
                ),
                (
                    "Specific guild recruitment or guild-seeking chatter. NEVER "
                    "say only 'guild recruiting?' or 'need guild'. Include useful "
                    "details such as play style, level range, raid focus, PvP, "
                    "casual/social focus, schedule, or roles needed. Examples of "
                    "STYLE: 'LF casual raid guild 2 nights/week', 'social guild "
                    "recruiting 70+', 'LF guild doing Naxx10', or 'guild recruiting "
                    "heals for weekend raids'. Do not invent achievements, raid "
                    "progress, guild history, or schedules as factual claims unless "
                    "that information is actually available."
                ),
                (
                    "Specific Auction House or market chatter. NEVER say only "
                    "'price check?' or 'AH is expensive'. Name a specific item, "
                    "material, consumable, trade good, or market category whenever "
                    "authoritative item data is available. Natural examples of STYLE: "
                    "'price check frozen orb?', 'anyone buying saronite?', 'why are "
                    "flasks so expensive today', or 'WTB eternal fire'. Do not invent "
                    "exact gold values, market averages, or claimed bargains unless "
                    "authoritative price data was provided."
                ),
                (
                    "A quick practical city question: where to find a trainer, "
                    "vendor, bank, auction house, mailbox, profession service, "
                    "or other common city utility."
                ),
                (
                    "Casual city General chat between players waiting around, "
                    "shopping, banking, crafting, forming groups, or passing "
                    "through the capital."
                ),
            ]

            topic = random.choice(city_topics)
        else:
            topic_pool = (
                AMBIENT_CHAT_TOPICS_RP
                if mode == 'roleplay'
                else AMBIENT_CHAT_TOPICS
            )
            topic = random.choice(topic_pool)
        chosen_topic = topic
        prompt = build_plain_statement_prompt(
            bot, zone_id, zone_mobs,
            config, current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            topic=topic,
            area_id=area_id,
            length_hint=rng_length,
        )
    elif msg_type == "quest":
        prompt = build_quest_statement_prompt(
            bot, quest_data, config,
            current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            zone_id=zone_id,
        )
    elif msg_type == "loot":
        prompt = build_loot_statement_prompt(
            bot, item_data, item_can_use,
            config, current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            zone_id=zone_id,
        )
    elif msg_type == "quest_reward":
        prompt = build_quest_reward_statement_prompt(
            bot, quest_data, config,
            current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            zone_id=zone_id,
        )
        # Also set item_data for replacement
        if quest_data and quest_data.get('item1_name'):
            item_data = {
                'item_id': quest_data['item1_id'],
                'item_name': quest_data['item1_name'],
                'item_quality': quest_data.get(
                    'item1_quality', 2
                )
            }
    elif msg_type == "trade":
        prompt = build_trade_statement_prompt(
            bot, item_data, config,
            current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            zone_id=zone_id,
        )
    elif msg_type == "spell":
        prompt = build_spell_statement_prompt(
            bot, spell_data, config,
            current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            zone_id=zone_id,
        )
    elif msg_type in ("npc", "bot"):
        chosen_topic = (
            f"{gossip_target_type}:{gossip_target.get('name')}"
        )
        prompt = build_gossip_statement_prompt(
            bot, gossip_target, gossip_target_type,
            zone_id, config, current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            area_id=area_id,
            length_hint=rng_length,
        )
    else:
        if is_capital and mode != 'roleplay':
            city_topics = [
                (
                    "Buying or selling something in city General chat. "
                    "Use natural WTB/WTS-style shorthand when appropriate. "
                    "Do not invent a specific item unless factual item data "
                    "was provided."
                ),
                (
                    "Specific portal chatter. NEVER say only 'need port' or "
                    "'any mages?'. Name a destination whenever possible. Use "
                    "natural Wrath-era shorthand such as 'LF port Dala tipping', "
                    "'WTB port IF', 'WTS ports SW/IF/Dala', or 'mage port to "
                    "Theramore?'. A speaker may only claim to personally sell "
                    "or cast a portal if their authoritative live class is Mage."
                ),
                (
                    "Specific profession or crafting chatter. NEVER say only "
                    "'any enchanters?' or 'need crafter'. Name the profession "
                    "and, when possible, the exact service, recipe, cut, enchant, "
                    "cooldown, or item type being requested. Examples of STYLE: "
                    "'LF enchanter +10 stats chest', 'need JC for runed scarlet "
                    "ruby', 'WTB titansteel cd', 'LFW BS pst', or 'LF alch for "
                    "transmute'. A speaker may only advertise a profession or "
                    "service they personally have if supported by authoritative "
                    "live state."
                ),
                (
                    "Specific LFG/LFM recruitment. NEVER say vague things like "
                    "'anyone lfg for raid', 'anyone want to run a dungeon', or "
                    "'looking for group'. Name a specific level-appropriate "
                    "dungeon or raid and, when natural, state exactly what is "
                    "needed: tank, heals, DPS, or number of players. Examples of "
                    "the STYLE: 'LF1M tank H UK', 'LFM Naxx10 need heals', "
                    "'need 2 dps for H Nexus', 'LF heals VoA10', or "
                    "'LFM Ulduar10 need tank'. These are style examples only; "
                    "choose content appropriate to the speaker's authoritative "
                    "live level. Do not claim raid clears, achievements, gearscore, "
                    "boss progress, or lockout state unless provided by live data."
                ),
                (
                    "Specific guild recruitment or guild-seeking chatter. NEVER "
                    "say only 'guild recruiting?' or 'need guild'. Include useful "
                    "details such as play style, level range, raid focus, PvP, "
                    "casual/social focus, schedule, or roles needed. Examples of "
                    "STYLE: 'LF casual raid guild 2 nights/week', 'social guild "
                    "recruiting 70+', 'LF guild doing Naxx10', or 'guild recruiting "
                    "heals for weekend raids'. Do not invent achievements, raid "
                    "progress, guild history, or schedules as factual claims unless "
                    "that information is actually available."
                ),
                (
                    "Specific Auction House or market chatter. NEVER say only "
                    "'price check?' or 'AH is expensive'. Name a specific item, "
                    "material, consumable, trade good, or market category whenever "
                    "authoritative item data is available. Natural examples of STYLE: "
                    "'price check frozen orb?', 'anyone buying saronite?', 'why are "
                    "flasks so expensive today', or 'WTB eternal fire'. Do not invent "
                    "exact gold values, market averages, or claimed bargains unless "
                    "authoritative price data was provided."
                ),
                (
                    "A quick practical city question: where to find a trainer, "
                    "vendor, bank, auction house, mailbox, profession service, "
                    "or other common city utility."
                ),
                (
                    "Casual city General chat between players waiting around, "
                    "shopping, banking, crafting, forming groups, or passing "
                    "through the capital."
                ),
            ]

            topic = random.choice(city_topics)
        else:
            topic_pool = (
                AMBIENT_CHAT_TOPICS_RP
                if mode == 'roleplay'
                else AMBIENT_CHAT_TOPICS
            )
            topic = random.choice(topic_pool)
        prompt = build_plain_statement_prompt(
            bot, zone_id,
            config=config,
            current_weather=current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            topic=topic,
            area_id=area_id,
            length_hint=rng_length,
        )

    # Outdoor autonomous statements need the same public-channel
    # restraint layer used by outdoor conversations.
    if not is_capital and mode != 'roleplay':
        prompt += _outdoor_general_guard(
            [bot], zone_id, msg_type
        )

    # Call LLM
    if speaker_talent:
        zone_meta['speaker_talent'] = (
            speaker_talent
        )
    response = call_llm(
        client, prompt, config,
        context=f"ambient:{bot['name']}",
        label='ambient_statement',
        metadata=zone_meta,
    )

    if response:
        parsed = parse_single_response(response)
        message = parsed['message']
        message = replace_placeholders(
            message, quest_data, item_data,
            spell_data
        )
        message = cleanup_message(
            message, action=parsed.get('action')
        )

        if is_too_similar(message, recent_msgs):
            return True


        # Insert for delivery — enforce zone gap
        extra = _zone_delivery_delay(zone_id, config)
        topic_label = (
            f" topic={chosen_topic}"
            if chosen_topic else ""
        )
        logger.info(
            "[GEN-FLOW] ambient statement | "
            "type=%s%s bot=%s delay=%.1fs seq=0",
            msg_type, topic_label, bot['name'],
            extra,
        )
        insert_chat_message(
            db, bot['guid'], bot['name'], message,
            channel=channel,
            delay_seconds=extra,
            queue_id=request['id'],
            sequence=0,
        )
        if channel == 'general':
            maybe_queue_group_general_reaction(
                db, config,
                bot['guid'], bot['name'], message,
                zone_id, 0,
                source_queue_id=request['id'],
                source_sequence=0,
                source_delay_seconds=extra,
            )

        return True
    return False


# Recent outdoor two-bot exchanges.
# Used to stop the same pair from repeatedly dominating General.
_outdoor_pair_last = {}


def _outdoor_general_guard(bots, zone_id, msg_type):
    """Build hard factual constraints for outdoor General chat."""
    zone_name = get_zone_name(zone_id) or f"zone {zone_id}"

    lines = [
        "",
        "OUTDOOR GENERAL CHAT RULES:",
        f"- Current authoritative zone: {zone_name}.",
        "- General is a public zone broadcast, not a personal gameplay log.",
        "- Every message should have a plausible social reason for other "
        "players in the zone to hear it: ask for help, ask a practical "
        "question, offer help or an item, seek a group, coordinate content, "
        "trade, or make a remark that genuinely invites or contributes to "
        "conversation.",
        "- Do NOT broadcast routine personal progress such as ordinary kills, "
        "pulls, quest counters, travel, looting, bag management, gathering, "
        "or simply saying what you are currently doing.",
        "- If the source event is mundane, do not narrate the event merely "
        "because it happened. Use it only when it supports a believable "
        "social purpose.",
        "- Most messages should end normally. Do not habitually append lol, "
        "lmao, haha, ugh, smh, bruh, or similar filler.",
        "- Stay in the current zone context.",
        "- NEVER invent boats, zeppelins, portals, cities, "
        "travel destinations, NPC locations, or local events.",
        "- Mention another location only when authoritative live "
        "travel/activity data explicitly provides it.",
        "- Do not claim gear, upgrades, professions, spells, "
        "talents, quest progress, or class abilities unless the "
        "provided live state supports that exact claim.",
        "- Most General chat is mundane. Do not turn every line "
        "into a joke, punchline, or enthusiastic reaction.",
        "- Prefer short player-like messages, usually 3-12 words.",
        "- Questions and remarks do NOT need a response.",
        "- Avoid narrating your character as 'my mage', "
        "'my warrior', etc. Speak as the player.",
    ]

    if msg_type == "plain":
        lines.extend([
            "- CRITICAL: this is NOT the quest-chat path.",
            "- Do NOT invent or mention a specific quest.",
            "- Do NOT claim to need quest mobs, quest items, "
            "objective counts, turn-ins, or quest progress.",
            "- Quest-specific chatter is generated separately "
            "from authoritative active quest state.",
        ])

    for bot in bots:
        name = str(bot.get('name') or 'Bot')
        bot_class = str(bot.get('class') or '').strip()
        state = bot.get('bot_state')

        facts = []

        if bot_class:
            facts.append(f"class={bot_class}")

        if isinstance(state, dict):
            equipment = state.get('equipment') or []
            equipped_names = []

            for item in equipment:
                if not isinstance(item, dict):
                    continue

                item_name = str(
                    item.get('name') or ''
                ).strip()

                if item_name:
                    equipped_names.append(item_name)

            if equipped_names:
                facts.append(
                    "equipped=" +
                    ", ".join(equipped_names[:8])
                )

            activity = state.get('activity') or {}
            if isinstance(activity, dict):
                destination = str(
                    activity.get('destination') or ''
                ).strip()

                if destination:
                    facts.append(
                        f"authoritative_destination={destination}"
                    )

                quest_name = str(
                    activity.get('quest_name') or ''
                ).strip()

                if quest_name:
                    facts.append(
                        f"activity_quest={quest_name}"
                    )

        if facts:
            lines.append(
                f"- {name} live facts: " +
                "; ".join(facts)
            )

    return "\n".join(lines)


def _plain_outdoor_has_fake_quest_claim(message):
    """Reject obvious quest claims from the ungrounded plain path."""
    value = str(message or '').strip().lower()

    if not value:
        return False

    quest_markers = (
        " quest",
        "quest ",
        "objective",
        "turn in",
        "turn-in",
        "questline",
    )

    return any(marker in value for marker in quest_markers)


def process_conversation(
    db, cursor, client, config,
    request, bots: List[dict]
):
    """Process a conversation request with 2-4 bots.

    Args:
        db: Database connection
        cursor: Database cursor
        client: LLM provider client
        config: Configuration dict
        request: Queue request row
        bots: List of 2-4 bot dicts with guid, name,
              class, race, level, zone
    """
    channel = 'general'
    bot_count = len(bots)
    bot_names = [b['name'] for b in bots]

    # Create guid lookup for message insertion
    bot_guids = {b['name']: b['guid'] for b in bots}


    zone_id = request.get('zone_id', 0)
    area_id = request.get('area_id', zone_id)
    current_weather = request.get('weather') or None
    mode = get_chatter_mode(config)

    # Zone metadata for request logging
    zone_meta = _build_zone_metadata(
        zone_id, area_id
    )

    # Fetch recent zone messages for anti-repetition
    recent_msgs = get_recent_zone_messages(
        db, zone_id
    )

    # Talent context injection (speaker only,
    # uses first bot as representative)
    speaker_talent = None
    talent_chance = int(config.get(
        'LLMChatter.TalentInjectionChance', '40',
    ))
    if (
        talent_chance > 0
        and random.randint(1, 100)
        <= talent_chance
    ):
        speaker_talent = build_talent_context(
            db, bots[0]['guid'],
            bots[0]['class'],
            bots[0]['name'],
            perspective='speaker',
        )

    # Capitals use a city-specific General-chat mix.
    # Outdoor zones retain the existing ambient distribution.
    is_capital = _is_capital_city_name(
        bots[0].get('zone', '')
    )

    # Outdoor General should contain silence too.
    # Do not force every conversation opportunity to speak.
    if (
        not is_capital
        and mode != 'roleplay'
        and random.random() < 0.25
    ):
        logger.info(
            "[GEN-FLOW] outdoor conversation skipped "
            "for natural silence | zone=%s",
            zone_id,
        )
        return True

    # ZERO-TOKEN CAPITAL TEMPLATE - CONVERSATION
    # Capital conversation requests use deterministic
    # multi-bot exchanges instead of spending LLM tokens.
    if (
        is_capital
        and mode != 'roleplay'
        and _maybe_emit_city_template_conversation(
            db,
            config,
            request,
            bots,
            zone_id,
        )
    ):
        return True

    msg_type = _select_gossip_or_existing_type(
        config,
        conversation=True,
        is_capital=is_capital,
    )

    # Get quest/loot/spell data if needed
    quest_data = None
    item_data = None
    spell_data = None
    gossip_target = None
    gossip_target_type = None

    if msg_type == "quest":
        quest_owner = None
        quest_candidates = []

        # Prefer an actual live quest from one of the
        # participating bots.
        shuffled_bots = list(bots)
        random.shuffle(shuffled_bots)

        for candidate in shuffled_bots:
            active_quests = _get_bot_active_zone_quests(
                candidate,
                config,
                zone_id,
            )
            if active_quests:
                quest_owner = candidate
                quest_candidates = active_quests
                break

        if quest_candidates:
            quest_data = random.choice(
                quest_candidates
            )

            # Keep track of whose authoritative quest
            # state this actually belongs to.
            quest_data = dict(quest_data)
            quest_data['_owner_guid'] = (
                quest_owner.get('guid')
            )
            quest_data['_owner_name'] = (
                quest_owner.get('name')
            )
        else:
            msg_type = "plain"

    if msg_type == "loot":
        item_data = _fetch_loot_data(
            config, zone_id, bots[0]['level']
        )
        if not item_data:
            msg_type = "plain"

    if msg_type == "trade":
        item_data = _fetch_loot_data(
            config, zone_id, bots[0]['level']
        )
        if not item_data:
            msg_type = "plain"

    if msg_type == "spell":
        spells = query_bot_spells(
            config, bots[0]['class'],
            bots[0]['level']
        )
        if spells:
            spell_data = random.choice(spells)
        else:
            msg_type = "plain"

    speaker_guids = [b['guid'] for b in bots]
    if msg_type == "npc":
        gossip_target = _pick_npc_gossip_target(
            config, zone_id
        )
        if gossip_target:
            gossip_target_type = "npc"
        else:
            msg_type = "plain"

    if msg_type == "bot":
        gossip_target = _pick_bot_gossip_target(
            config, cursor, zone_id, speaker_guids
        )
        if gossip_target:
            gossip_target_type = "bot"
        else:
            msg_type = "plain"

    # Build prompt
    chosen_topic = ""
    if msg_type == "plain":
        # Get zone mobs for context
        zone_mobs = []
        mobs = query_zone_mobs(
            config, zone_id, bots[0]['level']
        )
        if mobs:
            zone_mobs = random.sample(
                mobs, min(10, len(mobs))
            )
        if is_capital and mode != 'roleplay':
            city_topics = [
                (
                    "Buying or selling something in city General chat. "
                    "Use natural WTB/WTS-style shorthand when appropriate. "
                    "Do not invent a specific item unless factual item data "
                    "was provided."
                ),
                (
                    "Specific portal chatter. NEVER say only 'need port' or "
                    "'any mages?'. Name a destination whenever possible. Use "
                    "natural Wrath-era shorthand such as 'LF port Dala tipping', "
                    "'WTB port IF', 'WTS ports SW/IF/Dala', or 'mage port to "
                    "Theramore?'. A speaker may only claim to personally sell "
                    "or cast a portal if their authoritative live class is Mage."
                ),
                (
                    "Specific profession or crafting chatter. NEVER say only "
                    "'any enchanters?' or 'need crafter'. Name the profession "
                    "and, when possible, the exact service, recipe, cut, enchant, "
                    "cooldown, or item type being requested. Examples of STYLE: "
                    "'LF enchanter +10 stats chest', 'need JC for runed scarlet "
                    "ruby', 'WTB titansteel cd', 'LFW BS pst', or 'LF alch for "
                    "transmute'. A speaker may only advertise a profession or "
                    "service they personally have if supported by authoritative "
                    "live state."
                ),
                (
                    "Specific LFG/LFM recruitment. NEVER say vague things like "
                    "'anyone lfg for raid', 'anyone want to run a dungeon', or "
                    "'looking for group'. Name a specific level-appropriate "
                    "dungeon or raid and, when natural, state exactly what is "
                    "needed: tank, heals, DPS, or number of players. Examples of "
                    "the STYLE: 'LF1M tank H UK', 'LFM Naxx10 need heals', "
                    "'need 2 dps for H Nexus', 'LF heals VoA10', or "
                    "'LFM Ulduar10 need tank'. These are style examples only; "
                    "choose content appropriate to the speaker's authoritative "
                    "live level. Do not claim raid clears, achievements, gearscore, "
                    "boss progress, or lockout state unless provided by live data."
                ),
                (
                    "Specific guild recruitment or guild-seeking chatter. NEVER "
                    "say only 'guild recruiting?' or 'need guild'. Include useful "
                    "details such as play style, level range, raid focus, PvP, "
                    "casual/social focus, schedule, or roles needed. Examples of "
                    "STYLE: 'LF casual raid guild 2 nights/week', 'social guild "
                    "recruiting 70+', 'LF guild doing Naxx10', or 'guild recruiting "
                    "heals for weekend raids'. Do not invent achievements, raid "
                    "progress, guild history, or schedules as factual claims unless "
                    "that information is actually available."
                ),
                (
                    "Specific Auction House or market chatter. NEVER say only "
                    "'price check?' or 'AH is expensive'. Name a specific item, "
                    "material, consumable, trade good, or market category whenever "
                    "authoritative item data is available. Natural examples of STYLE: "
                    "'price check frozen orb?', 'anyone buying saronite?', 'why are "
                    "flasks so expensive today', or 'WTB eternal fire'. Do not invent "
                    "exact gold values, market averages, or claimed bargains unless "
                    "authoritative price data was provided."
                ),
                (
                    "A quick practical city question: where to find a trainer, "
                    "vendor, bank, auction house, mailbox, profession service, "
                    "or other common city utility."
                ),
                (
                    "Casual city General chat between players waiting around, "
                    "shopping, banking, crafting, forming groups, or passing "
                    "through the capital."
                ),
            ]

            topic = random.choice(city_topics)
        else:
            topic_pool = (
                AMBIENT_CHAT_TOPICS_RP
                if mode == 'roleplay'
                else AMBIENT_CHAT_TOPICS
            )
            topic = random.choice(topic_pool)
        chosen_topic = topic
        prompt = build_plain_conversation_prompt(
            bots, zone_id, zone_mobs,
            config, current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            topic=topic,
            area_id=area_id,
        )
    elif msg_type == "quest":
        prompt = build_quest_conversation_prompt(
            bots, quest_data, config,
            current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            zone_id=zone_id,
        )
    elif msg_type == "trade":
        prompt = build_trade_conversation_prompt(
            bots, item_data, config,
            current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            zone_id=zone_id,
        )
    elif msg_type == "spell":
        prompt = build_spell_conversation_prompt(
            bots, spell_data, config,
            current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            zone_id=zone_id,
        )
    elif msg_type in ("npc", "bot"):
        chosen_topic = (
            f"{gossip_target_type}:{gossip_target.get('name')}"
        )
        prompt = build_gossip_conversation_prompt(
            bots, gossip_target, gossip_target_type,
            zone_id, config, current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            area_id=area_id,
        )
    else:  # loot
        prompt = build_loot_conversation_prompt(
            bots, item_data, config,
            current_weather,
            recent_messages=recent_msgs,
            speaker_talent_context=speaker_talent,
            zone_id=zone_id,
        )

    # Outdoor General gets a hard factual/restraint layer
    # regardless of which topic-specific prompt builder was used.
    if not is_capital and mode != 'roleplay':
        prompt += _outdoor_general_guard(
            bots, zone_id, msg_type
        )

    # Call LLM
    conversation_max_tokens = int(
        config.get(
            'LLMChatter.ConversationMaxTokens',
            config.get('LLMChatter.MaxTokens', 200)
        )
    )
    if not is_capital and mode != 'roleplay':
        # Outdoor General should normally fit in one short remark
        # and, occasionally, one short response.
        conversation_max_tokens = min(
            conversation_max_tokens, 90
        )

    if speaker_talent:
        zone_meta['speaker_talent'] = (
            speaker_talent
        )
    bot_names_ctx = ','.join(bot_names)
    response = call_llm(
        client, prompt, config,
        max_tokens_override=conversation_max_tokens,
        context=f"ambient-conv:{bot_names_ctx}",
        label='ambient_conv',
        metadata=zone_meta,
    )

    if response:
        messages = parse_conversation_response(
            response, bot_names
        )

        if not messages:
            repair_prompt = build_conversation_json_repair_prompt(
                prompt, bot_names,
            )
            response = call_llm(
                client, repair_prompt, config,
                max_tokens_override=(
                    conversation_max_tokens
                ),
                context="json-repair",
                label='ambient_conv',
                metadata=zone_meta,
            )
            if response:
                messages = (
                    parse_conversation_response(
                        response, bot_names
                    )
                )

        if (
            messages
            and not is_capital
            and mode != 'roleplay'
        ):
            now_mono = time.monotonic()

            pair_key = tuple(sorted(
                str(name)
                for name in bot_names[:2]
            ))

            last_pair_exchange = (
                _outdoor_pair_last.get(
                    pair_key, 0.0
                )
            )

            pair_recent = (
                last_pair_exchange > 0
                and now_mono - last_pair_exchange < 300.0
            )

            allow_reply = (
                len(messages) > 1
                and not pair_recent
                and random.random() < 0.22
            )

            if allow_reply:
                messages = messages[:2]
                _outdoor_pair_last[pair_key] = now_mono
            else:
                messages = messages[:1]

        if messages:
            # Zone gap applies to first message only;
            # conversation followups stagger on top
            base_delay = _zone_delivery_delay(
                zone_id, config
            )
            cumulative_delay = base_delay
            prev_msg_len = 0
            for i, msg in enumerate(messages):
                bot_guid = bot_guids.get(
                    msg['name'], bots[0]['guid']
                )

                # Replace placeholders and cleanup
                final_message = replace_placeholders(
                    msg['message'], quest_data,
                    item_data, spell_data
                )
                final_message = strip_speaker_prefix(
                    final_message, msg['name']
                )
                final_message = cleanup_message(
                    final_message,
                    action=(
                        msg.get('action')
                        if should_include_action()
                        else None
                    ),
                )

                if (
                    not is_capital
                    and mode != 'roleplay'
                    and msg_type == 'plain'
                    and _plain_outdoor_has_fake_quest_claim(
                        final_message
                    )
                ):
                    logger.info(
                        "[GEN-FLOW] dropped ungrounded "
                        "plain quest claim | bot=%s msg=%r",
                        msg['name'],
                        final_message,
                    )
                    continue

                if i > 0:
                    delay = calculate_dynamic_delay(
                        len(final_message), config,
                        prev_message_length=prev_msg_len,
                    )
                    cumulative_delay += delay
                prev_msg_len = len(final_message)

                topic_label = (
                    f" topic={chosen_topic}"
                    if chosen_topic else ""
                )
                logger.info(
                    "[GEN-FLOW] ambient conv | "
                    "type=%s%s bot=%s delay=%.1fs "
                    "seq=%d/%d",
                    msg_type, topic_label,
                    msg['name'],
                    cumulative_delay, i,
                    len(messages),
                )
                insert_chat_message(
                    db, bot_guid,
                    msg['name'], final_message,
                    channel=channel,
                    delay_seconds=cumulative_delay,
                    queue_id=request['id'],
                    sequence=i,
                )
                if channel == 'general':
                    maybe_queue_group_general_reaction(
                        db, config,
                        bot_guid, msg['name'],
                        final_message, zone_id, 0,
                        source_queue_id=request['id'],
                        source_sequence=i,
                        source_delay_seconds=(
                            cumulative_delay
                        ),
                    )


            # Push zone timestamp to after the last
            # message so the gap applies from the END
            # of the conversation, not the start
            _zone_last_delivery[zone_id] = (
                time.monotonic() + cumulative_delay
            )
            db.commit()
            return True
    return False
