"""Adversarial edge cases for surface.dom cleaning + sensitive masking.

Pure `clean_html` string transform (no browser). Covers adjacency scoping of
the label-masking pass, non-table / deeply-nested layouts, and hostile field
values. Several cases document deliberate heuristic limits (flagged JUDGMENT
CALL) rather than asserting an ideal.
"""

import re

from surface.dom import _synthesize_labels, clean_html
from bs4 import BeautifulSoup

SSN = "912-18-2247"


def _page(body: str) -> str:
    return f"<html><body>{body}</body></html>"


# --- masking pass 1: immediate-sibling adjacency scoping -----------------


def test_only_the_cell_immediately_after_a_sensitive_label_is_masked():
    out = clean_html(_page(
        "<table><tr>"
        "<td>Verify member SSN</td>"
        f"<td><input name='a' value='{SSN}'></td>"     # prev = sensitive -> masked
        "<td>Preferred name</td>"
        "<td><input name='b' value='Jamie'></td>"       # prev = 'Preferred name' -> kept
        "</tr></table>"
    ))
    assert SSN not in out
    assert "[MASKED]" in out
    assert "Jamie" in out


def test_two_sensitive_labels_in_one_row_each_mask_their_own_field():
    out = clean_html(_page(
        "<table><tr>"
        "<td>SSN</td><td><input name='a' value='111-22-3333'></td>"
        "<td>Tax ID</td><td><input name='b' value='444-55-6666'></td>"
        "</tr></table>"
    ))
    assert "111-22-3333" not in out
    assert "444-55-6666" not in out
    assert out.count("[MASKED]") >= 2


def test_static_format_hint_cell_two_past_the_label_is_left_intact():
    # row is [label][field][hint]; only the field cell is masked, the
    # "NNN-NN-NNNN" hint is useful context and holds no real data.
    out = clean_html(_page(
        "<table><tr>"
        "<td>SSN</td><td><input name='a' value=''></td><td>NNN-NN-NNNN</td>"
        "</tr></table>"
    ))
    assert "NNN-NN-NNNN" in out


def test_empty_sensitive_field_is_not_replaced_with_a_fake_masked_value():
    # masking hides a real secret; it must never fabricate the look of one, or
    # the model reads the field as already-filled and skips the human hand-off.
    out = clean_html(_page(
        "<table><tr><td>Verify member SSN</td><td><input name='a' value=''></td></tr></table>"
    ))
    assert "[MASKED]" not in out


# --- non-table / div-only layouts (JUDGMENT CALL: documented limit) ------


def test_div_only_sensitive_display_value_is_not_caught_by_pass_1():
    # JUDGMENT CALL: pass 1 keys off <td>/<th> adjacency -- this legacy UI's
    # only labelling mechanism. A div/span layout that shows a secret with no
    # table cell is NOT masked by pass 1. Left as-is: the SSN-shape regex
    # (pass 2) is the backstop for actual SSNs, and adding a general
    # "any element next to sensitive text" rule would over-mask huge chunks of
    # arbitrary markup. Non-SSN secrets in a div layout are a known gap.
    non_ssn_secret = "hunter2pw"
    out = clean_html(_page(
        f"<div><span>Password</span><span>{non_ssn_secret}</span></div>"
    ))
    assert non_ssn_secret in out            # documents the gap


def test_ssn_shape_regex_still_catches_a_secret_in_a_non_table_layout():
    out = clean_html(_page(f"<div><span>Password</span><span>{SSN}</span></div>"))
    assert SSN not in out
    assert "[MASKED-SSN]" in out


def test_ssn_shape_regex_catches_an_ssn_buried_in_a_sentence():
    out = clean_html(_page(f"<p>Applicant SSN on file is {SSN} per the 2019 form.</p>"))
    assert SSN not in out
    assert "[MASKED-SSN]" in out


# --- deeply nested tables ---------------------------------------------


def test_field_in_a_table_nested_inside_a_sensitive_labelled_cell_is_masked():
    # `cell.find_all(FIELD_TAGS)` recurses, so a field in an inner table still
    # gets masked via the OUTER sensitive-labelled cell. Over-broad in the safe
    # direction (never leaks), which is the intended bias.
    out = clean_html(_page(
        "<table><tr>"
        "<td>SSN</td>"
        f"<td><table><tr><td><input name='deep' value='{SSN}'></td></tr></table></td>"
        "</tr></table>"
    ))
    assert SSN not in out
    assert "[MASKED]" in out


def test_triple_nested_table_label_synthesis_uses_the_immediate_cell_only():
    # JUDGMENT CALL: `_preceding_cell_text` looks one adjacency level up
    # (parent <td> -> its preceding <td> siblings). A label sitting only in an
    # OUTER table's cell is not pulled down into an inner field. The field here
    # gets its label from the innermost row that actually has a preceding cell.
    html = _page(
        "<table><tr><td>Outer label</td><td>"
        "  <table><tr><td>Inner label</td><td><input name='x'></td></tr></table>"
        "</td></tr></table>"
    )
    soup = BeautifulSoup(html, "html.parser")
    applied = _synthesize_labels(soup)
    assert [a["label"] for a in applied] == ["Inner label"]


# --- real label + sensitive-adjacent cell ---------------------------


def test_real_label_field_next_to_a_sensitive_cell_is_masked_once_not_doubled():
    # JUDGMENT CALL: adjacency masking fires on cell position regardless of
    # whether the field also has its own <label>. That over-masks a genuinely
    # non-secret field whose neighbouring cell merely mentions a keyword -- but
    # it degrades the model's context, it never leaks, so the aggressive bias
    # stands. What must NOT happen is double-masking ("[MASKED][MASKED]").
    out = clean_html(_page(
        "<table><tr>"
        "<td>Do not enter SSN in this box</td>"
        "<td><label for='m'>Member number</label>"
        "<input id='m' name='m' value='10003'></td>"
        "</tr></table>"
    ))
    assert "10003" not in out
    assert "[MASKED][MASKED]" not in out
    assert out.count("[MASKED]") == 1


def test_non_sensitive_field_with_a_real_label_keeps_its_value():
    out = clean_html(_page(
        "<table><tr>"
        "<td>Branch</td>"
        "<td><label for='b'>Branch code</label><input id='b' name='b' value='BR-07'></td>"
        "</tr></table>"
    ))
    assert "BR-07" in out


# --- hostile field values -------------------------------------------


def test_extremely_long_field_value_does_not_crash_and_is_preserved():
    big = "x" * 12000
    out = clean_html(_page(
        f"<table><tr><td>Notes</td><td><input name='n' value='{big}'></td></tr></table>"
    ))
    assert big in out


def test_html_special_chars_in_a_value_are_escaped_not_interpreted():
    payload = '"><script>alert(1)</script>'
    out = clean_html(_page(
        f"<table><tr><td>Notes</td><td><input name='n' value='{payload}'></td></tr></table>"
    ))
    assert "<script>alert(1)</script>" not in out      # no raw tag survives
    assert "&lt;script&gt;" in out or "alert(1)" in out  # escaped form is fine


def test_ampersands_and_entities_in_text_survive_cleaning():
    out = clean_html(_page("<table><tr><td>Owner &amp; co</td><td>value</td></tr></table>"))
    assert "Owner & co" in out or "Owner &amp; co" in out


# --- label synthesis on structureless markup ------------------------


def test_no_table_structure_yields_no_synthetic_labels_and_no_crash():
    html = _page("<div><span>Amount</span><input name='amount'></div>")
    soup = BeautifulSoup(html, "html.parser")
    applied = _synthesize_labels(soup)
    assert applied == []


def test_two_fields_sharing_a_label_cell_text_get_deduped_suffixes():
    html = _page(
        "<table>"
        "<tr><td>Amount</td><td><input name='a1'></td></tr>"
        "<tr><td>Amount</td><td><input name='a2'></td></tr>"
        "</table>"
    )
    soup = BeautifulSoup(html, "html.parser")
    applied = _synthesize_labels(soup)
    assert sorted(a["label"] for a in applied) == ["Amount (1)", "Amount (2)"]
