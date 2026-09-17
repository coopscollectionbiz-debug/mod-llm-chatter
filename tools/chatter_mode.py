"""Shared chatter-mode helpers.

Normal mode uses stable player-style personality profiles.
Roleplay mode preserves the existing authored character traits.
"""

import hashlib


_NORMAL_PLAYER_STYLE_PROFILES = [
    (("friendly", "patient"), "warm and conversational"),
    (("quiet", "polite"), "brief and understated"),
    (("helpful", "practical"), "clear and matter-of-fact"),
    (("relaxed", "easygoing"), "casual and unhurried"),
    (("dry-humored", "observant"), "wry and concise"),
    (("chatty", "curious"), "friendly and engaged"),
    (("focused", "cooperative"), "direct but respectful"),
    (("experienced", "patient"), "calm and measured"),
    (("playful", "good-natured"), "lightly teasing but kind"),
    (("reserved", "considerate"), "soft-spoken and thoughtful"),
    (("competitive", "fair-minded"), "energetic without hostility"),
    (("methodical", "reliable"), "precise and composed"),
    (("newer", "open-minded"), "curious and unpretentious"),
    (("social", "encouraging"), "upbeat without overdoing it"),
    (("independent", "courteous"), "plain-spoken and self-contained"),
    (("blunt", "well-meaning"), "direct with occasional mild salt"),
    (("mature", "supportive"), "steady and reassuring"),
    (("casual", "adaptable"), "natural and low-key"),
    (("analytical", "calm"), "specific without lecturing"),
    (("self-deprecating", "friendly"), "dry and approachable"),
]

_NORMAL_PLAYER_EXTRA_TRAITS = [
    "attentive",
    "team-minded",
    "low-key",
    "curious",
    "straightforward",
    "good-humored",
    "steady",
    "flexible",
    "thoughtful",
    "game-focused",
]


def normalize_chatter_mode(mode):
    value = str(mode or "").strip().lower()
    return value if value in ("normal", "roleplay") else "normal"


def is_roleplay(mode):
    return normalize_chatter_mode(mode) == "roleplay"


def resolve_player_personality(
    name,
    traits=None,
    tone="",
    mode="normal",
):
    """Return RP metadata or a stable Normal-mode player profile."""
    if is_roleplay(mode):
        return [value for value in (traits or []) if value], tone or ""

    seed = str(name or "playerbot").strip().casefold().encode("utf-8")
    digest = hashlib.sha256(seed).digest()

    index = (
        int.from_bytes(digest[:4], "big")
        % len(_NORMAL_PLAYER_STYLE_PROFILES)
    )
    player_traits, player_tone = _NORMAL_PLAYER_STYLE_PROFILES[index]

    extra_index = (
        int.from_bytes(digest[4:8], "big")
        % len(_NORMAL_PLAYER_EXTRA_TRAITS)
    )

    return (
        list(player_traits)
        + [_NORMAL_PLAYER_EXTRA_TRAITS[extra_index]],
        player_tone,
    )
