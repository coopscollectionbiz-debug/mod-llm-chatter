"""Regression coverage for raid downtime prompts."""

import ast
from pathlib import Path

import pytest


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "chatter_raid_prompts.py"
)


@pytest.mark.parametrize(
    "function_name",
    [
        "build_raid_banter_prompt",
        "build_raid_morale_prompt",
    ],
)
def test_normal_raid_prompt_has_no_forced_topic(function_name):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))

    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == function_name
    )

    branch = next(
        node for node in function.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "roleplay"
    )

    assert branch.body, "Roleplay branch must remain"
    assert branch.orelse, "Normal branch must exist"

    normal_text = ast.unparse(
        ast.Module(body=branch.orelse, type_ignores=[])
    )

    roleplay_text = ast.unparse(
        ast.Module(body=branch.body, type_ignores=[])
    )

    assert "random.choice" not in normal_text
    assert "Topic hint:" not in normal_text
    assert "random.choice" in roleplay_text
    assert "Topic hint:" in roleplay_text
    assert "No asterisks." in normal_text
