"""Behavioral tests for stable Normal-mode player personalities."""

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from chatter_mode import resolve_player_personality
from chatter_prompts import (
    build_event_statement_prompt,
    build_event_conversation_prompt,
    build_plain_conversation_prompt,
)

NORMAL = {"LLMChatter.ChatterMode": "normal"}
ROLEPLAY = {"LLMChatter.ChatterMode": "roleplay"}

BOTS = [
    {
        "name": "Aliss",
        "race": "Human",
        "class": "Mage",
        "level": 32,
        "zone": "Elwynn Forest",
    },
    {
        "name": "Grimblade",
        "race": "Orc",
        "class": "Warrior",
        "level": 32,
        "zone": "Elwynn Forest",
    },
]

EVENT_BOT = {
    "bot1_name": "Aliss",
    "bot1_race": "Human",
    "bot1_class": "Mage",
    "bot1_level": 32,
}


def test_personality_is_stable_for_same_name():
    first = resolve_player_personality("Aliss", mode="normal")
    second = resolve_player_personality("Aliss", mode="normal")
    third = resolve_player_personality("ALISS", mode="normal")

    assert first == second == third
    assert first[0]
    assert first[1]


def test_normal_event_statement_uses_stable_personality():
    prompt = build_event_statement_prompt(
        EVENT_BOT,
        event_context="The Darkmoon Faire has arrived.",
        event_type="holiday_darkmoon",
        config=NORMAL,
        allow_action=False,
    )

    traits, tone = resolve_player_personality(
        "Aliss", mode="normal"
    )

    assert "Your usual personality:" in prompt
    assert ", ".join(traits) in prompt
    assert f"Your usual tone: {tone}" in prompt
    assert "Your current mood:" not in prompt


def test_normal_event_personality_survives_context_change():
    contexts = [
        ("The Darkmoon Faire has arrived.", "holiday_darkmoon"),
        ("A zeppelin has arrived.", "transport"),
        ("Players are gathering in town.", "world"),
    ]

    traits, tone = resolve_player_personality(
        "Aliss", mode="normal"
    )

    for context, event_type in contexts:
        prompt = build_event_statement_prompt(
            EVENT_BOT,
            event_context=context,
            event_type=event_type,
            config=NORMAL,
            allow_action=False,
        )
        assert ", ".join(traits) in prompt
        assert f"Your usual tone: {tone}" in prompt


def test_normal_event_conversation_has_individual_personalities():
    prompt = build_event_conversation_prompt(
        BOTS,
        event_context="A world event has started.",
        config=NORMAL,
        allow_action=False,
    )

    for bot in BOTS:
        traits, tone = resolve_player_personality(
            bot["name"], mode="normal"
        )
        assert f"{bot['name']} personality:" in prompt
        assert ", ".join(traits) in prompt
        assert f"usual tone: {tone}" in prompt

    assert "MESSAGE LENGTH GUIDANCE:" in prompt
    assert "MOOD AND LENGTH SEQUENCE" not in prompt
    assert "mood=" not in prompt


def test_normal_plain_conversation_has_no_forced_mood():
    prompt = build_plain_conversation_prompt(
        BOTS,
        config=NORMAL,
        allow_action=False,
    )

    for bot in BOTS:
        traits, tone = resolve_player_personality(
            bot["name"], mode="normal"
        )
        assert f"{bot['name']} personality:" in prompt
        assert ", ".join(traits) in prompt
        assert f"usual tone: {tone}" in prompt

    assert "mood=" not in prompt


def test_roleplay_event_statement_preserves_mood():
    prompt = build_event_statement_prompt(
        EVENT_BOT,
        event_context="The Darkmoon Faire has arrived.",
        event_type="holiday_darkmoon",
        config=ROLEPLAY,
        allow_action=False,
    )

    assert "Your current mood:" in prompt
    assert "Your usual personality:" not in prompt


def test_roleplay_event_conversation_preserves_mood_sequence():
    prompt = build_event_conversation_prompt(
        BOTS,
        event_context="A world event has started.",
        config=ROLEPLAY,
        allow_action=False,
    )

    assert "MOOD AND LENGTH SEQUENCE" in prompt
    assert "mood=" in prompt
    assert "MESSAGE LENGTH GUIDANCE:" not in prompt

# EXTENDED GAMEPLAY PERSONALITY TESTS

# EXTENDED GAMEPLAY PERSONALITY TESTS

import re
import pytest
import chatter_prompts as prompts


QUEST = {
    "quest_name": "The People's Militia",
    "description": "Defeat Defias enemies in Westfall.",
}

ITEM = {
    "item_name": "Sword of Testing",
    "item_quality": 3,
    "item_count": 1,
    "sell_price": 2500,
}

SPELL = {
    "spell_name": "Fireball",
    "spell_desc": "Launches a fiery ball at an enemy.",
}

TARGET = {
    "name": "Hogger",
    "entry": 448,
    "type": "creature",
}

GAMEPLAY_BOT = {
    **BOTS[0],
    "bot1_name": BOTS[0]["name"],
    "bot1_race": BOTS[0]["race"],
    "bot1_class": BOTS[0]["class"],
    "bot1_level": BOTS[0]["level"],
}


def _statement_cases():
    return [
        ("plain", lambda mode: prompts.build_plain_statement_prompt(
            GAMEPLAY_BOT, config=mode, allow_action=False
        )),
        ("quest", lambda mode: prompts.build_quest_statement_prompt(
            GAMEPLAY_BOT, QUEST, config=mode, allow_action=False
        )),
        ("loot", lambda mode: prompts.build_loot_statement_prompt(
            GAMEPLAY_BOT, ITEM, True,
            config=mode, allow_action=False
        )),
        ("gossip", lambda mode: prompts.build_gossip_statement_prompt(
            GAMEPLAY_BOT, TARGET, "creature", 12,
            config=mode, allow_action=False
        )),
        ("event", lambda mode: prompts.build_event_statement_prompt(
            GAMEPLAY_BOT, "A zeppelin has arrived.",
            config=mode, allow_action=False
        )),
        ("spell", lambda mode: prompts.build_spell_statement_prompt(
            GAMEPLAY_BOT, SPELL, config=mode, allow_action=False
        )),
        ("trade", lambda mode: prompts.build_trade_statement_prompt(
            GAMEPLAY_BOT, ITEM, config=mode, allow_action=False
        )),
    ]


def _conversation_cases():
    return [
        ("plain", lambda mode: prompts.build_plain_conversation_prompt(
            BOTS, config=mode, allow_action=False
        )),
        ("quest", lambda mode: prompts.build_quest_conversation_prompt(
            BOTS, QUEST, config=mode, allow_action=False
        )),
        ("gossip", lambda mode: prompts.build_gossip_conversation_prompt(
            BOTS, TARGET, "creature", 12,
            config=mode, allow_action=False
        )),
        ("event", lambda mode: prompts.build_event_conversation_prompt(
            BOTS, "A zeppelin has arrived.",
            config=mode, allow_action=False
        )),
        ("spell", lambda mode: prompts.build_spell_conversation_prompt(
            BOTS, SPELL, config=mode, allow_action=False
        )),
        ("trade", lambda mode: prompts.build_trade_conversation_prompt(
            BOTS, ITEM, config=mode, allow_action=False
        )),
    ]


@pytest.mark.parametrize("name,build", _statement_cases())
def test_all_normal_statements_use_stable_personality(name, build):
    prompt = build(NORMAL)
    traits, tone = resolve_player_personality(
        GAMEPLAY_BOT["name"], mode="normal"
    )

    assert ", ".join(traits) in prompt, name
    assert tone in prompt, name
    assert "Creative twist:" not in prompt, name
    assert "Reaction style:" not in prompt, name


@pytest.mark.parametrize("name,build", _conversation_cases())
def test_all_normal_conversations_keep_speaker_identity(name, build):
    prompt = build(NORMAL)

    for bot in BOTS:
        traits, tone = resolve_player_personality(
            bot["name"], mode="normal"
        )
        assert f"{bot['name']} personality:" in prompt, name
        assert ", ".join(traits) in prompt, name
        assert f"usual tone: {tone}" in prompt, name

    assert "mood=" not in prompt, name
    assert "Creative twist:" not in prompt, name


@pytest.mark.parametrize("name,build", _conversation_cases())
def test_normal_conversations_use_flexible_message_lengths(name, build):
    prompt = build(NORMAL)

    assert "Do not assign turns or message lengths to speakers" in prompt
    assert not re.search(
        r"Message\s+\d+\s+\([^)]+\):\s*length=",
        prompt,
    )


@pytest.mark.parametrize("name,build", _conversation_cases())
def test_roleplay_conversations_keep_mood_sequence(name, build):
    prompt = build(ROLEPLAY)

    assert "MOOD AND LENGTH SEQUENCE" in prompt, name
    assert "mood=" in prompt, name
    assert "MESSAGE LENGTH GUIDANCE:" not in prompt, name


def test_normal_transport_does_not_force_creativity():
    prompt = prompts.build_event_statement_prompt(
        GAMEPLAY_BOT,
        "A zeppelin has arrived.",
        config=NORMAL,
        allow_action=False,
    )

    assert "Do not force excitement" in prompt
    assert "Be creative and original" not in prompt


def test_roleplay_transport_keeps_original_instruction():
    prompt = prompts.build_event_statement_prompt(
        GAMEPLAY_BOT,
        "A zeppelin has arrived.",
        config=ROLEPLAY,
        allow_action=False,
    )

    assert "Be creative and original" in prompt
