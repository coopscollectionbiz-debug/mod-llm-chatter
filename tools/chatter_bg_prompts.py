"""
chatter_bg_prompts.py — BG-specific prompt builders for
mod-llm-chatter.

Uses get_class_name() from chatter_shared to resolve
class IDs from C++ extra_data.

Each builder follows the signature:
    (extra_data, bot_data, is_raid_worker=False) -> str

Called by chatter_battlegrounds.py handlers via
dual_worker_dispatch.
"""

import logging
import random

from chatter_shared import (
    get_class_name,
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
from chatter_constants import BG_LORE

LOG = logging.getLogger("chatter_bg_prompts")

# ── Shared constraints ────────────────────────────────

OBSERVATION_CONSTRAINT = (
    "CRITICAL RULE: You are an observer. React to "
    "what happened. NEVER claim you are doing or "
    "will do something \u2014 your actual behavior may "
    "contradict the message. No action declarations, "
    "no promises, no movement plans. Only reactive "
    "observations and generic encouragement."
)

BREVITY_INSTRUCTION = (
    "Keep it VERY SHORT. One sentence only. "
    "Aim for roughly 6 to 14 words. BG chat is "
    "fast and urgent. No paragraphs, no poetry, "
    "no contemplation, no long explanations."
)

BG_EMOTE_GUIDANCE = (
    "NEVER put /slash commands or emote commands "
    "in your message text. No /roar, /cheer, "
    "/say, /yell, /battleshout, /angry, or any "
    "/command. Just write plain speech. Emotes "
    "are handled separately — do NOT include them "
    "in your text at all."
)
BG_NORMAL_GUIDANCE = (
    "You are a real WoW player controlling this character, "
    "not the fantasy character roleplaying in Azeroth. "
    "Write like actual battleground chat typed while playing. "
    "Gameplay language is normal: inc, def, cap, flag, fc, "
    "efc, heals, healer, tank, dps, mid, base, node, rez, "
    "gy, push, wipe, team, score, etc. "
    "Normal shorthand, lowercase, fragments, missing "
    "punctuation, and occasional typos are fine. "
    "Casual internet language like lol, lmao, bruh, rip, "
    "ngl, or tbh is fine occasionally, but do not force it. "
    "The message can be mundane, annoyed, salty, confused, "
    "excited, or low-effort. Do not make every line a battle "
    "cry, joke, speech, rally, or dramatic declaration. "
    "Do not narrate the battlefield or scenery. "
    "Do not write fantasy dialogue."
)


# ── Shared context builder ────────────────────────────

def _bg_base_context(
    extra_data, bot_data,
    db=None, config=None,
    skip_observation_constraint=False
):
    """Build shared BG context block for all prompts.

    Args:
        extra_data: Parsed extra_data from event.
            May contain '_db' and '_config' keys
            injected by the dispatch layer.
        bot_data: Bot traits or lightweight data.
        db: Optional DB connection for anti-rep.
            Falls back to extra_data['_db'].
        config: Optional config dict for spices.
            Falls back to extra_data['_config'].
    """
    # Allow injected refs from dispatch layer
    if db is None:
        db = extra_data.get('_db')
    if config is None:
        config = extra_data.get('_config')

    chatter_mode = get_chatter_mode(config or {})
    is_rp = (chatter_mode == 'roleplay')

    bg_type_id = int(extra_data.get('bg_type_id', 0))
    lore = BG_LORE.get(bg_type_id, {})
    team = extra_data.get('team', 'Unknown')

    faction_name = lore.get(
        f'{team.lower()}_faction', team)

    score_a = int(
        extra_data.get('score_alliance', 0))
    score_h = int(
        extra_data.get('score_horde', 0))

    # Bot identity (full traits or lightweight)
    bot_name = bot_data.get('bot_name', 'Unknown')
    traits = bot_data.get('traits')
    race = bot_data.get('race', '')
    cls = bot_data.get('class', '')
    gender = bot_data.get('gender', '')

    # Environmental context
    if is_rp:
        env_lines = build_environmental_context_lines()

        ctx = (
            f"You are {bot_name}"
        )
        if race and cls:
            ctx = build_bot_identity(
                bot_name, race, cls, gender
            )[:-1]

        ctx += (
            f", fighting in "
            f"{lore.get('name', 'a battleground')}. "
            f"You fight for the {faction_name} "
            f"({team}).\n"
            f"Score: Alliance {score_a} — "
            f"Horde {score_h}.\n"
            f"Alive on your team: "
            f"{extra_data.get('players_alive_team', '?')}. "
            f"Alive on enemy team: "
            f"{extra_data.get('players_alive_enemy', '?')}.\n"
            + "\n".join(env_lines) + "\n"
        )
    else:
        ctx = (
            f"You are {bot_name}, a real WoW player "
            f"controlling a"
        )

        if cls:
            ctx += f" {cls}"

        ctx += (
            f" character in "
            f"{lore.get('name', 'a battleground')} "
            f"on the {team} team.\n"
            f"Score: Alliance {score_a} — "
            f"Horde {score_h}.\n"
            f"Alive on your team: "
            f"{extra_data.get('players_alive_team', '?')}. "
            f"Alive on enemy team: "
            f"{extra_data.get('players_alive_enemy', '?')}.\n"
        )

    # Flag carrier status (WSG)
    friendly_fc = extra_data.get(
        'friendly_flag_carrier')
    enemy_fc = extra_data.get(
        'enemy_flag_carrier')
    if friendly_fc:
        ctx += (
            f"Your teammate {friendly_fc} is "
            f"carrying the enemy flag!\n"
        )
    if enemy_fc:
        ctx += (
            f"Enemy {enemy_fc} is carrying "
            f"YOUR flag!\n"
        )

    # Real players on the team (name + race)
    real_players = extra_data.get('real_players')
    if real_players and isinstance(
            real_players, list):
        parts = []
        for rp in real_players:
            n = rp.get('name', '?')
            r = rp.get('race', '')
            parts.append(
                f"{n} ({r})" if r else n)
        if parts:
            ctx += (
                "Real players on your team: "
                + ", ".join(parts) + ".\n"
            )

    if is_rp and lore.get('lore'):
        ctx += f"Lore: {lore['lore']}\n"

    if is_rp and lore.get('landmarks'):
        ctx += f"{lore['landmarks']}\n"

    if traits:
        trait_str = ', '.join(
            str(t) for t in traits[:3])
        ctx += f"Your personality: {trait_str}\n"

    # Race/class personality context is RP-only.
    if is_rp and race and cls:
        rp_ctx = build_race_class_context(race, cls)
        if rp_ctx:
            ctx += f"{rp_ctx}\n"

    # RP gets explicit fantasy/personality spices.
    # Normal relies on the global casual style plus traits.
    if is_rp and config:
        spices = pick_personality_spices(
            config, spice_count_override=1)
        if spices:
            ctx += (
                f"Background flavor: "
                f"{', '.join(spices)}\n"
            )

    # Talent context (injected by dispatch layer)
    talent_ctx = extra_data.get(
        '_talent_context')
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
    if db:
        zone_id = int(
            extra_data.get('zone_id', 0))
        if zone_id:
            recent = get_recent_zone_messages(
                db, zone_id, limit=8, minutes=10)
            anti_rep = build_anti_repetition_context(
                recent, max_items=6)
            if anti_rep:
                ctx += f"{anti_rep}\n"

    if not skip_observation_constraint:
        ctx += f"\n{OBSERVATION_CONSTRAINT}\n"

    ctx += f"{BREVITY_INSTRUCTION}\n"
    ctx += f"{BG_EMOTE_GUIDANCE}\n"

    if not is_rp:
        ctx += f"{BG_NORMAL_GUIDANCE}\n"

    return ctx


# ── Prompt builders ───────────────────────────────────

def build_bg_match_start_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Match start reaction."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    if is_rp:
        ctx += (
            "\nThe gates just opened! The match is "
            "starting. React with a battle cry, "
            "faction pride, or encouragement for "
            "your team. Be fierce and energetic."
        )
    else:
        ctx += (
            "\nThe battleground just started. Say the kind "
            "of brief thing a real player might type as the "
            "gates open. It could be a quick plan, casual "
            "reaction, mild hype, complaint, joke, or "
            "nothing more elaborate than 'gl' or 'lets go'. "
            "Do not force a rallying cry."
        )

    return append_json_instruction(
        ctx, allow_action=False, skip_emote=True
    )

def build_bg_match_end_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Match end \u2014 victory or defeat."""
    ctx = _bg_base_context(extra_data, bot_data)
    won = extra_data.get('won', False)

    # Override score with final_score if available
    # (AppendBGContext snapshot may miss the
    # winning capture)
    fs_a = extra_data.get('final_score_alliance')
    fs_h = extra_data.get('final_score_horde')
    if fs_a is not None and fs_h is not None:
        ctx += (
            f"Final score: Alliance {fs_a} "
            f"- Horde {fs_h}.\n"
        )

    # Performance stats (may be absent for bots)
    kb = extra_data.get('player_killing_blows')
    dmg = extra_data.get('player_damage_done')
    heal = extra_data.get('player_healing_done')
    if kb is not None:
        ctx += (
            f"Team performance glimpse: "
            f"{kb} killing blows, "
            f"{dmg} damage, {heal} healing.\n"
        )
    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )
    if is_rp:
        if won:
            ctx += (
                "\nYour team WON! React with "
                "celebration, faction pride, or a "
                "victory cheer. Reference the final "
                "score if meaningful."
            )
        else:
            ctx += (
                "\nYour team LOST. React with "
                "frustration, defiance, or honorable "
                "defeat. No whining — keep dignity."
            )
    else:
        if won:
            ctx += (
                "\nYour team won. React like a real player "
                "at the end of a BG. The response can be "
                "brief excitement, relief, praise, mild "
                "trash talk, 'gg', or a casual comment "
                "about how the match went."
            )
        else:
            ctx += (
                "\nYour team lost. React like a real player "
                "at the end of a BG. Frustration, 'gg', "
                "complaining, shrugging it off, blaming a "
                "bad play, or a dry joke are all fine. "
                "Do not force dignity or heroic defiance."
            )
    return append_json_instruction(
        ctx, allow_action=False, skip_emote=True
    )


def build_bg_flag_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Flag events — pickup, drop, capture."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    event_type = extra_data.get('event_type', '')
    flag_team = extra_data.get('flag_team', '')
    team = extra_data.get('team', '')

    score_a = int(
        extra_data.get('score_alliance', 0))
    score_h = int(
        extra_data.get('score_horde', 0))
    exact_score = int(
        extra_data.get('new_score', 0))

    carrier = extra_data.get('carrier_name', '')
    scorer = extra_data.get('scorer_name', '')
    dropper = extra_data.get('dropper_name', '')

    carrier_real = extra_data.get(
        'carrier_is_real_player', False)
    scorer_real = extra_data.get(
        'scorer_is_real_player', False)
    dropper_real = extra_data.get(
        'dropper_is_real_player', False)

    if 'picked_up' in event_type:
        if flag_team == team:
            ctx += (
                "\nThe enemy just picked up your flag."
            )
            if not is_rp:
                ctx += (
                    " React like a real BG player: a short "
                    "warning, complaint, or callout is enough."
                )
        else:
            if carrier:
                ctx += (
                    f"\n{carrier} picked up the enemy flag."
                )
            else:
                ctx += (
                    "\nYour team picked up the enemy flag."
                )

            if is_rp:
                if carrier and carrier_real:
                    ctx += (
                        f" Cheer {carrier} on by name."
                    )
                else:
                    ctx += " React positively."
            else:
                ctx += (
                    " A quick useful or casual reaction is "
                    "enough: acknowledge the pickup, mention "
                    "protecting the FC, or say nothing fancy."
                )

    elif 'dropped' in event_type:
        if flag_team == team:
            if dropper:
                ctx += (
                    f"\nThe enemy {dropper} dropped "
                    "your flag."
                )
            else:
                ctx += (
                    "\nThe enemy dropped your flag."
                )

            if is_rp:
                ctx += (
                    " Celebrate and urge someone to return it."
                )
            else:
                ctx += (
                    " React briefly like a real player. "
                    "Something like 'return flag', 'nice', "
                    "or a simple acknowledgement is enough."
                )

        else:
            if dropper:
                ctx += (
                    f"\n{dropper} dropped the enemy flag."
                )
            else:
                ctx += (
                    "\nYour team dropped the enemy flag."
                )

            if is_rp:
                if dropper and dropper_real:
                    ctx += (
                        f" Express concern for {dropper}."
                    )
                else:
                    ctx += (
                        " Urge someone to recover it."
                    )
            else:
                ctx += (
                    " React like a real BG player: a short "
                    "call to grab it, mild frustration, or "
                    "a low-effort reaction is fine."
                )

    elif 'captured' in event_type:
        if flag_team == team:
            enemy_score = (
                score_h if team == 'Alliance'
                else score_a
            )

            ctx += (
                f"\nYour team captured the flag. "
                f"CURRENT SCORE: Alliance {score_a}, "
                f"Horde {score_h}. "
                "Use these exact numbers if mentioning "
                "the score."
            )

            if scorer:
                ctx += (
                    f" {scorer} was the scorer."
                )

            if is_rp:
                if scorer and scorer_real:
                    ctx += (
                        f" Celebrate and praise "
                        f"{scorer} by name. "
                        f"Your team now has exactly "
                        f"{exact_score} captures while "
                        f"the enemy has {enemy_score}."
                    )
                else:
                    ctx += (
                        " Celebrate the capture."
                    )
            else:
                ctx += (
                    " React like a real player after a flag "
                    "cap. 'nice', 'gj', 'huge', a score "
                    "comment, or a brief reaction is enough. "
                    "Do not force praise or celebration."
                )

        else:
            ctx += (
                "\nThe enemy captured your flag. "
                "EXACT SCORE AFTER THIS CAPTURE: "
                f"Alliance {score_a}, Horde {score_h}. "
                f"The enemy now has exactly "
                f"{exact_score} captures. "
                "Do not invent a different number."
            )

            if is_rp:
                ctx += (
                    " React with frustration or renewed "
                    "determination."
                )
            else:
                ctx += (
                    " React like a real BG player: mild "
                    "frustration, a short complaint, 'rip', "
                    "or a practical comment is fine."
                )

    ctx += " Keep the reaction brief."

    return append_json_instruction(
        ctx,
        allow_action=False,
        skip_emote=True,
    )


def build_bg_flag_carrier_prompt(
    extra_data, bot_data, action
):
    """First-person message from the flag carrier."""
    ctx = _bg_base_context(
        extra_data,
        bot_data,
        skip_observation_constraint=True,
    )

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    if action == 'pickup':
        if is_rp:
            ctx += (
                "\nYOU just picked up the enemy flag! "
                "Say something in first person: call "
                "for protection, express urgency, or "
                "rally your team to cover you. "
                "One sentence as the flag carrier."
            )
        else:
            ctx += (
                "\nYou just picked up the enemy flag. "
                "Type something a real flag carrier might "
                "quickly say while moving. It can be as "
                "simple as 'got flag', 'cover me', "
                "'need heals', 'fc going tunnel', or "
                "another brief useful/casual message. "
                "Do not turn it into a heroic speech."
            )

    else:
        if is_rp:
            ctx += (
                "\nYOU just dropped the enemy flag after "
                "being overwhelmed. Give a brief first-person "
                "reaction: frustration, apology, or tell "
                "someone else to grab it."
            )
        else:
            ctx += (
                "\nYou just dropped the enemy flag. "
                "React like a real player: 'flag down', "
                "'grab it', 'mb', 'rip', a short complaint, "
                "or no-frills explanation is appropriate. "
                "Keep it very brief."
            )

    return append_json_instruction(
        ctx, allow_action=False
    )


def build_bg_flag_return_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Flag return reaction."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    flag_team = extra_data.get('flag_team', '')
    team = extra_data.get('team', '')
    returner = extra_data.get(
        'returner_name', '')
    returner_real = extra_data.get(
        'returner_is_real_player', False)

    if flag_team == team:
        if returner:
            ctx += (
                f"\n{returner} returned your team's "
                "flag to base."
            )
        else:
            ctx += (
                "\nYour flag was returned to base."
            )

        if is_rp:
            if returner and returner_real:
                ctx += (
                    f" Praise {returner} by name for "
                    "the clutch return."
                )
            else:
                ctx += " React positively."
        else:
            ctx += (
                " Give a brief natural BG reaction. "
                "'nice return', 'gj', 'nice', or even "
                "just moving on without fanfare is fine."
            )

    else:
        ctx += (
            "\nThe enemy returned their flag to base."
        )

        if is_rp:
            ctx += " Express frustration."
        else:
            ctx += (
                " React briefly if appropriate. Mild "
                "annoyance, 'rip', or a practical comment "
                "is enough."
            )

    return append_json_instruction(
        ctx,
        allow_action=False,
        skip_emote=True,
    )


def build_bg_node_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Node events — contest, capture."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    node_name = extra_data.get(
        'node_name', 'a node')
    new_owner = extra_data.get('new_owner', '')
    team = extra_data.get('team', '')
    event_type = extra_data.get('event_type', '')

    claimer = extra_data.get('claimer_name', '')
    claimer_real = extra_data.get(
        'claimer_is_real_player', False)

    score_a = int(
        extra_data.get('score_alliance', 0))
    score_h = int(
        extra_data.get('score_horde', 0))

    my_score = (
        score_a if team == 'Alliance'
        else score_h
    )
    enemy_score = (
        score_h if team == 'Alliance'
        else score_a
    )

    if 'contested' in event_type:
        if new_owner == team:
            if claimer:
                ctx += (
                    f"\n{claimer} is assaulting "
                    f"{node_name}."
                )
            else:
                ctx += (
                    f"\nYour team is assaulting "
                    f"{node_name}."
                )

            if is_rp:
                ctx += (
                    " React with attacking energy."
                )
            else:
                ctx += (
                    " Give a brief normal BG reaction. "
                    "It can be tactical, supportive, or "
                    "simply acknowledge the push."
                )

        else:
            ctx += (
                f"\n{node_name} is under enemy attack."
            )

            if is_rp:
                ctx += (
                    " Urgently rally defenders to stop "
                    "the assault."
                )
            else:
                ctx += (
                    " This is useful tactical information. "
                    "A realistic response might be "
                    f"'inc {node_name}', "
                    f"'def {node_name}', ask for help, "
                    "or give another short callout. "
                    "Do not make it a dramatic alarm."
                )

    elif 'captured' in event_type:
        if new_owner == team:
            if claimer:
                ctx += (
                    f"\n{claimer} captured "
                    f"{node_name} for your team."
                )
            else:
                ctx += (
                    f"\nYour team captured {node_name}."
                )

            if is_rp:
                if claimer and claimer_real:
                    ctx += (
                        f" Praise {claimer} by name."
                    )
                else:
                    ctx += " Celebrate."
            else:
                ctx += (
                    " React naturally if warranted: "
                    "'nice', 'gj', a tactical next step, "
                    "or a brief acknowledgement is enough."
                )

        else:
            ctx += (
                f"\nThe enemy captured {node_name}."
            )

            if is_rp:
                ctx += (
                    " React with frustration or call "
                    "to reclaim it."
                )
            else:
                ctx += (
                    " A brief complaint or tactical call "
                    "to take it back is appropriate."
                )

    if my_score > 0 or enemy_score > 0:
        diff = my_score - enemy_score

        if is_rp:
            if diff > 300:
                ctx += " Your team has a large lead."
            elif diff < -300:
                ctx += " Your team is far behind."
            elif (
                abs(diff) <= 100
                and (my_score + enemy_score) > 500
            ):
                ctx += " The match is very close."
        else:
            if diff > 300:
                ctx += (
                    " Your team currently has a large lead."
                )
            elif diff < -300:
                ctx += (
                    " Your team is currently far behind."
                )
            elif (
                abs(diff) <= 100
                and (my_score + enemy_score) > 500
            ):
                ctx += (
                    " The scores are currently close."
                )

    return append_json_instruction(
        ctx, allow_action=False
    )


def build_bg_pvp_kill_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """PvP kill — quick reaction."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    victim = extra_data.get(
        'victim_name', 'an enemy')

    victim_class_id = extra_data.get(
        'victim_class')
    victim_class = ''

    if victim_class_id is not None:
        victim_class = get_class_name(
            int(victim_class_id))

    killer = extra_data.get('killer_name', '')
    killer_real = extra_data.get(
        'killer_is_real_player', False)

    victim_info = (
        f"{victim} ({victim_class})"
        if victim_class
        else victim
    )

    if killer:
        ctx += (
            f"\n{killer} just killed {victim_info}."
        )
    else:
        ctx += (
            f"\nA teammate just killed {victim_info}."
        )

    if is_rp:
        if killer and killer_real:
            ctx += (
                f" Praise {killer} by name. "
                "Use a quick sharp battlefield reaction."
            )
        else:
            ctx += (
                " React with a quick sharp comment. "
                "Trash talk, tactical praise, dark humor, "
                "or a class-related taunt can work."
            )
    else:
        ctx += (
            " React only if this feels worth commenting on. "
            "A real player might say 'nice', 'lol', 'gj', "
            "'finally', mildly trash talk the victim, "
            "comment on the class, or barely react at all. "
            "Do not force praise, a joke, or a taunt."
        )

    return append_json_instruction(
        ctx, allow_action=False
    )


def build_bg_score_milestone_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Score milestone — tension, momentum."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    milestone_team = extra_data.get(
        'milestone_team', '')
    milestone_value = int(
        extra_data.get('milestone_value', 0))
    team = extra_data.get('team', '')

    score_a = int(
        extra_data.get('score_alliance', 0))
    score_h = int(
        extra_data.get('score_horde', 0))

    my_score = (
        score_a if team == 'Alliance'
        else score_h
    )
    enemy_score = (
        score_h if team == 'Alliance'
        else score_a
    )

    if milestone_team == team:
        ctx += (
            f"\nYour team just reached "
            f"{milestone_value} resources."
        )

        if milestone_value >= 1500:
            if is_rp:
                ctx += (
                    " Victory is close. Rally the team "
                    "to finish the battle."
                )
            else:
                ctx += (
                    " You're very close to winning. "
                    "React like a real player: 'almost', "
                    "'just hold', 'gg soon', a tactical "
                    "comment, or brief excitement."
                )

        elif my_score > enemy_score + 200:
            if is_rp:
                ctx += (
                    " Your team has strong momentum."
                )
            else:
                ctx += (
                    " Your team has a decent lead. "
                    "A low-key comment about holding the "
                    "lead or keeping pressure is enough."
                )

        else:
            ctx += (
                " React briefly to the current score."
            )

    else:
        ctx += (
            f"\nThe enemy just reached "
            f"{milestone_value} resources."
        )

        if milestone_value >= 1500:
            if is_rp:
                ctx += (
                    " They are close to victory. "
                    "React with desperate urgency."
                )
            else:
                ctx += (
                    " They're close to winning. A realistic "
                    "reaction could be 'we need caps now', "
                    "'rip', frustration, or a last tactical "
                    "call. Do not force panic."
                )

        elif enemy_score > my_score + 200:
            if is_rp:
                ctx += (
                    " They are pulling ahead. Rally "
                    "your team."
                )
            else:
                ctx += (
                    " They're pulling ahead. A brief "
                    "complaint or tactical suggestion is "
                    "enough."
                )

        else:
            ctx += (
                " React briefly to the current pressure."
            )

    return append_json_instruction(
        ctx, allow_action=False
    )


# -- Group-event BG prompt builders ------------------

def build_bg_achievement_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Achievement earned mid-battle."""
    ctx = _bg_base_context(extra_data, bot_data)
    achiever = extra_data.get(
        'achiever_name', 'someone')
    achievement = extra_data.get(
        'achievement_name', 'an achievement')
    ctx += (
        f"\n{achiever} just earned "
        f"[{achievement}] mid-battle! "
        "Quick, impressed reaction -- keep "
        "it short and battlefield-appropriate."
    )
    return append_json_instruction(
        ctx, allow_action=False)


def build_bg_spell_cast_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Spell cast reaction in BG context."""
    ctx = _bg_base_context(extra_data, bot_data)
    caster = extra_data.get(
        'caster_name', 'someone')
    spell = extra_data.get(
        'spell_name', 'a spell')
    target = extra_data.get(
        'target_name', 'someone')
    category = extra_data.get(
        'spell_category', 'spell')
    ctx += (
        f"\n{caster} cast {spell} on {target} "
        f"({category}). Brief tactical comment -- "
        "acknowledge the play, keep it snappy."
    )
    return append_json_instruction(
        ctx, allow_action=False)


def build_bg_low_health_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Low health callout in BG context."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    target = extra_data.get(
        'target_name', '')

    if target:
        ctx += (
            f"\n{target} is very low on health."
        )
    else:
        ctx += (
            "\nYou are very low on health."
        )

    if is_rp:
        ctx += (
            " Give a brief urgent reaction: panic, plea "
            "for healing, or defiant last stand."
        )
    else:
        ctx += (
            " Give a realistic short BG callout if useful: "
            "'heals?', 'im low', 'help', 'rip', or another "
            "brief reaction. Do not make it dramatic."
        )

    return append_json_instruction(
        ctx, allow_action=False
    )


def build_bg_oom_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Out of mana callout in BG context."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    ctx += "\nYou are out of mana mid-fight."

    if is_rp:
        ctx += (
            " Give a brief frustrated or urgent callout."
        )
    else:
        ctx += (
            " Type something a real player might actually "
            "say: 'oom', 'no mana', 'sec oom', a short "
            "complaint, or another very brief callout."
        )

    return append_json_instruction(
        ctx, allow_action=False
    )


def build_bg_death_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Teammate death reaction in BG context."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    dead = extra_data.get(
        'dead_name', 'a teammate')
    killer = extra_data.get('killer_name', '')

    if killer:
        ctx += (
            f"\n{dead} was just killed by {killer}."
        )
    else:
        ctx += (
            f"\n{dead} just died."
        )

    if is_rp:
        ctx += (
            " Give a brief urgent reaction: mourn them, "
            "swear revenge, or rally the team."
        )
    else:
        ctx += (
            " React like a real teammate in a BG, if worth "
            "commenting on. 'rip', 'ouch', 'lol', a short "
            "warning about the killer, mild frustration, "
            "or no-frills tactical information are fine. "
            "Do not mourn them or vow revenge."
        )

    return append_json_instruction(
        ctx, allow_action=False
    )

def build_bg_combat_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Combat reaction in BG context."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    creature = extra_data.get(
        'creature_name', 'enemies')
    is_boss = bool(int(
        extra_data.get('is_boss', 0)))

    if is_rp:
        if is_boss:
            ctx += (
                f"\nEngaging {creature}! Give a brief "
                "battle cry or taunt at a worthy foe."
            )
        else:
            ctx += (
                f"\nEngaging {creature}! Give a quick "
                "battle cry."
            )
    else:
        ctx += (
            f"\nCombat just started with {creature}. "
            "If you comment at all, make it something a "
            "real player could type mid-fight: a short "
            "callout, complaint, warning, target comment, "
            "or quick reaction. Do not force a battle cry "
            "or taunt."
        )

        if is_boss:
            ctx += (
                " This is an important battleground NPC, "
                "so a tactical comment is more likely than "
                "fantasy dialogue."
            )

    return append_json_instruction(
        ctx, allow_action=False
    )


# -- Idle chatter ------------------------------------

BG_IDLE_CATEGORIES_RP = [
    "battle humor or sarcasm about the match",
    "faction pride or a brief war cry",
    "tactical observation (score, team strength)",
    "combat fatigue or resource complaint",
    "class fantasy -- something your class "
    "would say in battle",
    "taunting the enemy faction",
    "morale boost or rallying cry",
    "lore reference about this battleground",
    "comment about a teammate's performance",
    "enemy team observation or grudging respect",
    "weather or environment comment with "
    "battle urgency",
    "dark humor about dying or respawning",
    "racial grudge against the enemy faction",
    "nostalgia for a past battle or victory",
    "complaint about the chaos of the fight",
    "admiring or cursing a specific enemy class",
    "spiritual or religious invocation "
    "(Light, Elune, ancestors, elements)",
    "impatience or eagerness for the next clash",
    "gallows humor when losing badly",
    "swagger or overconfidence when winning",
]
BG_IDLE_CATEGORIES = [
    "what the team should be doing right now",
    "where the enemy team seems to be going",
    "someone leaving a base undefended",
    "needing more people on defense",
    "too many people fighting in the wrong place",
    "the current score",
    "whether the match is close",
    "complaining about the other team",
    "complaining about your own team",
    "a teammate doing something useful",
    "someone making a questionable play",
    "getting repeatedly killed",
    "a healer being annoying to kill",
    "an enemy player who keeps showing up",
    "having no heals",
    "having great heals",
    "being low on mana",
    "waiting to resurrect",
    "getting stuck at the graveyard",
    "a flag carrier needing help",
    "wondering where the flag carrier is",
    "an incoming attack on a base or node",
    "asking who is defending",
    "asking where everyone is",
    "trying to figure out what the team is doing",
    "the enemy team being surprisingly good",
    "the enemy team being terrible",
    "the match being messy",
    "the match being boring for the moment",
    "the match being way closer than expected",
    "the match looking basically over",
    "a recent good play",
    "a recent bad play",
    "a frustrating death",
    "a lucky escape",
    "being tired of fighting at mid",
    "someone ignoring objectives",
    "wanting people to play the objective",
    "mild trash talk",
    "a quick joke about the match",
    "having nothing important to say",
]

def build_bg_idle_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Ambient idle chatter during a BG match."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    category_pool = (
        BG_IDLE_CATEGORIES_RP
        if is_rp
        else BG_IDLE_CATEGORIES
    )
    category = random.choice(category_pool)

    if is_rp:
        ctx += (
            f"\nThere's a lull in the action. Say "
            f"something to your team about: "
            f"{category}. "
            "Keep it natural and in-character. "
            "One sentence only."
        )
    else:
        ctx += (
            f"\nThere's a lull in the match. If you type "
            f"anything, make it a casual BG chat message "
            f"about: {category}. "
            "Keep it brief and low-effort. It does not "
            "need to be funny, tactical, or important."
        )

    return append_json_instruction(
        ctx, allow_action=False
    )


# -- BG arrival greeting ----------------------------

def build_bg_arrival_prompt(
    extra_data, bot_data, is_raid_worker=False
):
    """Reaction when entering a BG."""
    ctx = _bg_base_context(extra_data, bot_data)

    config = extra_data.get('_config') or {}
    is_rp = (
        get_chatter_mode(config) == 'roleplay'
    )

    if is_rp:
        ctx += (
            "\nYou just joined a battleground and "
            "the team is gathering before the fight. "
            "Say something team-oriented: a battle "
            "cry, faction pride, rallying your side, "
            "trash-talking the enemy, or hyping up "
            "the group. Focus on the TEAM, not any "
            "one player. One sentence only."
        )
    else:
        ctx += (
            "\nYou just entered the battleground before "
            "the fight starts. Say something a real player "
            "might casually type, if anything: hi, gl, a "
            "quick plan, a question about defense, mild "
            "trash talk, or some other brief BG comment. "
            "Do not force hype or a battle cry."
        )

    return append_json_instruction(
        ctx, allow_action=False
    )
