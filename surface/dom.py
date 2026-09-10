"""Perception: turn a live page into a compact DOM the model can reason over.

`get_cleaned_dom(page)` is the Surface-level entry point (takes a Playwright
Page); it returns a `CleanedDom(html, synthetic_labels)`. `clean_html(str)` is
the pure transform underneath it, so the cleaner can be tested against real
target_app markup with no browser in the loop.

Per-turn order for a caller that will act on the page afterwards:
    dom = get_cleaned_dom(page)
    apply_synthetic_labels(page, dom.synthetic_labels)   # push labels onto live DOM
    ...                                                   # send dom.html to the model
    execute_on_page(page, action)                         # "label" now resolves live

What survives the clean:
  * document structure and all visible text
  * every interactive element (a / input / button / select / textarea / option)
    with the attributes needed to locate and describe it
  * tables (this UI lays everything out in nested tables) -- but stripped of
    presentational attributes and emptied spacer cells

What gets removed: <script>/<style>/<head>, comments, inline styles and every
other presentational attribute, event handlers, and non-semantic wrappers
(font/center/b/i/span/div with nothing left on them).

Synthetic labels: this UI labels fields only by an adjacent table cell
(`<td>Amount</td><td><input></td>`), so `get_by_label` and friends resolve to
nothing. The cleaner closes that gap upstream -- a field with no real label /
for / aria-label gets an `aria-label` synthesised from the nearest preceding
`<td>` with text, deduplicated with a positional suffix so two "Amount" fields
become "Amount (1)" / "Amount (2)" rather than a fresh ambiguity. Fields with a
real label are never touched; fields with no adjacent text are left alone (still
unresolvable -- correctly so).

`_synthesize_labels` is the single source of truth: it mutates the parsed tree
*and* returns the `{selector, match_index, label}` pairs it applied, so the same
labels can be pushed onto the live page with `apply_synthetic_labels` without
re-deriving anything.

Live field values: `page.content()` serialises the stale HTML `value` ATTRIBUTE
of `<input>` (etc.), NOT the live DOM `value` PROPERTY. After Playwright's
`.fill()` the property updates but the attribute usually still reads empty, so
the model would see every field it just filled as blank and retype it forever.
`get_cleaned_dom` fixes this: it reads every field's current value in ONE
`page.evaluate()` and overlays those onto the parsed tree before serialising, so
the string the model reads always matches what is actually on screen. The pure
`clean_html(str)` path has no live page and does not do this.

Sensitive masking (runs in `_clean`, so it applies to BOTH `clean_html` and
`get_cleaned_dom` -- discovery, replay, anywhere): a real SSN / Tax ID / etc.
must NEVER reach the model, displayed OR typed-in. Masking hides a real secret
-- it must NOT fabricate the appearance of one: an EMPTY sensitive field stays
visibly empty (`value=""`), or the model reads it as already-filled and skips
the human hand-off. Two independent passes, using guardrail.policy's ONE shared
keyword list + SSN pattern:
  1. label-adjacency -- a table cell whose IMMEDIATE preceding sibling cell is a
     sensitive label: its display text, or a NON-EMPTY value of any field it
     holds (incl. a live value just overlaid), becomes "[MASKED]". The static
     "NNN-NN-NNNN" format-hint cell (two past the label) is deliberately NOT
     masked -- no real data, useful context.
  2. SSN-shape regex over the final serialised string -- an NNN-NN-NNNN run of
     digits anywhere (sentence, attribute, a spot pass 1 missed) -> "[MASKED-SSN]".
This ONLY changes what `get_cleaned_dom` returns to the model. It does NOT touch
`execute_on_page`, which still types the real value -- required when a human
supplies it directly for a guardrail-blocked sensitive field.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment, NavigableString

# ONE shared sensitive keyword list + SSN pattern (guardrail.policy owns them).
from guardrail.policy import SENSITIVE_FIELD_KEYWORDS, SSN_SHAPE_RE

_log = logging.getLogger(__name__)

_MASK = "[MASKED]"
_MASK_SSN = "[MASKED-SSN]"

_DROP_TAGS = {"script", "style", "noscript", "template", "head", "svg"}

# Wrappers that carry no meaning once their presentational attrs are gone.
_UNWRAP_TAGS = {
    "font", "center", "b", "i", "u", "em", "strong", "small", "big",
    "strike", "s", "tt", "span", "div",
}

# Attributes worth keeping, by tag. Everything else (style, class, bgcolor,
# width, align, on*, ...) is dropped.
_KEEP_ATTRS_GLOBAL = {"id", "role", "aria-label", "aria-labelledby", "title", "alt"}
_KEEP_ATTRS_BY_TAG = {
    "a": {"href"},
    "input": {"type", "name", "value", "placeholder", "checked", "disabled",
              "readonly", "required"},
    "button": {"type", "name", "value", "disabled"},
    "select": {"name", "disabled", "required", "multiple"},
    "textarea": {"name", "placeholder", "disabled", "required"},
    "option": {"value", "selected"},
    "label": {"for"},
    "form": {"action", "method"},
    "img": {"alt"},
}

_INTERACTIVE = {"a", "input", "button", "select", "textarea", "option", "label"}

# Fields that want a label. <input type=submit|button|reset|image|hidden> carry
# their own accessible name (the value), so they're excluded.
_LABELABLE = {"input", "select", "textarea"}
_UNLABELABLE_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}

# Form fields whose live value is overlaid onto the tree before serialising.
_FIELD_TAGS = ("input", "select", "textarea")

# One round trip: read every field's current value off the live DOM.
_READ_FIELD_VALUES_JS = (
    "() => Array.from(document.querySelectorAll('input, select, textarea'))"
    ".map(el => ({"
    "  tag: el.tagName.toLowerCase(),"
    "  type: (el.getAttribute('type') || '').toLowerCase(),"
    "  key: el.id || el.getAttribute('name') || '',"
    "  value: el.value == null ? '' : String(el.value)"
    "}))"
)

# Structural containers that are safe to delete once they hold nothing.
_PRUNE_IF_EMPTY = {"td", "th", "tr", "table", "tbody", "thead", "tfoot",
                   "p", "ul", "ol", "li", "form"}


@dataclass(frozen=True)
class CleanedDom:
    """The model's view of a page plus the label pushes needed to act on it live."""

    html: str
    synthetic_labels: list[dict]


def get_cleaned_dom(page) -> CleanedDom:
    """Cleaned DOM for the page as it stands right now, plus its synthetic labels.

    Overlays every form field's LIVE value (one `page.evaluate`) so the model
    never sees a field it just filled as blank. Call
    `apply_synthetic_labels(page, result.synthetic_labels)` before acting on the
    page, so a "label" action resolves against the same names the model saw.
    """
    html, pairs = _clean(page.content(), live_field_values=read_field_values(page))
    return CleanedDom(html=html, synthetic_labels=pairs)


def read_field_values(page):
    """`[{tag, type, key, value}]` for every input/select/textarea, live. None on
    failure (fake page, detached frame). Used by `get_cleaned_dom` (the value
    overlay) and by escalation handoff (to tell whether a human typed into a
    field while they had control -- the masked DOM can't reveal that)."""
    try:
        return page.evaluate(_READ_FIELD_VALUES_JS)
    except Exception:  # pragma: no cover - best effort
        return None


def clean_html(html: str) -> str:
    """Just the cleaned string (no live page, so no value overlay)."""
    return _clean(html)[0]


def _clean(html: str, *, live_field_values=None) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()

    # Sync stale value attributes to the live DOM before anything else reads them
    # (a sensitive field's live value is masked here, not overlaid).
    _apply_live_field_values(soup, live_field_values)

    # Before attrs are stripped -- synthesis reads for=/id=/aria-* off the raw tree.
    synthetic_labels = _synthesize_labels(soup)

    # Pass 1: mask sensitive values by label adjacency (display cells + fields).
    _mask_sensitive_cells(soup)

    for tag in soup.find_all(True):
        _filter_attrs(tag)

    for tag in soup.find_all(_UNWRAP_TAGS):
        if not tag.attrs:
            tag.unwrap()

    _normalize_whitespace(soup)
    _prune_empty(soup)

    root = soup.body or soup
    text = root.decode_contents() if root is soup.body else str(root)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    # Pass 2: independent SSN-shape sweep over the final string (catches anything
    # pass 1 could not reach -- inside a sentence, an attribute, wherever).
    text = SSN_SHAPE_RE.sub(_MASK_SSN, text)
    return text.strip(), synthetic_labels


def _apply_live_field_values(soup, live) -> None:
    """Overlay live field values onto the parsed tree.

    `live` is `_read_live_field_values()`'s output. Fields appear in the same
    document order in the serialised HTML and in `querySelectorAll`, so match
    positionally; fall back to id/name keys if the two lists ever disagree
    (parser divergence on malformed markup).
    """
    if not live:
        return
    fields = soup.find_all(_FIELD_TAGS)
    if len(fields) == len(live):
        pairs = zip(fields, live)
    else:
        by_key = {e["key"]: e for e in live if e.get("key")}
        pairs = [
            (el, by_key[el.get("id") or el.get("name") or ""])
            for el in fields
            if (el.get("id") or el.get("name") or "") in by_key
        ]
    for el, entry in pairs:
        value = entry.get("value", "") or ""
        # A sensitive field's live value must never reach the model, even one a
        # human just typed in -- BUT only when it actually holds something. An
        # EMPTY sensitive field must stay visibly empty, or the model reads it as
        # already-filled and skips it (defeating the guardrail block on it).
        # (_mask_sensitive_cells covers the adjacent-<td> case; this also covers a
        # field labelled by aria-label / a real <label> instead.)
        if value.strip() and _is_sensitive_text(_field_label_text(el)):
            value = _MASK
        _set_field_value(el, value)


def _set_field_value(el, value: str) -> None:
    if el.name == "input":
        if el.get("type", "text").strip().lower() in ("checkbox", "radio"):
            return  # `.value` isn't the checked state; leave these alone
        el["value"] = value
    elif el.name == "textarea":
        el.clear()
        if value:
            el.append(value)
    elif el.name == "select":
        for opt in el.find_all("option"):
            if opt.get("value", opt.get_text(strip=True)) == value:
                opt["selected"] = "selected"
            elif opt.has_attr("selected"):
                del opt["selected"]


# --- sensitive-value masking (what the MODEL sees; never affects typing) ------


def _is_sensitive_text(text: str) -> bool:
    lowered = (text or "").lower()
    return any(kw in lowered for kw in SENSITIVE_FIELD_KEYWORDS)


def _field_label_text(el) -> str:
    """Best-effort label for a field: aria-label, a <label for=id>, a wrapping
    <label>, or the nearest preceding table cell (the adjacency this UI uses)."""
    parts = [el.get("aria-label", "")]
    fid = el.get("id")
    if fid:
        lbl = el.find_parent().find("label", attrs={"for": fid}) if el.parent else None
        lbl = lbl or (el.find_previous("label", attrs={"for": fid}))
        if lbl:
            parts.append(lbl.get_text(" ", strip=True))
    wrap = el.find_parent("label")
    if wrap:
        parts.append(wrap.get_text(" ", strip=True))
    parts.append(_preceding_cell_text(el))
    return " ".join(p for p in parts if p)


def _mask_sensitive_cells(soup) -> None:
    """Pass 1: a table cell whose IMMEDIATE preceding sibling cell is a sensitive
    label -- mask its display text, or a NON-EMPTY value of any field it holds.

    Only non-empty: masking hides a real secret, it must never fabricate the
    appearance of one -- an empty sensitive field stays visibly empty so the
    model still stops for a human to fill it.

    Immediate sibling only (not "nearest cell with text"): the row is
    [label][field][format-hint], so the field cell is masked but the static
    "NNN-NN-NNNN" hint cell -- which holds no real data and is useful context --
    is deliberately left alone.
    """
    for cell in soup.find_all(("td", "th")):
        prev = cell.find_previous_sibling(("td", "th"))
        if prev is None or not _is_sensitive_text(prev.get_text(" ", strip=True)):
            continue
        fields = cell.find_all(_FIELD_TAGS)
        if fields:
            for field in fields:
                if _field_current_value(field).strip():
                    _set_field_value(field, _MASK)
        elif cell.get_text(strip=True):
            cell.clear()
            cell.append(_MASK)


def _field_current_value(field) -> str:
    if field.name == "textarea":
        return field.get_text()
    if field.name == "select":
        chosen = field.find("option", selected=True)
        return (chosen.get("value", chosen.get_text(strip=True)) if chosen else "")
    return field.get("value", "")  # input


def apply_synthetic_labels(page, pairs: list[dict]) -> list[str]:
    """Push `{selector, match_index, label}` pairs onto the live page as aria-label.

    Returns the selectors that matched nothing live (page moved on since the DOM
    was cleaned); those are warned, not raised.
    """
    if not pairs:
        return []
    missing = page.evaluate(
        """(pairs) => {
            const missing = [];
            for (const p of pairs) {
                let els;
                try { els = document.querySelectorAll(p.selector); }
                catch (e) { missing.push(p.selector + ' (invalid selector)'); continue; }
                const el = els[p.match_index || 0];
                if (el) { el.setAttribute('aria-label', p.label); }
                else { missing.push(p.selector + '[' + (p.match_index || 0) + ']'); }
            }
            return missing;
        }""",
        pairs,
    )
    for sel in missing:
        _log.warning("apply_synthetic_labels: no live element for %s", sel)
    return missing


def _filter_attrs(tag) -> None:
    allowed = _KEEP_ATTRS_GLOBAL | _KEEP_ATTRS_BY_TAG.get(tag.name, set())
    for name in list(tag.attrs):
        if name not in allowed:
            del tag[name]


def _synthesize_labels(soup) -> list[dict]:
    """Give unlabelled fields an aria-label from their adjacent cell text.

    Mutates `soup` and returns one `{selector, match_index, label}` dict per field
    it labelled -- the same pairs `apply_synthetic_labels` replays onto the live
    page. `selector` + `match_index` address the field on the *original* markup
    (id, else `tag[name="..."]`, else the tag with a document-order index).
    """
    referenced_ids = {
        lbl["for"] for lbl in soup.find_all("label") if lbl.get("for")
    }

    pending = []  # (element, base_label_text) in document order
    for el in soup.find_all(_LABELABLE):
        if not _needs_synthetic_label(el, referenced_ids):
            continue
        text = _preceding_cell_text(el)
        if text:
            pending.append((el, text))

    counts = Counter(text for _, text in pending)
    seen = Counter()
    applied = []
    for el, text in pending:
        if counts[text] > 1:
            seen[text] += 1
            label = f"{text} ({seen[text]})"
        else:
            label = text
        el["aria-label"] = label
        selector, match_index = _stable_selector(el, soup)
        applied.append(
            {"selector": selector, "match_index": match_index, "label": label}
        )
    return applied


def _stable_selector(el, soup) -> tuple[str, int]:
    if el.get("id"):
        return f"#{el['id']}", 0
    name = el.get("name")
    if name:
        return f'{el.name}[name="{name}"]', 0
    same_tag = soup.find_all(el.name)
    return el.name, same_tag.index(el)


def _needs_synthetic_label(el, referenced_ids: set) -> bool:
    if el.has_attr("aria-label") or el.has_attr("aria-labelledby"):
        return False
    if el.get("id") and el["id"] in referenced_ids:
        return False
    if el.find_parent("label") is not None:
        return False
    if el.name == "input":
        if el.get("type", "text").strip().lower() in _UNLABELABLE_INPUT_TYPES:
            return False
    return True


def _preceding_cell_text(el) -> str:
    cell = el.find_parent("td")
    if cell is None:
        return ""
    for sibling in cell.find_previous_siblings("td"):
        text = sibling.get_text(" ", strip=True)
        if text:
            return re.sub(r"\s+", " ", text)
    return ""


def _normalize_whitespace(soup) -> None:
    for node in list(soup.find_all(string=True)):
        if node.parent.name in ("pre", "textarea"):
            continue
        collapsed = re.sub(r"\s+", " ", str(node))
        if collapsed.strip():
            node.replace_with(collapsed)
        else:
            node.extract()


def _prune_empty(soup) -> None:
    changed = True
    while changed:
        changed = False
        for tag in soup.find_all(_PRUNE_IF_EMPTY):
            if tag.find(_INTERACTIVE) or tag.find("img"):
                continue
            if tag.get_text(strip=True):
                continue
            tag.decompose()
            changed = True
