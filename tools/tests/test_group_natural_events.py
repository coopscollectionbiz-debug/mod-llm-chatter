
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chatter_group_prompts as p


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
        "build_levelup_reaction_prompt",
        (BOT, TRAITS, "Coopadin", 60, False),
        ["Coopadin", "60"],
    ),
    (
        "build_quest_complete_reaction_prompt",
        (BOT, TRAITS, "Coopadin", "The Great Masquerade"),
        ["The Great Masquerade", "turned in"],
    ),
    (
        "build_quest_objectives_reaction_prompt",
        (BOT, TRAITS, "The Great Masquerade", "Coopadin"),
        ["The Great Masquerade", "NOT been turned in"],
    ),
    (
        "build_achievement_reaction_prompt",
        (BOT, TRAITS, "Coopadin", "Explore Eastern Kingdoms", False),
        ["Coopadin", "Explore Eastern Kingdoms"],
    ),
    (
        "build_group_achievement_reaction_prompt",
        (BOT, TRAITS, ["Coopadin", "Testwarrior"], "Leeeeeeeeeeeeeroy!"),
        ["Coopadin", "Testwarrior", "Leeeeeeeeeeeeeroy!"],
    ),
]


@pytest.mark.parametrize("function_name,args,expected", CASES)
def test_normal_event_reaction(function_name, args, expected):
    function = getattr(p, function_name)

    with patch.object(
        p,
        "build_player_prompt_header_from_dict",
        return_value="IDENTITY: Testwarrior",
    ), patch.object(
        p,
        "build_player_chat_guidance",
        return_value="Speak like a real player.",
    ):
        result = function(
            *args,
            "normal",
            chat_history="Recent party chat",
            speaker_talent_context="Protection Warrior",
        )

    assert "IDENTITY: Testwarrior" in result
    assert "Speak like a real player." in result
    assert "Recent party chat" in result
    assert "Protection Warrior" in result
    assert "Your tone:" not in result
    assert "Creative twist:" not in result

    for phrase in expected:
        assert phrase in result, (function_name, phrase)


@pytest.mark.parametrize("function_name,args,expected", CASES)
def test_roleplay_event_reaction(function_name, args, expected):
    function = getattr(p, function_name)

    with patch.object(
        p,
        "build_player_prompt_header_from_dict",
        return_value="ROLEPLAY IDENTITY",
    ), patch.object(
        p,
        "build_race_class_context",
        return_value="ROLEPLAY RACE CONTEXT",
    ), patch.object(
        p,
        "pick_random_tone",
        return_value="ROLEPLAY TONE",
    ), patch.object(
        p,
        "maybe_get_creative_twist",
        return_value=None,
    ):
        result = function(
            *args,
            "roleplay",
            chat_history="Recent roleplay chat",
        )

    assert "ROLEPLAY IDENTITY" in result
    assert "ROLEPLAY RACE CONTEXT" in result
    assert "Your tone: ROLEPLAY TONE" in result
    assert "Recent roleplay chat" in result

    # Verify the event's actual subject, not Normal-specific wording.
    event_subject = {
        "build_levelup_reaction_prompt": "Coopadin",
        "build_quest_complete_reaction_prompt": "The Great Masquerade",
        "build_quest_objectives_reaction_prompt": "The Great Masquerade",
        "build_achievement_reaction_prompt": "Explore Eastern Kingdoms",
        "build_group_achievement_reaction_prompt": "Leeeeeeeeeeeeeroy!",
    }[function_name]
    assert event_subject in result
