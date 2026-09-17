import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from chatter_mode import resolve_player_personality
from chatter_group_prompts import _build_speaker_header
from chatter_shared import build_bot_state_context


def test_normal_mode_profile_is_stable_and_ignores_rp_traits():
    first = resolve_player_personality(
        "Arthaslol",
        ["hesitant", "mystical", "self-doubting"],
        "brooding",
        "normal",
    )
    second = resolve_player_personality(
        "Arthaslol",
        ["bold", "jovial", "reckless"],
        "loud",
        "normal",
    )

    assert first == second
    traits, tone = first
    assert "hesitant" not in traits
    assert "mystical" not in traits
    assert tone != "brooding"


def test_roleplay_preserves_existing_traits_and_tone():
    traits, tone = resolve_player_personality(
        "Arthaslol",
        ["hesitant", "mystical", "self-doubting"],
        "brooding",
        "roleplay",
    )

    assert traits == [
        "hesitant",
        "mystical",
        "self-doubting",
    ]
    assert tone == "brooding"


def test_normal_speaker_header_does_not_leak_rp_traits():
    bot = {
        "name": "Arthaslol",
        "class": "Mage",
        "race": "Human",
        "level": 24,
    }

    header = _build_speaker_header(
        bot,
        ["hesitant", "mystical", "self-doubting"],
        "normal",
        "brooding",
    )

    assert "real WoW player" in header
    assert "hesitant" not in header
    assert "mystical" not in header
    assert "brooding" not in header


def test_roleplay_speaker_header_preserves_identity():
    bot = {
        "name": "Arthaslol",
        "class": "Mage",
        "race": "Human",
        "level": 24,
    }

    header = _build_speaker_header(
        bot,
        ["hesitant", "mystical", "self-doubting"],
        "roleplay",
        "brooding",
    )

    assert "level 24 Human Mage" in header
    assert "hesitant" in header
    assert "mystical" in header
    assert "brooding" in header


def test_bot_state_keeps_hard_facts_and_allows_future_agency():
    state = {
        "identity": {
            "name": "Arthaslol",
            "level": 24,
            "race": "Human",
            "class": "Mage",
            "zone": "Stormwind City",
        },
    }

    context = build_bot_state_context(state)

    assert "level=24" in context
    assert "race=Human" in context
    assert "class=Mage" in context
    assert "zone=Stormwind City" in context
    assert "Do not contradict them." in context
    assert "plans, commit to actions" in context
    assert "maybe/might/probably" in context
