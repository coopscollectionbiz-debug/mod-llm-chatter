
import ast
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "chatter_group_prompts.py"
)


def get_function(name):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    matches = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_nearby_objects_flexible_only_in_normal():
    node = get_function(
        "build_nearby_object_conversation_prompt"
    )

    calls = [
        call for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id ==
        "append_conversation_json_instruction"
    ]

    assert len(calls) == 1

    keywords = {
        keyword.arg: keyword.value
        for keyword in calls[0].keywords
    }

    assert "flexible_count" in keywords
    assert ast.unparse(
        keywords["flexible_count"]
    ) == "not is_rp"

    participation = [
        item for item in ast.walk(node)
        if isinstance(item, ast.If)
        and ast.unparse(item.test) ==
        "is_rp and num_bots > 2"
    ]
    assert len(participation) == 1


def test_player_message_normal_response_contract():
    node = get_function(
        "build_player_msg_conversation_prompt"
    )

    normal_branches = [
        item for item in ast.walk(node)
        if isinstance(item, ast.If)
        and ast.unparse(item.test) == "not is_rp"
    ]

    assert len(normal_branches) >= 1

    branch = normal_branches[0]
    assert any(
        isinstance(item, ast.Return)
        for item in ast.walk(branch)
    )

    source = ast.unparse(branch)

    assert "Other speakers may remain silent" in source
    assert "The first message" in source
    assert "brief_casual" in source
    assert "EMOTE_LIST_STR" in source
    assert "item_context" in source
    assert "link_context" in source
    assert "chat_history" in source


def test_player_message_roleplay_path_retained():
    node = get_function(
        "build_player_msg_conversation_prompt"
    )

    normal_branch = next(
        item for item in node.body
        if isinstance(item, ast.If)
        and ast.unparse(item.test) == "not is_rp"
    )

    assert any(
        isinstance(item, ast.Return)
        for item in normal_branch.body
    )

    # The original Roleplay implementation still follows
    # the early Normal-mode return.
    assert any(
        isinstance(item, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "msg_count"
            for target in item.targets
        )
        for item in node.body
        if item.lineno > normal_branch.end_lineno
    )


def test_quest_conversations_preserve_event_states():
    cases = {
        "build_quest_complete_conversation_prompt": (
            "Quest turned in:",
            "Do not invent specific rewards",
        ),
        "build_quest_objectives_conversation_prompt": (
            "Quest objectives completed:",
            "NOT been turned in yet",
        ),
        "build_quest_accept_conversation_prompt": (
            "New quest accepted by",
            "objectives are not yet complete",
        ),
    }

    for name, expected in cases.items():
        node = get_function(name)

        branches = [
            item for item in node.body
            if isinstance(item, ast.If)
            and ast.unparse(item.test) == "not is_rp"
        ]
        assert len(branches) == 1

        normal = branches[0]
        text = ast.unparse(normal)

        for phrase in expected:
            assert phrase in text

        assert "flexible_count=True" in text
        assert "chat_history" in text
        assert "quest_details" in text
        assert "quest_objectives" in text
        assert "_append_bots_with_rp" in text

        assert any(
            isinstance(item, ast.Return)
            for item in normal.body
        )

        assert any(
            isinstance(item, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "count_word"
                for target in item.targets
            )
            for item in node.body
            if item.lineno > normal.end_lineno
        )


def test_bot_question_normal_mode_preserves_context():
    node = get_function("build_bot_question_prompt")

    normal_branches = [
        item for item in node.body
        if isinstance(item, ast.If)
        and ast.unparse(item.test) == "not is_rp"
    ]
    assert len(normal_branches) == 1

    normal = normal_branches[0]
    text = ast.unparse(normal)

    # Normal mode retains the human identity and
    # context without forcing a memory-based question.
    assert "build_player_prompt_header_from_dict" in text
    assert "build_player_chat_guidance" in text
    assert "sanitize_memory_for_prompt" in text
    assert "Memories are optional" in text
    assert "chat_history" in text
    assert "recent_messages" in text
    assert "speaker_talent_context" in text
    assert "target_talent_context" in text
    assert "append_json_instruction" in text

    assert any(
        isinstance(item, ast.Return)
        for item in normal.body
    )

    # Original Roleplay logic remains after the
    # Normal-mode branch.
    assert any(
        item.lineno > normal.end_lineno
        for item in node.body
    )
