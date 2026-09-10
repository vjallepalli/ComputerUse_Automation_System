"""Actions: translate an Action into the one Playwright call it names.

`execute_on_page(page, action)` is deterministic and model-free -- the same
surface the discovery agent drives and, later, replay. It resolves the target
with exactly the strategy the action names, insists the locator matches exactly
one element (zero or many -> SelectorResolutionError), then performs the
type / click. `done` is a no-op that just reports.

Anything past resolution (e.g. filling a match that isn't editable) is left to
Playwright's own error -- those messages are already specific.
"""

from __future__ import annotations

from schema.action import Action, Target


class SelectorResolutionError(RuntimeError):
    """A target locator matched zero, or more than one, element."""


def execute_on_page(page, action: Action) -> dict:
    if action.action == "done":
        return {"status": "done", "action": "done", "reason": action.reason}

    locator = _locator_for(page, action.target)
    count = locator.count()
    if count != 1:
        raise SelectorResolutionError(_resolution_message(action.target, count, locator))

    if action.action == "type":
        locator.fill(action.text)
        return {
            "status": "ok", "action": "type",
            "strategy": action.target.strategy, "value": action.target.value,
            "text": action.text, "resolved_count": 1,
        }

    locator.click()
    return {
        "status": "ok", "action": "click",
        "strategy": action.target.strategy, "value": action.target.value,
        "resolved_count": 1,
    }


def resolve_locator(page, strategy: str, value: str):
    """Strategy + value -> a Playwright Locator. Shared by the executor (acting)
    and replay (success-condition checks + output extraction), so the strategies
    resolve identically in both.

    `text_of` and `next_cell` are extraction-only strategies:
      * `text_of`   -- locates exactly like `text_contains`; the caller reads the
                       matched element's full text.
      * `next_cell` -- locates a table cell (role=cell, i.e. a <td>) whose trimmed
                       text equals `value` exactly, then returns that cell's
                       immediate next-sibling <td>. Raises SelectorResolutionError
                       on zero matches, more than one match (ambiguous), or a
                       matched cell with no following <td> -- never a silent empty.
    """
    if strategy == "label":
        return page.get_by_label(value)
    if strategy in ("text_contains", "text_of"):
        return page.get_by_text(value)
    if strategy == "next_cell":
        return _next_cell_locator(page, value)
    if strategy == "role_text":
        role, _, name = value.partition(":")
        role = role.strip()
        name = name.strip()
        if not role:
            raise SelectorResolutionError(
                f"role_text value {value!r} has no role; use 'role:name' or a bare role"
            )
        return page.get_by_role(role, name=name) if name else page.get_by_role(role)
    raise SelectorResolutionError(f"unknown strategy {strategy!r}")


def _next_cell_locator(page, value: str):
    """The <td> immediately after the cell whose trimmed text is exactly `value`.

    Exact match (not substring): the label cell in this UI is exactly "Savings" /
    "Checking" / etc., and an exact match makes the ambiguity check meaningful --
    two accounts of the same type is a real "which one?" that must not be
    silently resolved to the first.
    """
    cells = page.get_by_role("cell", name=value, exact=True)
    count = cells.count()
    if count == 0:
        raise SelectorResolutionError(f"next_cell target {value!r} matched no table cell")
    if count > 1:
        raise SelectorResolutionError(
            f"next_cell target {value!r} matched {count} cells; need exactly 1"
        )
    following = cells.locator("xpath=following-sibling::td[1]")
    if following.count() == 0:
        raise SelectorResolutionError(
            f"next_cell: the cell matching {value!r} has no next <td> sibling"
        )
    return following


def _locator_for(page, target: Target):
    return resolve_locator(page, target.strategy, target.value)


def _resolution_message(target: Target, count: int, locator) -> str:
    head = (
        f"{target.strategy}={target.value!r} resolved to {count} elements; "
        "need exactly 1"
    )
    if count == 0:
        return head
    samples = []
    for i in range(min(count, 3)):
        try:
            html = locator.nth(i).evaluate("e => e.outerHTML")
        except Exception:  # pragma: no cover - best-effort diagnostics only
            html = "<unavailable>"
        samples.append(html[:200])
    return head + "\n  " + "\n  ".join(samples)
