
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1])
)
import chatter_group_prompts as prompts


BOT = {
    "name": "Testwarrior",
    "race": "Human",
    "class": "Warrior",
    "level": 60,
    "gender": "Male",
    "role": "tank",
}

TRAITS = ["laid-back", "helpful", "experienced"]


CASES = [
    (
        "kill",
        "build_kill_reaction_prompt",
        (BOT, TRAITS, "Ragnaros", True, False),
        ["Ragnaros", "boss"],
    ),
    (
        "loot",
        "build_loot_reaction_prompt",
        (BOT, TRAITS, "Ashkandi", 4),
        ["Ashkandi", "epic"],
    ),
    (
        "combat",
        "build_combat_reaction_prompt",
        (BOT, TRAITS, "Onyxia", True),
        ["Onyxia", "boss"],
    ),
    (
        "death",
        "build_death_reaction_prompt",
        (BOT, TRAITS, "Coopadin", "Onyxia"),
        ["Coopadin", "Onyxia"],
    ),
    (
        "resurrect",
        "build_resurrect_reaction_prompt",
        (BOT, TRAITS),
        ["resurrected"],
    ),
    (
        "wipe",
        "build_wipe_reaction_prompt",
        (BOT, TRAITS, "Ragnaros"),
        ["wiped", "Ragnaros"],
    ),
    (
        "corpse_run",
        "build_corpse_run_reaction_prompt",
        (BOT, TRAITS, "Blackrock Mountain"),
        ["corpse", "Blackrock Mountain"],
    ),
]


@pytest.mark.parametrize(
    "name,function_name,args,expected",
    CASES,
)
def test_normal_gameplay_reaction(
    name, function_name, args, expected
):
    function = getattr(prompts, function_name)

    with patch.object(
        prompts,
        "build_player_prompt_header_from_dict",
        return_value="IDENTITY: Testwarrior",
    ), patch.object(
        prompts,
        "build_player_chat_guidance",
        return_value="Speak like a real player.",
    ):
        result = function(
            *args,
            "normal",
            chat_history="Recent chat example",
        )

    assert isinstance(result, str), name

    for phrase in expected:
        assert phrase in result, (name, phrase)

    assert "IDENTITY: Testwarrior" in result
    assert "Recent chat example" in result
    assert "Speak like a real player." in result
    assert "Your tone:" not in result
    assert "Creative twist:" not in result


@pytest.mark.parametrize(
    "name,function_name,args,expected",
    CASES,
)
def test_roleplay_gameplay_reaction(
    name, function_name, args, expected
):
    function = getattr(prompts, function_name)

    with patch.object(
        prompts,
        "build_player_prompt_header_from_dict",
        return_value="ROLEPLAY IDENTITY",
    ), patch.object(
        prompts,
        "build_race_class_context",
        return_value="ROLEPLAY RACE CONTEXT",
    ), patch.object(
        prompts,
        "pick_random_tone",
        return_value="ROLEPLAY TONE",
    ), patch.object(
        prompts,
        "maybe_get_creative_twist",
        return_value=None,
    ):
        result = function(
            *args,
            "roleplay",
            chat_history="Recent roleplay chat",
        )

    assert isinstance(result, str), name
    assert "ROLEPLAY IDENTITY" in result
    assert "ROLEPLAY RACE CONTEXT" in result
    assert "Your tone: ROLEPLAY TONE" in result
    assert "Recent roleplay chat" in result

    for phrase in expected:
        assert phrase in result, (name, phrase)

    assert "Normal mode: human gameplay reaction" not in result
