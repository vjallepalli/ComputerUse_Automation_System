"""Recorder: distil a successful discovery run into a Capability artifact.

    python -m agent.record --run artifacts/runs/<run-id> \\
        --capability-id lookup-savings-balance --name "Look up savings balance" \\
        --description "..." --output-name savings_balance \\
        --output-strategy text_contains --output-target "Savings"

Reads `<run-dir>/meta.json` + `<run-dir>/steps.jsonl` and produces a
`schema.capability.Capability`, written to
`artifacts/capabilities/<capability_id>-v<version>.json`.

Only a successful run (meta `exit_code` 0) can be recorded -- a failed or
incomplete transcript is refused.

Two things the recorder derives mechanically:
  * steps -- carried over with the selector targets exactly as executed;
  * parameters -- a 'type' step whose text appears verbatim in the run goal is a
    per-invocation input. It becomes a Parameter named from the field's label
    (snake_cased, e.g. "Member number" -> member_number) with the recorded value
    as its example, and the step's value_template becomes "{that_name}". A typed
    value that is NOT in the goal is a fixed UI constant and stays literal.

One thing it does NOT derive: which text on the final page is "the answer".
Inferring that from the model's freeform `done` reason is unreliable, so output
targeting (output_name / output_strategy / output_target) is passed in by a
human who has reviewed the run. That is the review gate the brief calls for:
the recorder proposes structure, a person confirms what the capability returns.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from schema.action import Target
from schema.capability import (
    INITIAL_CAPABILITY_VERSION,
    Capability,
    CapabilityStep,
    Extraction,
    OutputSpec,
    Parameter,
    SuccessCondition,
)

EXTRACTION_STRATEGIES = ("label", "role_text", "text_contains", "text_of", "next_cell")


class RecorderError(RuntimeError):
    """This run cannot be recorded as a capability."""


def record_capability(
    run_dir,
    *,
    output_name: str,
    output_strategy: str,
    output_target: str,
    capability_id: str,
    name: str,
    description: str,
    success_condition: SuccessCondition | None = None,
) -> Capability:
    run_dir = Path(run_dir)
    meta = _load_meta(run_dir)
    raw_steps = _load_steps(run_dir)

    if meta.get("exit_code") != 0:
        raise RecorderError(
            f"run {meta.get('run_id', run_dir.name)!r} did not complete successfully "
            f"(exit_code={meta.get('exit_code')!r}, "
            f"stop_reason={meta.get('stop_reason')!r}); only a successful run "
            f"(exit_code 0, ending in a 'done' action) can be recorded"
        )
    if output_strategy not in EXTRACTION_STRATEGIES:
        raise RecorderError(
            f"output_strategy {output_strategy!r} must be one of {EXTRACTION_STRATEGIES}"
        )

    goal = meta.get("goal", "")
    parameters: dict[str, Parameter] = {}
    steps: list[CapabilityStep] = []

    for raw in raw_steps:
        decision = raw.get("decision")
        if not decision:
            continue
        action = decision.get("action")
        if action == "done":
            continue
        if action not in ("type", "click"):
            raise RecorderError(
                f"step {raw.get('step')}: cannot record action {action!r}"
            )

        target = Target(**decision["target"])  # verbatim, as executed

        if action == "click":
            steps.append(CapabilityStep(
                step_number=len(steps) + 1, action="click", target=target,
            ))
            continue

        text = decision.get("text", "")
        if text and text in goal:
            pname = _parameter_name(target, fallback_index=len(parameters) + 1)
            existing = parameters.get(pname)
            if existing is not None and existing.example != text:
                # Two `type` steps derive the SAME parameter name (their targets
                # snake_case to the same identifier) but recorded DIFFERENT
                # values. `setdefault` would keep the first and silently bind the
                # later step to it, dropping a distinct input. Refuse rather than
                # emit a capability that cannot reproduce the run; a human can
                # rename a field or hand-edit the artifact. Regression:
                # tests/agent/test_record_edge.py.
                raise RecorderError(
                    f"step {raw.get('step')}: parameter name {pname!r} already "
                    f"bound to {existing.example!r} but this step typed {text!r} "
                    f"-- two different inputs collapse to one name; disambiguate "
                    f"the field labels or record this value as a literal"
                )
            parameters.setdefault(pname, Parameter(
                name=pname,
                type=_infer_type(text),
                description=f"Value entered into the {target.value!r} field.",
                example=text,
            ))
            value_template = "{" + pname + "}"
        else:
            value_template = text

        steps.append(CapabilityStep(
            step_number=len(steps) + 1, action="type", target=target,
            value_template=value_template,
        ))

    if not steps:
        raise RecorderError("run has no type/click steps to record")

    outputs = [OutputSpec(
        name=output_name,
        type="str",
        description=(
            f"Read from the final page via {output_strategy} {output_target!r}. "
            f"Type defaults to 'str' -- refine on review."
        ),
        extraction=Extraction(strategy=output_strategy, target=output_target),
    )]

    if success_condition is None:
        success_condition = SuccessCondition(
            strategy=output_strategy,
            target=output_target,
            description=(
                f"The output selector ({output_strategy} {output_target!r}) must "
                f"resolve on the final page: if the value can be read, the flow "
                f"reached its end state."
            ),
        )

    return Capability(
        capability_id=capability_id,
        version=INITIAL_CAPABILITY_VERSION,
        # Recording a capability once does NOT make it trusted for unattended
        # replay. It ships as a draft; a human clears it via `python -m
        # agent.approve`. Stated explicitly (not left to the schema default) so a
        # future default change can't silently promote fresh recordings.
        # Regression: tests/agent/test_record.py::test_recorder_always_emits_draft.
        status="draft",
        name=name,
        description=description,
        created_from_run=meta.get("run_id", run_dir.name),
        target_app=_base_url(meta.get("start_url", "")),
        parameters=list(parameters.values()),
        steps=steps,
        outputs=outputs,
        success_condition=success_condition,
    )


def write_capability(capability: Capability, out_dir="artifacts/capabilities") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{capability.capability_id}-v{capability.version}.json"
    path.write_text(capability.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


# --- helpers --------------------------------------------------------------


def _load_meta(run_dir: Path) -> dict:
    path = run_dir / "meta.json"
    if not path.is_file():
        raise RecorderError(f"no meta.json in {run_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_steps(run_dir: Path) -> list[dict]:
    path = run_dir / "steps.jsonl"
    if not path.is_file():
        raise RecorderError(f"no steps.jsonl in {run_dir}")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _infer_type(value: str) -> str:
    v = value.strip()
    if v.lower() in ("true", "false"):
        return "bool"
    if re.fullmatch(r"-?\d+", v):
        return "int"
    if re.fullmatch(r"-?\d*\.\d+", v):
        return "float"
    return "str"


def _snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _parameter_name(target: Target, *, fallback_index: int) -> str:
    raw = target.value.split(":", 1)[-1] if target.strategy == "role_text" else target.value
    return _snake(raw) or f"param_{fallback_index}"


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return url


# --- CLI ----------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.record",
        description="Record a successful discovery run as a Capability artifact.",
    )
    parser.add_argument("--run", required=True, help="path to artifacts/runs/<run-id>/")
    parser.add_argument("--capability-id", required=True, help="kebab-case slug")
    parser.add_argument("--name", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--output-name", required=True, help="snake_case identifier")
    parser.add_argument("--output-strategy", required=True, choices=EXTRACTION_STRATEGIES)
    parser.add_argument("--output-target", required=True,
                        help="label text / 'role:name' / visible-text substring")
    parser.add_argument("--out-dir", default="artifacts/capabilities")
    args = parser.parse_args(argv)

    try:
        capability = record_capability(
            Path(args.run),
            output_name=args.output_name,
            output_strategy=args.output_strategy,
            output_target=args.output_target,
            capability_id=args.capability_id,
            name=args.name,
            description=args.description,
        )
    except RecorderError as exc:
        print(f"cannot record capability: {exc}", file=sys.stderr)
        return 1

    path = write_capability(capability, args.out_dir)
    print(
        f"wrote {path}  "
        f"({len(capability.parameters)} parameter(s), {len(capability.steps)} step(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
