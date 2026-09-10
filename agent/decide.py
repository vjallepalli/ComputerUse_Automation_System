"""The `decide` step of the discovery loop: ask Claude for the next Action.

`ask_claude(goal, dom, history)` sends the goal, the current cleaned DOM, and a
one-line-per-step history to the Anthropic API, forcing the `next_action` tool
(schema/action.py) so the reply is a single structured action. The tool input is
run through `parse_action`; anything that does not validate becomes an
`AgentActionError` -- a typed, catchable failure the orchestrator can decide what
to do with, never a raw ValidationError or an uncaught crash.

Model comes from `AGENT_MODEL` (see .env.example); only the discovery run needs
an API key. No retry logic here -- that is the loop's call to make.
"""

from __future__ import annotations

import json
import os

import anthropic
from pydantic import ValidationError

from schema.action import ACTION_TOOL, Action, parse_action

DEFAULT_MODEL = "claude-sonnet-5"
MODEL_ENV_VAR = "AGENT_MODEL"
_MAX_TOKENS = 1024


def resolve_model() -> str:
    """The model id for the discovery agent: AGENT_MODEL, else the pinned default."""
    return os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL

SYSTEM_PROMPT = """\
You are the decision step of a computer-use agent driving a legacy bank
back-office web UI through a fixed action interface. Each turn you get a GOAL,
the current page as cleaned DOM text, and a summary of the steps already taken.
Reply with exactly one next action by calling the `next_action` tool.

GOAL
- The goal is supplied fresh in every request. It is never hardcoded or
  remembered between runs -- act only on the goal in the current request.

REASONING
- Reason only from the cleaned DOM text in this request. Do not assume any
  field, button, link, value, or page that is not present in it.
- Use the steps-so-far summary to know where you are in the flow and to avoid
  repeating an action that already succeeded.

ACTIONS
- type: enter text into a field -- provide `target` and `text`.
- click: activate a link, button, or button-like control -- provide `target`.
- done: the goal is complete -- provide `reason`.

TARGET STRATEGIES -- these three are the ONLY ones that exist. Never invent a
selector or a CSS/XPath expression or any other strategy.
- label: the field's visible label text, e.g. "Member number".
- role_text: "role:accessible name", e.g. "link:Open sub-account" or
  "button:Sign On"; a bare role is also allowed.
- text_contains: a substring of the element's visible text, e.g. "Retrieve".

WHEN TO EMIT done
- Emit done only when the current DOM actually shows the information or result
  the goal asked for -- e.g. the confirmation page with the expected values is
  visible right now.
- Do NOT emit done just because the sequence of steps feels finished, or
  because you clicked a final button on the previous turn. If the confirming
  page is not in the current DOM, pick the next action that moves toward it.
"""


class AgentActionError(RuntimeError):
    """Claude's reply could not be turned into a valid Action.

    Raised for a malformed / missing tool call or a tool input that fails
    parse_action. Catchable by the orchestrator (retry, abort, ...); this
    function itself does not retry.
    """


def ask_claude(
    goal: str,
    dom: str,
    history: list,
    *,
    client: anthropic.Anthropic | None = None,
) -> Action:
    client = client or anthropic.Anthropic()

    response = client.messages.create(
        model=resolve_model(),
        max_tokens=_MAX_TOKENS,
        thinking={"type": "disabled"},  # deterministic; forced tool_choice needs it off
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_message(goal, dom, history)}],
        tools=[ACTION_TOOL],
        tool_choice={"type": "tool", "name": ACTION_TOOL["name"]},
    )

    tool_input = _extract_tool_input(response)
    try:
        return parse_action(tool_input)
    except ValidationError as exc:
        raise AgentActionError(
            f"model proposed an action that failed validation: {tool_input!r}\n{exc}"
        ) from exc


def _user_message(goal: str, dom: str, history: list) -> str:
    return (
        f"GOAL:\n{goal}\n\n"
        f"STEPS SO FAR:\n{_render_history(history)}\n\n"
        f"CURRENT PAGE (cleaned DOM):\n{dom}"
    )


def _render_history(history: list) -> str:
    if not history:
        return "(none yet)"
    return "\n".join(f"- {_summarize_step(step)}" for step in history)


def _summarize_step(step) -> str:
    """One line per prior step -- no DOM snapshots, keep the context small.

    For a `type` step the wording states the field is now populated, so the model
    does not read it as still-empty and retype it.
    """
    if isinstance(step, str):
        return step
    if isinstance(step, dict):
        note = f"  [{step['note']}]" if step.get("note") else ""
        if step.get("error"):
            return f"error: {step['error']}"
        kind = step.get("action")
        if kind == "type":
            return (
                f"typed {step.get('text')!r} into {step.get('value')!r} "
                f"(that field now contains {step.get('text')!r} -- do not re-type it)"
                f"{note}"
            )
        if kind == "click":
            return f"clicked {step.get('value')!r}{note}"
        if kind == "done" or step.get("status") == "done":
            return f"done: {step.get('reason', '')}".strip()
        if step.get("status") == "resumed":
            return (
                f"a human handled this step; agent did not act "
                f"({step.get('note', 'resumed')})"
            )
        return ", ".join(f"{k}={v!r}" for k, v in step.items())
    for attr in ("action",):  # tolerate an Action-like object
        if hasattr(step, attr):
            return repr(step)
    return repr(step)


def _extract_tool_input(response) -> dict:
    block = next(
        (
            b for b in getattr(response, "content", [])
            if getattr(b, "type", None) == "tool_use"
            and getattr(b, "name", None) == ACTION_TOOL["name"]
        ),
        None,
    )
    if block is None:
        raise AgentActionError(
            "model returned no next_action tool call "
            f"(stop_reason={getattr(response, 'stop_reason', None)!r})"
        )
    raw = block.input
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentActionError(f"tool input was not valid JSON: {raw!r}") from exc
    if not isinstance(raw, dict):
        raise AgentActionError(f"tool input was not an object: {raw!r}")
    return raw
