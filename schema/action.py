"""The action a model may take on a page, as a fixed, deterministically-parsed shape.

Wire shape (exactly one of):
    {"action": "type",  "target": {"strategy": ..., "value": ...}, "text": ...}
    {"action": "click", "target": {"strategy": ..., "value": ...}}
    {"action": "done",  "reason": ...}

`strategy` is one of: "label" | "role_text" | "text_contains".
  * label         -> Playwright get_by_label(value)
  * role_text     -> get_by_role(role, name=name); value is "role:name"
                     (or a bare role, e.g. "textbox")
  * text_contains -> get_by_text(value)  (substring, case-insensitive)

The model produces this via a forced tool call against ACTION_TOOL, whose
input_schema is permissive at the top level (only `action` is required). The
per-variant rules -- target required for type/click, text for type, reason for
done, and no stray keys -- are enforced here by `parse_action`, not the schema.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Strategy = Literal["label", "role_text", "text_contains"]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Strategy
    value: str = Field(min_length=1)


class TypeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["type"]
    target: Target
    text: str


class ClickAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["click"]
    target: Target


class DoneAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["done"]
    reason: str = Field(min_length=1)


Action = Annotated[
    Union[TypeAction, ClickAction, DoneAction],
    Field(discriminator="action"),
]

_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def parse_action(data: dict) -> Action:
    """Validate a raw dict (e.g. a tool-call input) into a concrete Action.

    Raises pydantic.ValidationError if it does not match exactly one variant.
    """
    return _ADAPTER.validate_python(data)


# --- Anthropic tool definition ------------------------------------------------
# One tool, forced. Top-level schema is loose (an "any-of" by conditional
# fields is awkward under strict mode); parse_action is the real gate.

ACTION_TOOL: dict = {
    "name": "next_action",
    "description": (
        "Return the single next action to take on the current page. "
        "Use 'type' to enter text into a field, 'click' to activate a control "
        "(link, button, or button-like element), or 'done' when the goal is "
        "fully achieved and the confirmation is visible."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": ["type", "click", "done"],
            },
            "target": {
                "type": "object",
                "additionalProperties": False,
                "description": "Required for 'type' and 'click'. Omit for 'done'.",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "enum": ["label", "role_text", "text_contains"],
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "label: the field's visible label. "
                            "role_text: 'role:accessible name' (e.g. "
                            "'link:Open sub-account', 'button:Sign On') or a "
                            "bare role. text_contains: a substring of the "
                            "element's visible text."
                        ),
                    },
                },
                "required": ["strategy", "value"],
            },
            "text": {
                "type": "string",
                "description": "Required for 'type': the text to enter.",
            },
            "reason": {
                "type": "string",
                "description": "Required for 'done': why the goal is complete.",
            },
        },
        "required": ["action"],
    },
}
