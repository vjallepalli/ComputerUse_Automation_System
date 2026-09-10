"""ask_claude with the Anthropic client mocked -- no real API calls."""

import types
from unittest.mock import MagicMock

import pytest

from agent.decide import AgentActionError, ask_claude
from schema.action import ClickAction, DoneAction, TypeAction


def _tool_response(tool_input: dict):
    block = types.SimpleNamespace(
        type="tool_use", name="next_action", input=tool_input, id="tu_1"
    )
    return types.SimpleNamespace(content=[block], stop_reason="tool_use")


def _client_returning(response):
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def _call_kwargs(client) -> dict:
    return client.messages.create.call_args.kwargs


# --- happy path: one test per action type -----------------------------------


def test_parses_type_action():
    client = _client_returning(_tool_response(
        {"action": "type",
         "target": {"strategy": "label", "value": "Member number"},
         "text": "10003"}
    ))
    action = ask_claude("look up member 10003", "<dom/>", [], client=client)
    assert isinstance(action, TypeAction)
    assert action.text == "10003"
    assert action.target.strategy == "label"


def test_parses_click_action():
    client = _client_returning(_tool_response(
        {"action": "click",
         "target": {"strategy": "role_text", "value": "link:Open sub-account"}}
    ))
    action = ask_claude("open the sub-account", "<dom/>", [], client=client)
    assert isinstance(action, ClickAction)
    assert action.target.value == "link:Open sub-account"


def test_parses_done_action():
    client = _client_returning(_tool_response(
        {"action": "done", "reason": "confirmation page shows the new account number"}
    ))
    action = ask_claude("confirm the sub-account", "<dom/>", [], client=client)
    assert isinstance(action, DoneAction)
    assert "confirmation" in action.reason


# --- request shape ---------------------------------------------------------


def test_tool_choice_is_forced_to_next_action():
    client = _client_returning(_tool_response({"action": "done", "reason": "x"}))
    ask_claude("goal", "<dom/>", [], client=client)
    kwargs = _call_kwargs(client)
    assert kwargs["tool_choice"] == {"type": "tool", "name": "next_action"}
    assert any(t["name"] == "next_action" for t in kwargs["tools"])


def test_history_is_included_in_the_request():
    client = _client_returning(_tool_response({"action": "done", "reason": "x"}))
    history = [
        {"status": "ok", "action": "type", "value": "Member number", "text": "10003"},
        {"status": "ok", "action": "click", "value": "Retrieve"},
    ]
    ask_claude("goal", "<dom/>", history, client=client)
    sent = _call_kwargs(client)["messages"][0]["content"]
    assert "10003" in sent
    assert "Retrieve" in sent
    assert "goal" in sent


def test_model_comes_from_agent_model_env(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-test-not-real")
    client = _client_returning(_tool_response({"action": "done", "reason": "x"}))
    ask_claude("goal", "<dom/>", [], client=client)
    assert _call_kwargs(client)["model"] == "claude-test-not-real"


# --- failure modes are typed and catchable -------------------------------


def test_malformed_tool_input_raises_agent_action_error():
    client = _client_returning(_tool_response({"action": "type"}))  # no target/text
    with pytest.raises(AgentActionError):
        ask_claude("goal", "<dom/>", [], client=client)


def test_unknown_strategy_raises_agent_action_error():
    client = _client_returning(_tool_response(
        {"action": "click", "target": {"strategy": "xpath", "value": "//a"}}
    ))
    with pytest.raises(AgentActionError):
        ask_claude("goal", "<dom/>", [], client=client)


def test_no_tool_call_in_response_raises_agent_action_error():
    text_only = types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text="I cannot help")],
        stop_reason="end_turn",
    )
    with pytest.raises(AgentActionError):
        ask_claude("goal", "<dom/>", [], client=_client_returning(text_only))
