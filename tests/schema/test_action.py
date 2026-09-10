"""The action shape parses deterministically: exactly one variant, or it raises."""

import pytest
from pydantic import ValidationError

from schema.action import (
    ACTION_TOOL,
    ClickAction,
    DoneAction,
    TypeAction,
    parse_action,
)


def test_parses_type_action():
    a = parse_action(
        {"action": "type",
         "target": {"strategy": "label", "value": "Member number"},
         "text": "10001"}
    )
    assert isinstance(a, TypeAction)
    assert a.target.strategy == "label"
    assert a.text == "10001"


def test_parses_click_action():
    a = parse_action(
        {"action": "click",
         "target": {"strategy": "role_text", "value": "link:Open sub-account"}}
    )
    assert isinstance(a, ClickAction)
    assert a.target.value == "link:Open sub-account"


def test_parses_done_action():
    a = parse_action({"action": "done", "reason": "confirmation page shown"})
    assert isinstance(a, DoneAction)
    assert a.reason == "confirmation page shown"


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "type", "target": {"strategy": "label", "value": "x"}},   # no text
        {"action": "click"},                                                 # no target
        {"action": "done"},                                                  # no reason
        {"action": "wave", "target": {"strategy": "label", "value": "x"}},    # bad verb
        {"action": "click",
         "target": {"strategy": "xpath", "value": "//a"}},                    # bad strategy
        {"action": "done", "reason": "ok",
         "target": {"strategy": "label", "value": "x"}},                      # stray key
        {"action": "click",
         "target": {"strategy": "label", "value": ""}},                       # empty value
    ],
)
def test_rejects_malformed(bad):
    with pytest.raises(ValidationError):
        parse_action(bad)


def test_action_tool_schema_shape():
    schema = ACTION_TOOL["input_schema"]
    assert ACTION_TOOL["name"] == "next_action"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["action"]
    assert schema["properties"]["action"]["enum"] == ["type", "click", "done"]
    assert schema["properties"]["target"]["properties"]["strategy"]["enum"] == [
        "label", "role_text", "text_contains",
    ]
