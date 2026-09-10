"""Capability artifact: a typed, versioned, reviewable capability distilled from
one successful discovery run.

The raw run transcript (`artifacts/runs/<run-id>/steps.jsonl`) is a recording.
A Capability is what an AI agent actually *invokes*: declared typed
`parameters`, an ordered list of `steps` carrying the selector targets exactly
as they were executed, typed `outputs` saying what the capability returns and
where each value is read from the final page, and a `success_condition` that
must hold for an invocation to count as successful.

Versioning (CLAUDE.md: "schema version + capability version"):
  * `schema_version` -- the artifact *shape*. Bumped when these models change;
    replay refuses a shape it does not know how to run.
  * `version` -- the capability's own semver, starting at "0.1.0", bumped when a
    human edits the artifact.

Every field has a description so the JSON is reviewable without reading code.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schema.action import Target  # reuse: strategy (label|role_text|text_contains) + value

SCHEMA_VERSION = "1.0"
INITIAL_CAPABILITY_VERSION = "0.1.0"

#: Scalar parameter / output types. Kept deliberately small.
ScalarType = Literal["str", "int", "float", "bool"]

#: Where-to-read strategies: the three action strategies, plus two read-only ones:
#:   * text_of   -- full visible text of the element located by text_contains(target)
#:   * next_cell -- text of the <td> immediately after the table cell whose trimmed
#:                  text is exactly `target` (label -> adjacent value, for flat rows
#:                  like <tr><td>Savings</td><td>812.55</td></tr>)
ExtractionStrategy = Literal[
    "label", "role_text", "text_contains", "text_of", "next_cell"
]

_SEMVER = r"^\d+\.\d+\.\d+$"
_SLUG = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_IDENT = r"^[a-z_][a-z0-9_]*$"
_PARAM_REF_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class Parameter(BaseModel):
    """One typed input the caller supplies when invoking the capability."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        pattern=_IDENT,
        description="snake_case identifier; referenced from a step's value_template as {name}.",
    )
    type: ScalarType = Field(description="Scalar type the caller must supply.")
    description: str = Field(description="What this parameter means to the caller.")
    example: str = Field(
        description="The value seen in the recording run -- documentation and test "
        "data, not a default.",
    )


class CapabilityStep(BaseModel):
    """One UI action, replayed deterministically with no model in the loop."""

    model_config = ConfigDict(extra="forbid")

    step_number: int = Field(
        ge=1,
        description="1-based position in the capability. The discovery run's "
        "terminating 'done' step is not included.",
    )
    action: Literal["type", "click"] = Field(
        description="'type' enters text into a field; 'click' activates a control.",
    )
    target: Target = Field(
        description="Selector strategy + value exactly as executed in the discovery "
        "run (carried over verbatim, never re-derived).",
    )
    value_template: Optional[str] = Field(
        default=None,
        description="'type' steps only: either a literal string, or '{param_name}' "
        "referencing a declared Parameter. Must be null for 'click'.",
    )

    @model_validator(mode="after")
    def _value_template_matches_action(self) -> "CapabilityStep":
        if self.action == "type" and not self.value_template:
            raise ValueError("a 'type' step requires a value_template")
        if self.action == "click" and self.value_template is not None:
            raise ValueError("a 'click' step must not have a value_template")
        return self

    def referenced_parameters(self) -> list[str]:
        """Parameter names referenced as {name} in value_template (may be empty)."""
        if self.action != "type" or self.value_template is None:
            return []
        return _PARAM_REF_RE.findall(self.value_template)


class Extraction(BaseModel):
    """How replay reads one value off the final page."""

    model_config = ConfigDict(extra="forbid")

    strategy: ExtractionStrategy = Field(
        description="Read strategy: label / role_text / text_contains, 'text_of' "
        "(full text of the text_contains match), or 'next_cell' (text of the <td> "
        "after the cell whose exact text is target).",
    )
    target: str = Field(
        min_length=1,
        description="Argument for the strategy: label text, 'role:name', a "
        "visible-text substring, or (for next_cell) the exact text of the label cell.",
    )


class OutputSpec(BaseModel):
    """One typed value the capability returns to its caller."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_IDENT, description="snake_case identifier for the value.")
    type: ScalarType = Field(
        description="Scalar type of the extracted value. Defaults to 'str' at record "
        "time; refine on review.",
    )
    description: str = Field(description="What this output represents.")
    extraction: Extraction = Field(description="Where on the final page it is read from.")


class SuccessCondition(BaseModel):
    """A check against the final page that must hold for an invocation to count
    as successful. By default it reuses the output's own selector: if the value
    can be located, the flow reached the intended end state."""

    model_config = ConfigDict(extra="forbid")

    strategy: ExtractionStrategy = Field(description="Locate strategy for the check.")
    target: str = Field(min_length=1, description="Argument for the strategy.")
    description: str = Field(description="Plain-language statement of what must be true.")


class Capability(BaseModel):
    """A reusable, invokable capability recorded from one successful discovery run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Artifact shape version. Replay refuses a shape it cannot run.",
    )
    capability_id: str = Field(
        pattern=_SLUG,
        description="Stable kebab-case slug identifying this capability.",
    )
    version: str = Field(
        default=INITIAL_CAPABILITY_VERSION,
        pattern=_SEMVER,
        description="Capability semver. Starts at 0.1.0; bump when the artifact is edited.",
    )
    name: str = Field(description="Short human-readable name.")
    description: str = Field(description="What the capability does and when to use it.")
    created_from_run: str = Field(
        description="The run_id this was recorded from -- a reference to "
        "artifacts/runs/<run_id>/, not the transcript itself.",
    )
    target_app: str = Field(description="Base URL the steps run against.")
    parameters: list[Parameter] = Field(
        description="Typed inputs the caller supplies at invocation time.",
    )
    steps: list[CapabilityStep] = Field(
        description="Ordered UI actions replay performs, model-free.",
    )
    outputs: list[OutputSpec] = Field(
        description="Typed values the capability returns to the caller.",
    )
    success_condition: SuccessCondition = Field(
        description="Final-page check that must hold for the invocation to be a success.",
    )

    @model_validator(mode="after")
    def _referenced_params_are_declared(self) -> "Capability":
        declared = {p.name for p in self.parameters}
        for step in self.steps:
            missing = set(step.referenced_parameters()) - declared
            if missing:
                raise ValueError(
                    f"step {step.step_number} references undeclared parameter(s): "
                    f"{sorted(missing)}"
                )
        return self
