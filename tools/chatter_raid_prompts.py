"""
chatter_raid_prompts.py — PvE raid-specific prompt
builders for mod-llm-chatter.

Each builder follows the signature:
    (extra_data, bot_data, is_raid_worker=False) -> str

Called by chatter_raids.py handlers via
dual_worker_dispatch.
"""

import logging
import random

from chatter_shared import (
    build_race_class_context,
    build_bot_identity,
    build_bot_state_context,
    build_anti_repetition_context,
    get_recent_zone_messages,
    append_json_instruction,
    get_chatter_mode,
)
from chatter_prompts import (
    pick_personality_spices,
    build_environmental_context_lines,
)

LOG = logging.getLogger("chatter_raid_prompts")

# -- Raid lore constants ---------------------------------

RAID_LORE = {
    # ── WotLK ──────────────────────────────────────
    'Icecrown Citadel': {
        'lore': (
            'The Lich King\'s throne atop '
            'Icecrown. The Ashen Verdict leads '
            'the final assault against Arthas '
            'and his undead armies.'),
        'tone': 'Dark, desperate, epic.',
        'landmarks': (
            'Lower Spire, Plagueworks, '
            'Crimson Hall, Frostwing Halls, '
            'the Frozen Throne. Saronite and '
            'ice architecture, oppressive cold.'),
    },
    'Ulduar': {
        'lore': (
            'Ancient titan city-prison in the '
            'Storm Peaks. The corrupted keepers '
            'guard Yogg-Saron\'s prison vault. '
            'The grandest raid in Northrend.'),
        'tone': 'Awe-inspiring, mysterious, grand.',
        'landmarks': (
            'Siege area, Antechamber, Keepers '
            'of Ulduar, the Descent into '
            'Madness, the Celestial Planetarium. '
            'Gleaming titan metal and stone.'),
    },
    'Naxxramas': {
        'lore': (
            'Floating necropolis of Kel\'Thuzad '
            'over Dragonblight. Four themed '
            'wings of undead horrors and the '
            'lich\'s inner sanctum.'),
        'tone': 'Creepy, tense, grim.',
        'landmarks': (
            'Arachnid Quarter (giant spiders), '
            'Plague Quarter (disease and '
            'abominations), Military Quarter '
            '(death knight commanders), '
            'Construct Quarter (flesh golems), '
            'Frostwyrm Lair. Green slime and '
            'dark stone.'),
    },
    'Vault of Archavon': {
        'lore': (
            'Titan vault beneath Wintergrasp '
            'Fortress, accessible only to the '
            'faction controlling Wintergrasp.'),
        'tone': 'Quick, practical, loot-focused.',
        'landmarks': (
            'Utilitarian titan chambers. Stone '
            'giants and elemental constructs. '
            'Massive but unadorned.'),
    },
    'The Obsidian Sanctum': {
        'lore': (
            'Volcanic chamber beneath Wyrmrest '
            'Temple where Sartharion guards '
            'black dragon eggs alongside '
            'three drake lieutenants.'),
        'tone': 'Intense, fiery, dragon-themed.',
        'landmarks': (
            'Lava rivers, obsidian platforms, '
            'three drake islands. Orange and '
            'red glow, heat shimmer.'),
    },
    'The Eye of Eternity': {
        'lore': (
            'Malygos\'s sanctum atop the Nexus '
            'above Coldarra. A platform in raw '
            'ley energy where the Spell-Weaver '
            'wages war on all magic users.'),
        'tone': 'Arcane, otherworldly, desperate.',
        'landmarks': (
            'No ground or walls. A disc of '
            'magical force over swirling blue '
            'and violet arcana. The heart of '
            'Azeroth\'s arcane storm.'),
    },
    'The Ruby Sanctum': {
        'lore': (
            'Red dragon sanctum beneath Wyrmrest '
            'Temple, invaded by the twilight '
            'dragonflight. Halion phases between '
            'physical and shadow realms.'),
        'tone': 'Urgent, fiery, apocalyptic.',
        'landmarks': (
            'Chamber shifts between warm ruby '
            'light and cold purple shadow. '
            'The last raid before the Cataclysm.'),
    },
    'Trial of the Crusader': {
        'lore': (
            'Argent Coliseum in Icecrown. '
            'A tournament arena that collapses '
            'into nerubian caverns below. '
            'Festive competition above, '
            'ancient terror below.'),
        'tone': 'Competitive, tense, dramatic.',
        'landmarks': (
            'Tournament arena with banners and '
            'crowds, underground nerubian '
            'cavern. Anub\'arak lurks below.'),
    },
    # ── TBC ────────────────────────────────────────
    'Karazhan': {
        'lore': (
            'Medivh\'s haunted tower in '
            'Deadwind Pass. Echoes of the Last '
            'Guardian play out eternally in '
            'rooms that shift and bend time.'),
        'tone': 'Eerie, magical, surreal.',
        'landmarks': (
            'Ballroom, Opera stage, Chess '
            'event, Netherspace. '
            'Spectral dinner party. The tower '
            'exists partially outside reality.'),
    },
    'Gruul\'s Lair': {
        'lore': (
            'Rough cavern complex in Blade\'s '
            'Edge Mountains. Home to Gruul the '
            'Dragonkiller and his gronn sons. '
            'Dragon bones litter the floor.'),
        'tone': 'Brutal, primal, savage.',
        'landmarks': (
            'Raw stone caves, ogre servants, '
            'dragon bone trophies. No '
            'architecture, just giant fists '
            'and brute force.'),
    },
    'Magtheridon\'s Lair': {
        'lore': (
            'A single brutal chamber beneath '
            'Hellfire Citadel where the pit '
            'lord Magtheridon is chained. '
            'Channelers maintain his prison.'),
        'tone': 'Oppressive, demonic, punishing.',
        'landmarks': (
            'One deadly room. Hellfire energy, '
            'demon blood and brimstone. '
            'No room for error.'),
    },
    'Serpentshrine Cavern': {
        'lore': (
            'Lady Vashj\'s underwater palace '
            'in Coilfang Reservoir. Naga '
            'architecture meets the raw power '
            'of a subterranean ocean.'),
        'tone': (
            'Elegant, aquatic, treacherous.'),
        'landmarks': (
            'Waterfalls, luminous pools, '
            'bridges over underground lakes. '
            'Naga, tidewalkers, colossal '
            'hydras. Corrupted Zangarmarsh '
            'waters.'),
    },
    'Tempest Keep': {
        'lore': (
            'Kael\'thas Sunstrider\'s captured '
            'naaru fortress floating above '
            'Netherstorm. Crystalline draenei '
            'technology repurposed by desperate '
            'blood elves.'),
        'tone': 'Arcane, alien, beautiful.',
        'landmarks': (
            'Shimmering crystal chambers. '
            'Blood elf advisors, void '
            'creatures. Stunning views of '
            'shattered Netherstorm.'),
    },
    'Battle for Mount Hyjal': {
        'lore': (
            'Caverns of Time raid during the '
            'Battle of Mount Hyjal. Waves of '
            'undead and demons assault three '
            'bases in succession with legendary '
            'heroes at your side.'),
        'tone': 'Epic, heroic, desperate.',
        'landmarks': (
            'Alliance base (Jaina), Horde base '
            '(Thrall), night elf camp at '
            'Nordrassil. The forest burns. '
            'Archimonde approaches.'),
    },
    'Black Temple': {
        'lore': (
            'Illidan\'s fortress in Shadowmoon '
            'Valley. A draenei temple corrupted '
            'by demonic occupation. Fel orcs, '
            'demons, naga, and blood elves '
            'serve the Betrayer.'),
        'tone': 'Dark, grand, corrupted.',
        'landmarks': (
            'Sprawling courtyards, sewer '
            'systems, grand halls. Cracked '
            'holy symbols, defiled altars, '
            'green fel fire. Illidan\'s throne '
            'awaits at the summit.'),
    },
    'Sunwell Plateau': {
        'lore': (
            'The heart of the Sunwell on the '
            'Isle of Quel\'Danas. The Burning '
            'Legion attempts to summon '
            'Kil\'jaeden through the Sunwell '
            'itself.'),
        'tone': 'Holy, desperate, climactic.',
        'landmarks': (
            'Pristine elven architecture. '
            'The Sunwell\'s holy light clashes '
            'with demonic darkness. The final '
            'stand of the Burning Crusade.'),
    },
    # ── Classic ────────────────────────────────────
    'Molten Core': {
        'lore': (
            'The burning heart of Blackrock '
            'Mountain. Ragnaros the Firelord '
            'rules this realm of pure fire. '
            'The ultimate trial by fire.'),
        'tone': 'Fiery, apocalyptic, primal.',
        'landmarks': (
            'Lava rivers between obsidian '
            'platforms. Core hounds, molten '
            'giants, flamewakers. The heat '
            'is overwhelming.'),
    },
    'Blackwing Lair': {
        'lore': (
            'Nefarian\'s dark laboratory atop '
            'Blackrock Spire where the black '
            'dragon experiments on other '
            'dragonflights. Clinical and '
            'sinister.'),
        'tone': 'Sinister, experimental, draconic.',
        'landmarks': (
            'Dark iron and dragon bone halls. '
            'Drakonid soldiers, chromatic '
            'drakes, failed experiments. '
            'A mad scientist\'s lair at '
            'dragon scale.'),
    },
    'Onyxia\'s Lair': {
        'lore': (
            'A vast cavern in Dustwallow '
            'Marsh, home to the broodmother '
            'Onyxia. Whelps swarm, lava '
            'bubbles, dragonfire fills the '
            'chamber.'),
        'tone': 'Intense, claustrophobic, fiery.',
        'landmarks': (
            'Narrow scorched tunnel opening '
            'into enormous cavern. Bones, '
            'egg clutches, lava edges.'),
    },
    'Ruins of Ahn\'Qiraj': {
        'lore': (
            'Open-air battlefield in Silithus '
            'where qiraji insectoid forces '
            'mass for war. Sand-swept '
            'courtyards and crumbling temple '
            'ruins.'),
        'tone': 'Alien, harsh, warlike.',
        'landmarks': (
            'Insectoid warriors, obsidian '
            'destroyers. Architecture is '
            'half Egyptian tomb, half insect '
            'hive. Desert wind and clicking '
            'of countless legs.'),
    },
    'Temple of Ahn\'Qiraj': {
        'lore': (
            'Sealed inner sanctum of the '
            'qiraji empire. The old god '
            'C\'Thun lurks within. Walls '
            'pulse with organic growth, '
            'eyes watch from every surface.'),
        'tone': (
            'Alien, disturbing, oppressive.'),
        'landmarks': (
            'Twin emperors\' chamber, '
            'organic corridors, C\'Thun\'s '
            'stomach. Reality bends near '
            'the old god\'s prison. The most '
            'alien place in Azeroth.'),
    },
    'Zul\'Gurub': {
        'lore': (
            'Massive troll temple in '
            'Stranglethorn jungle. The '
            'Gurubashi have unleashed the '
            'blood god Hakkar. Overgrown '
            'courtyards and sacrificial '
            'altars.'),
        'tone': 'Primal, voodoo, tropical.',
        'landmarks': (
            'Snake priests, bat riders, '
            'tiger cultists. Sacrificial '
            'altars dripping with blood '
            'magic. The jungle pulses with '
            'primal voodoo energy.'),
    },
}

# -- Shared constraints ----------------------------------

RAID_EMOTE_GUIDANCE = (
    "NEVER put /slash commands or emote commands "
    "in your message text. No /roar, /cheer, "
    "/say, /yell, /battleshout, /angry, or any "
    "/command. Just write plain speech. Emotes "
    "are handled separately — do NOT include them "
    "in your text at all."
)

BREVITY_INSTRUCTION = (
    "Keep it VERY SHORT. One sentence only. "
    "Aim for roughly 6 to 14 words. Raid chat "
    "is fast and urgent. No paragraphs, no "
    "poetry, no contemplation."
)

RAID_NORMAL_GUIDANCE = (
    "You are a real WoW player controlling this character, "
    "not the fantasy character roleplaying in Azeroth. "
    "Write like an actual player typing during a raid. "
    "Gameplay terminology is completely normal: boss, pull, "
    "wipe, tank, heals, healer, dps, threat, aggro, adds, "
    "mechanic, phase, cd, brez, lust, hero, flask, food, "
    "buffs, loot, roll, repair, afk, ready, etc. "
    "Use normal WoW shorthand when it fits. Lowercase, "
    "fragments, missing punctuation, occasional typos, and "
    "one-word reactions are fine. Casual internet language "
    "like lol, lmao, bruh, rip, tbh, or ngl is fine "
    "occasionally but should not be forced. "
    "The player can be tired, distracted, confused, salty, "
    "amused, excited, or completely mundane. "
    "Do not turn ordinary raid chat into a speech. "
    "Do not narrate the environment or describe the scene. "
    "Do not write fantasy dialogue."
)

# -- Shared context builder ------------------------------

def _raid_base_context(extra_data, bot_data):
    """Build shared PvE raid context block."""
    config = extra_data.get('_config') or {}
    chatter_mode = get_chatter_mode(config)
    is_rp = (chatter_mode == 'roleplay')

    # Bot identity
    bot_name = bot_data.get('bot_name', 'Unknown')
    race = bot_data.get('race', '')
    cls = bot_data.get('class', '')
    gender = bot_data.get('gender', '')
    traits = bot_data.get('traits')

    # Raid info
    raid_name = extra_data.get(
        'raid_name', 'an unknown raid')
    wing = extra_data.get('wing', '')
    difficulty = extra_data.get(
        'difficulty', 'Normal')
    lore_entry = RAID_LORE.get(raid_name, {})

    # Talent context
    talent_ctx = extra_data.get(
        '_talent_context', '')

    if is_rp:
        rc_ctx = ''
        if race and cls:
            rc_ctx = build_race_class_context(
                race, cls)

        spice_str = ''
        if config:
            spices = pick_personality_spices(
                config,
                spice_count_override=1,
            )
            if spices:
                spice_str = ', '.join(spices)

        env_lines = (
            build_environmental_context_lines()
        )

        ctx = f"You are {bot_name}"
        if race and cls:
            ctx = build_bot_identity(
                bot_name,
                race,
                cls,
                gender,
            )[:-1]

        ctx += f", raiding {raid_name}"

        if wing:
            ctx += f" ({wing})"

        ctx += (
            f". Difficulty: {difficulty}.\n"
        )

        if env_lines:
            ctx += "\n".join(env_lines) + "\n"

        if lore_entry.get('lore'):
            ctx += (
                f"Lore: {lore_entry['lore']}\n"
            )

        if lore_entry.get('landmarks'):
            ctx += (
                "Setting: "
                f"{lore_entry['landmarks']}\n"
            )

        if lore_entry.get('tone'):
            ctx += (
                f"Tone: {lore_entry['tone']}\n"
            )

        if traits:
            trait_str = ', '.join(
                str(t) for t in traits[:3])
            ctx += (
                f"Your personality: {trait_str}\n"
            )

        if rc_ctx:
            ctx += f"{rc_ctx}\n"

        if spice_str:
            ctx += (
                "Background flavor: "
                f"{spice_str}\n"
            )

    else:
        ctx = (
            f"You are {bot_name}, a real WoW player "
            "controlling"
        )

        if cls:
            ctx += f" a {cls}"

        ctx += f" in {raid_name}"

        if wing:
            ctx += f" ({wing})"

        ctx += (
            f". Difficulty: {difficulty}.\n"
        )

        if traits:
            trait_str = ', '.join(
                str(t) for t in traits[:3])
            ctx += (
                f"General personality tendencies: "
                f"{trait_str}\n"
            )

    if talent_ctx:
        ctx += f"{talent_ctx}\n"

    bot_state = bot_data.get(
        'bot_state', {}
    )

    factual_context = build_bot_state_context(
        bot_state
    )

    if factual_context:
        ctx += (
            "\nAuthoritative current state for "
            f"{bot_name}:\n"
        )
        ctx += factual_context + "\n"

        if not is_rp:
            ctx += (
                "\nLIVE STATE RULES:\n"
                "- The authoritative bot state above "
                "overrides personality, previous messages, "
                "and other context for specific factual "
                "claims about this bot.\n"
                "- Use it for specific facts about level, "
                "quests, objectives, counts, inventory, "
                "equipment, professions, money, location, "
                "and current activity.\n"
                "- Never invent a quest name, objective, "
                "mob, item, NPC, number, destination, "
                "profession, equipment item, or other "
                "specific game-state fact.\n"
                "- If a specific fact is absent from the "
                "authoritative state, do not guess it.\n"
                "- Supplied [[quest:...]] and [[item:...]] "
                "tokens are exact opaque strings. Copy a "
                "token exactly when relevant or omit it. "
                "Never create or modify a token.\n"
            )

    # Anti-repetition
    db = extra_data.get('_db')
    if db:
        zone_id = int(
            extra_data.get('zone_id', 0))
        if zone_id:
            recent = get_recent_zone_messages(
                db,
                zone_id,
                limit=8,
                minutes=10,
            )
            anti_rep = (
                build_anti_repetition_context(
                    recent,
                    max_items=6,
                )
            )
            if anti_rep:
                ctx += f"{anti_rep}\n"

    ctx += f"\n{BREVITY_INSTRUCTION}\n"
    ctx += f"{RAID_EMOTE_GUIDANCE}\n"

    if not is_rp:
        ctx += f"{RAID_NORMAL_GUIDANCE}\n"

    return ctx


# -- Prompt builders -------------------------------------

def build_raid_boss_pull_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Boss pull reaction."""
    boss_name = extra_data.get(
        'boss_name', 'the boss')
    raid_name = extra_data.get(
        'raid_name', 'the raid')
    wing = extra_data.get('wing', '')
    difficulty = extra_data.get(
        'difficulty', 'Normal')

    ctx = _raid_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    if wing:
        ctx += (
            f"Your raid is about to engage "
            f"{boss_name} in {wing} of "
            f"{raid_name} ({difficulty}).\n"
        )
    else:
        ctx += (
            f"Your raid is about to engage "
            f"{boss_name} in "
            f"{raid_name} ({difficulty}).\n"
        )

    if is_rp:
        if is_raid_worker:
            ctx += (
                "You are addressing the entire raid. "
                "Give one short rally, command, or "
                "bold declaration before the fight."
            )
        else:
            ctx += (
                "You are talking to your squad before "
                "the fight. Brief nervousness, excitement, "
                "or battle-ready determination is fine."
            )
    else:
        if is_raid_worker:
            ctx += (
                "Write the kind of short raid-chat message "
                "a real player might type just before a "
                "boss pull. It could be 'rdy', 'pulling', "
                "'gl', 'lust on pull?', 'lets go', a quick "
                "reminder, or a casual reaction. "
                "Do not force leadership, hype, or a speech."
            )
        else:
            ctx += (
                "Write a short party-chat reaction before "
                "the pull. It can be practical, nervous, "
                "casual, or low-effort. Do not force "
                "excitement or drama."
            )

    return append_json_instruction(
        ctx,
        allow_action=(not is_raid_worker),
        skip_emote=is_raid_worker,
    )


def build_raid_boss_kill_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Boss kill reaction."""
    boss_name = extra_data.get(
        'boss_name', 'the boss')

    ctx = _raid_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    ctx += (
        f"Your raid has defeated {boss_name}.\n"
    )

    if is_rp:
        if is_raid_worker:
            ctx += (
                "Give a brief triumphant victory "
                "announcement to the raid."
            )
        else:
            ctx += (
                "React with relief, joy, exhaustion, "
                "or humor with your squad."
            )
    else:
        ctx += (
            "React like a real raider after a boss dies. "
            "A short 'gg', 'nice', 'finally', 'ez', "
            "'good kill', loot comment, relief, joke, "
            "or almost no reaction is fine. "
            "Do not force celebration or praise."
        )

    return append_json_instruction(
        ctx,
        allow_action=(not is_raid_worker),
        skip_emote=is_raid_worker,
    )


def build_raid_boss_wipe_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Boss wipe reaction."""
    boss_name = extra_data.get(
        'boss_name', 'the boss')

    ctx = _raid_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    ctx += (
        f"Your raid just wiped on {boss_name}.\n"
    )

    if is_rp:
        if is_raid_worker:
            ctx += (
                "Address the raid briefly after the wipe. "
                "Rally them without blaming individuals."
            )
        else:
            ctx += (
                "React to the wipe with frustration, "
                "dark humor, or determination."
            )
    else:
        ctx += (
            "React like a real raider after a wipe. "
            "It can be 'rip', 'my bad', 'what happened', "
            "'again', 'almost', a mechanics comment, "
            "mild blame, a joke, silence-adjacent annoyance, "
            "or a practical reset comment. "
            "Do not force positivity or motivational talk."
        )

    return append_json_instruction(
        ctx,
        allow_action=(not is_raid_worker),
        skip_emote=is_raid_worker,
    )


def build_raid_battle_cry_prompt(
    extra_data, bot_data, is_raid_worker=True
):
    """Short combat reaction in raid chat."""
    creature_name = extra_data.get(
        'creature_name', 'the enemy')
    is_boss = bool(int(
        extra_data.get('is_boss', 0)))

    ctx = _raid_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    if is_boss:
        ctx += (
            f"Your raid is engaging boss "
            f"{creature_name}.\n"
        )
    else:
        ctx += (
            f"Your raid is fighting elite "
            f"{creature_name}.\n"
        )

    if is_rp:
        ctx += (
            "Write one short, punchy battle cry. "
            "5 to 15 words maximum. A war shout, "
            "rallying call, fierce declaration, or "
            "race/class-flavored combat line is appropriate. "
            "No narration."
        )
    else:
        ctx += (
            "Write one very short raid-chat reaction "
            "someone could realistically type while "
            "combat is starting. A target callout, "
            "'go', 'burn', 'adds', 'lust', 'here we go', "
            "'oh boy', or another quick reaction is fine. "
            "Do not write a fantasy battle cry."
        )

    return append_json_instruction(
        ctx,
        allow_action=False,
        skip_emote=True,
    )


def build_raid_banter_prompt(
    extra_data, bot_data, is_raid_worker=True
):
    """Casual raid banter between pulls."""
    ctx = _raid_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    if is_rp:
        banter_topics = random.choice([
            "a funny observation about the raid environment",
            "a playful jab at a fellow raider",
            "a lore tidbit or rumor about this place",
            "a humorous complaint about trash mobs",
            "an irreverent comment about the bosses",
            "something odd about the raid",
            "repair bills or consumable costs",
            "food, drink, or downtime",
            "someone going AFK",
            "loot drama or RNG luck",
        ])

        ctx += (
            "You are making casual banter in raid chat "
            "between pulls. Be humorous, observational, "
            "or playful. Not tactical or motivational.\n"
            f"Topic hint: {banter_topics}\n"
            "One short sentence."
        )

    else:
        banter_topics = random.choice([
            "someone going afk",
            "waiting for a pull",
            "repair bills",
            "consumables or flasks",
            "loot RNG",
            "a recent wipe",
            "a recent good pull",
            "someone taking forever",
            "trash mobs",
            "running back",
            "bags being full",
            "needing a summon",
            "someone forgetting a buff",
            "a class doing something annoying",
            "food or drinks",
            "being tired",
            "the raid taking longer than expected",
            "the boss being easier than expected",
            "the boss being more annoying than expected",
            "nothing important at all",
        ])

        ctx += (
            "You are casually typing in raid chat "
            "between pulls like a real player. "
            f"Possible topic: {banter_topics}. "
            "The topic is optional; do not force it. "
            "The message can be boring, incomplete, "
            "sarcastic, mildly salty, or one-word. "
            "Do not force a joke or conversation starter."
        )

    return append_json_instruction(
        ctx,
        allow_action=False,
        skip_emote=True,
    )


def build_raid_morale_prompt(
    extra_data, bot_data, is_raid_worker=True
):
    """Idle raid chat between pulls."""
    ctx = _raid_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    if is_rp:
        topics = random.choice([
            "morale boost or encouragement",
            "tactical banter about the next pull",
            "readiness check",
            "raid encouragement",
            "idle commentary",
            "joke or light trash talk",
            "compliment a recent play",
            "a past wipe or close call",
            "raid atmosphere",
            "repair bills or consumables",
            "what loot may drop",
            "raid history or lore",
            "who is pulling their weight",
            "how the raid is progressing",
            "anticipation about the next boss",
            "food and flask buffs",
            "AFK raiders",
            "trash mobs",
        ])

        ctx += (
            "You are chatting in raid chat between pulls. "
            "The mood is relaxed but focused. "
            f"Topic hint: {topics}. "
            "Keep it brief."
        )

    else:
        topics = random.choice([
            "ready check",
            "who is afk",
            "food or flask buffs",
            "next pull",
            "boss mechanics",
            "whether everyone is ready",
            "someone missing",
            "how the last pull went",
            "whether to lust",
            "cooldowns",
            "a recent mistake",
            "a recent good play",
            "repairs",
            "loot",
            "progress",
            "waiting around",
            "nothing especially important",
        ])

        ctx += (
            "Write a natural raid-chat message between "
            "pulls. It can be practical or casual. "
            f"Possible topic: {topics}. "
            "Do not act like the raid leader unless that "
            "naturally fits the message. "
            "Do not force encouragement, authority, "
            "or a complete thought."
        )

    return append_json_instruction(
        ctx,
        allow_action=False,
        skip_emote=True,
    )
