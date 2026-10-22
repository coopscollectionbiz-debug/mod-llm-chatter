"""Regression tests for quest acceptance prompt generation."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1])
)

import chatter_group_prompts as prompts


BOT = {
    "name": "Testhealer",
    "class": "Priest",
    "race": "Human",
    "level": 60,
}
TRAITS = ["quiet", "practical", "friendly"]


@pytest.mark.parametrize("mode", ["normal", "roleplay"])
def test_single_quest_acceptance(mode):
    with (
        patch.object(
            prompts,
            "build_player_prompt_header_from_dict",
            return_value="Player identity: Testhealer",
        ),
        patch.object(
            prompts,
            "build_player_chat_guidance",
            return_value="Use natural party chat.",
        ),
    ):
        result = prompts.build_quest_accept_reaction_prompt(
            BOT,
            TRAITS,
            "Coopadin",
            "The Great Masquerade",
            60,
            "Stormwind City",
            mode,
            chat_history="Recent chat example",
            quest_details="Investigate the situation.",
            quest_objectives="Speak to the contact.",
        )

    assert isinstance(result, str)
    assert "The Great Masquerade" in result
    assert "Stormwind City" in result
    assert "Recent chat example" in result

    if mode == "normal":
        assert "not been completed or turned in" in result
        assert "Speak to the contact" in result
        assert "Your tone:" not in result
        assert "Creative twist:" not in result


@pytest.mark.parametrize("mode", ["normal", "roleplay"])
def test_batch_quest_acceptance(mode):
    with (
        patch.object(
            prompts,
            "build_player_prompt_header_from_dict",
            return_value="Player identity: Testhealer",
        ),
        patch.object(
            prompts,
            "build_player_chat_guidance",
            return_value="Use natural party chat.",
        ),
    ):
        result = prompts.build_quest_accept_batch_prompt(
            BOT,
            TRAITS,
            "Coopadin",
            ["Quest Alpha", "Quest Beta"],
            "Stormwind City",
            mode,
            chat_history="Recent chat example",
        )

    assert isinstance(result, str)
    assert "Quest Alpha" in result
    assert "Quest Beta" in result
    assert "Stormwind City" in result
    assert "Recent chat example" in result

    if mode == "normal":
        assert "not completed" in result
        assert "Do not recite the quest list" in result
        assert "Your tone:" not in result
        assert "Creative twist:" not in result


def test_single_normal_invalid_quest_level():
    with (
        patch.object(
            prompts,
            "build_player_prompt_header_from_dict",
            return_value="Player identity: Testhealer",
        ),
        patch.object(
            prompts,
            "build_player_chat_guidance",
            return_value="Use natural party chat.",
        ),
    ):
        result = prompts.build_quest_accept_reaction_prompt(
            BOT,
            TRAITS,
            "Coopadin",
            "Unknown Quest",
            "unknown",
            "Stormwind City",
            "normal",
        )

    assert "Unknown Quest" in result
    assert "above your level" not in result
    assert "below your level" not in result
