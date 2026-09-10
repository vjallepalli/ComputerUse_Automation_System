"""clean_html against real target_app markup: strip the legacy chrome, keep the flow.

No browser here -- the cleaner is a pure string transform, fed the same HTML
Playwright's page.content() would return.
"""

import re

import pytest
from bs4 import BeautifulSoup

from surface.dom import CleanedDom, _clean, clean_html, get_cleaned_dom


class _FakePage:
    """Just enough Playwright Page surface for get_cleaned_dom (no browser)."""

    def __init__(self, html: str):
        self._html = html

    def content(self) -> str:
        return self._html


@pytest.fixture(scope="module")
def pages():
    from target_app.app import create_app

    c = create_app().test_client()
    c.post("/login", data={"u": "clerk", "p": "vault"})
    return {
        "login": create_app().test_client().get("/login").get_data(as_text=True),
        "lookup": c.get("/member").get_data(as_text=True),
        "detail": c.get("/member/10001").get_data(as_text=True),
        "sub_account": c.get("/member/10001/sub-account").get_data(as_text=True),
    }


def test_strips_scripts_styles_comments_and_presentational_cruft(pages):
    for name, raw in pages.items():
        out = clean_html(raw)
        assert "<script" not in out, name
        assert "<style" not in out and "<font" not in out, name
        assert "<!--" not in out, name
        assert "bgcolor" not in out and "cellpadding" not in out, name
        assert "onclick" not in out, name
        assert 'style="' not in out, name


def test_keeps_structure_text_and_interactive_elements(pages):
    lookup = clean_html(pages["lookup"])
    assert "<table" in lookup                       # nested-table layout survives
    assert "Member Lookup" in lookup                # visible text survives
    assert '<input' in lookup and 'name="q"' in lookup
    assert "Retrieve" in lookup                     # the span-submit's label

    detail = clean_html(pages["detail"])
    assert "Dorothy Q. Fentiman" in detail
    assert "2841.19" in detail
    assert 'href="/member/10001/sub-account"' in detail
    assert "Open sub-account" in detail


def test_interactive_element_count_is_preserved(pages):
    for name, raw in pages.items():
        before = BeautifulSoup(raw, "html.parser")
        after = BeautifulSoup(clean_html(raw), "html.parser")
        for tag in ("input", "a", "form"):
            assert len(before.find_all(tag)) == len(after.find_all(tag)), (name, tag)


def test_cleaning_meaningfully_shrinks_the_page(pages):
    for name, raw in pages.items():
        out = clean_html(raw)
        assert len(out) < 0.6 * len(raw), (name, len(raw), len(out))


def test_empty_spacer_cells_are_pruned(pages):
    # target_app uses <tr><td height="8"></td></tr> spacer rows everywhere
    raw = pages["detail"]
    assert 'height="8"' in raw
    out = clean_html(raw)
    soup = BeautifulSoup(out, "html.parser")
    empty_cells = [td for td in soup.find_all("td") if not td.get_text(strip=True)]
    assert empty_cells == []


# --- synthetic aria-labels for cell-labelled fields --------------------------


def _input(html: str, name: str):
    return BeautifulSoup(clean_html(html), "html.parser").find("input", attrs={"name": name})


def test_real_label_is_left_untouched():
    # associated via <label for>
    for_html = """<table><tr>
      <td><label for="amt">Amount</label></td>
      <td><input id="amt" name="viafor" type="text"></td>
    </tr></table>"""
    assert _input(for_html, "viafor").get("aria-label") is None

    # a real aria-label is never overridden by the adjacent cell text
    aria_html = """<table><tr>
      <td>Wrong Text</td>
      <td><input name="viaaria" aria-label="Chosen Name" type="text"></td>
    </tr></table>"""
    assert _input(aria_html, "viaaria")["aria-label"] == "Chosen Name"


def test_unlabelled_field_gets_preceding_cell_text():
    html = """<table><tr>
      <td>Routing number</td>
      <td><input name="rt" type="text"></td>
    </tr></table>"""
    assert _input(html, "rt")["aria-label"] == "Routing number"


def test_two_same_text_fields_are_disambiguated_not_collided():
    html = """<table>
      <tr><td>Amount</td><td><input name="a1" type="text"></td></tr>
      <tr><td>Amount</td><td><input name="a2" type="text"></td></tr>
    </table>"""
    a1 = _input(html, "a1")["aria-label"]
    a2 = _input(html, "a2")["aria-label"]
    assert {a1, a2} == {"Amount (1)", "Amount (2)"}
    assert a1 != a2


def test_field_with_no_preceding_text_is_left_alone():
    html = """<table><tr><td><input name="lonely" type="text"></td></tr></table>"""
    assert _input(html, "lonely").get("aria-label") is None


def test_sub_account_fields_get_their_row_labels(pages):
    out = clean_html(pages["sub_account"])
    soup = BeautifulSoup(out, "html.parser")
    got = {i["name"]: i.get("aria-label") for i in soup.find_all("input")}
    assert got["f1"] == "Sub-account type"
    assert got["f2"] == "Initial deposit"
    assert got["f3"] == "Verify member SSN"
    assert got["x7"] is None  # type=button carries its own name (value)


def test_get_cleaned_dom_returns_pairs_that_match_the_cleaned_string():
    html = """<table>
      <tr><td>Amount</td><td><input name="a1" type="text"></td></tr>
      <tr><td>Amount</td><td><input name="a2" type="text"></td></tr>
      <tr><td>Routing</td><td><input name="rt" type="text"></td></tr>
      <tr><td><label for="ok">Real</label></td><td><input id="ok" name="r0"></td></tr>
      <tr><td>Ticket</td><td><input id="tk" type="text"></td></tr>
      <tr><td>Free</td><td><input type="text"></td></tr>
    </table>"""
    result = get_cleaned_dom(_FakePage(html))
    assert isinstance(result, CleanedDom)

    # one pair per synthesised field: id -> #id, else tag[name=...], else tag+index
    pairs = {(p["selector"], p["match_index"]): p["label"] for p in result.synthetic_labels}
    assert pairs == {
        ('input[name="a1"]', 0): "Amount (1)",
        ('input[name="a2"]', 0): "Amount (2)",
        ('input[name="rt"]', 0): "Routing",
        ("#tk", 0): "Ticket",
        ("input", 5): "Free",           # 6th <input> in document order
    }

    # every pair's label is exactly what landed in the cleaned string
    soup = BeautifulSoup(result.html, "html.parser")
    assert soup.find("input", {"name": "a1"})["aria-label"] == "Amount (1)"
    assert soup.find("input", {"id": "tk"})["aria-label"] == "Ticket"
    assert soup.find("input", {"name": "r0"}).get("aria-label") is None  # real label


# --- live field-value overlay (page.content() serialises stale value attrs) ---


class _FakeFormPage:
    """content() is the stale served HTML; evaluate() returns the live values."""

    def __init__(self, html: str, live_values: list[dict]):
        self._html = html
        self._live = live_values

    def content(self) -> str:
        return self._html

    def evaluate(self, _script: str):
        return self._live


_SUB_FORM_STALE = (
    '<table>'
    '<tr><td>Sub-account type</td><td><input name="f1" type="text" value=""></td></tr>'
    '<tr><td>Initial deposit</td><td><input name="f2" type="text" value=""></td></tr>'
    '<tr><td>Notes</td><td><input name="f3" type="text" value=""></td></tr>'
    '</table>'
)


def _field(html: str, name: str) -> str:
    return re.search(rf'<input[^>]*name="{name}"[^>]*/?>', html).group(0)


def test_clean_html_without_a_page_keeps_the_served_value():
    # no live page -> no overlay; the stale served value is left as-is
    assert 'value=""' in clean_html('<input name="f1" type="text" value="">')


def test_clean_overlays_live_value_onto_the_tree_when_supplied():
    out, _ = _clean(
        _SUB_FORM_STALE,
        live_field_values=[
            {"tag": "input", "type": "text", "key": "f1", "value": "Savings"},
            {"tag": "input", "type": "text", "key": "f2", "value": "500.00"},
            {"tag": "input", "type": "text", "key": "f3", "value": ""},
        ],
    )
    assert 'value="Savings"' in _field(out, "f1")
    assert 'value="500.00"' in _field(out, "f2")
    assert 'value=""' in _field(out, "f3")


def test_clean_falls_back_to_key_match_when_field_counts_disagree():
    # one extra live entry (e.g. a field the parser merged) -> match by name
    out, _ = _clean(
        _SUB_FORM_STALE,
        live_field_values=[
            {"tag": "input", "type": "text", "key": "phantom", "value": "x"},
            {"tag": "input", "type": "text", "key": "f2", "value": "500.00"},
            {"tag": "input", "type": "text", "key": "f1", "value": "Savings"},
            {"tag": "input", "type": "text", "key": "f3", "value": ""},
        ],
    )
    assert 'value="Savings"' in _field(out, "f1")
    assert 'value="500.00"' in _field(out, "f2")


def test_get_cleaned_dom_shows_the_filled_value_so_the_model_wont_retype():
    # exact repro shape: f1 typed, f2 not yet -> the DOM the model decides f2 from
    # must show f1 as filled, or it retargets f1 forever.
    live = [
        {"tag": "input", "type": "text", "key": "f1", "value": "Savings"},
        {"tag": "input", "type": "text", "key": "f2", "value": ""},
        {"tag": "input", "type": "text", "key": "f3", "value": ""},
    ]
    dom = get_cleaned_dom(_FakeFormPage(_SUB_FORM_STALE, live)).html
    assert 'value="Savings"' in _field(dom, "f1")     # NOT value="" -- the whole bug
    assert 'value=""' in _field(dom, "f2")
    assert 'value=""' in _field(dom, "f3")


def test_get_cleaned_dom_skips_overlay_when_page_has_no_evaluate():
    # _FakePage (content() only) -> evaluate() raises -> overlay skipped, no crash
    assert 'value=""' in get_cleaned_dom(_FakePage('<input name="f1" value="">')).html


# --- sensitive-value masking (never reaches the model, displayed or typed) ----


def test_account_detail_tax_id_is_masked_never_the_real_digits(pages):
    # target_app /member/10001 detail page has a "Tax ID" row with 912-04-5510
    raw = pages["detail"]
    assert "912-04-5510" in raw                         # present before cleaning
    out = clean_html(raw)
    assert "912-04-5510" not in out
    assert "[MASKED]" in out
    assert not re.search(r"\d{3}-\d{2}-\d{4}", out)     # no SSN-shape survives anywhere


_SSN_ROW = (
    '<table><tr>'
    '<td>Verify member SSN</td>'
    '<td><input name="f3" type="text" value=""></td>'
    '<td>NNN-NN-NNNN</td>'                       # static format hint -- not real data
    '</tr></table>'
)


def test_filled_ssn_field_is_masked_even_when_overlaid_live():
    # a human just typed the real SSN -> it must NOT reach the model
    out, _ = _clean(_SSN_ROW, live_field_values=[
        {"tag": "input", "type": "text", "key": "f3", "value": "912-33-1092"},
    ])
    assert "912-33-1092" not in out
    assert 'value="[MASKED]"' in _field(out, "f3")


def test_empty_ssn_field_stays_visibly_empty_not_masked():
    # regression: an untouched sensitive field must look empty, or the model
    # treats it as filled and skips the human hand-off.
    string_path = clean_html(_SSN_ROW)                        # no live values
    assert 'value=""' in _field(string_path, "f3")
    assert "[MASKED]" not in string_path

    overlaid, _ = _clean(_SSN_ROW, live_field_values=[        # live overlay, still empty
        {"tag": "input", "type": "text", "key": "f3", "value": ""},
    ])
    assert 'value=""' in _field(overlaid, "f3")
    assert "[MASKED]" not in overlaid


def test_ssn_format_hint_cell_is_not_masked():
    # deliberate: the "NNN-NN-NNNN" cell holds no real data and is useful context;
    # it sits two cells past the label, so immediate-sibling adjacency spares it.
    out = clean_html(_SSN_ROW)
    assert "NNN-NN-NNNN" in out


def test_server_prefilled_sensitive_field_is_masked_on_the_string_path():
    prefilled = _SSN_ROW.replace('name="f3" type="text" value=""',
                                 'name="f3" type="text" value="912-77-3301"')
    out = clean_html(prefilled)          # no live page, value came from the server
    assert "912-77-3301" not in out
    assert 'value="[MASKED]"' in _field(out, "f3")


def test_ssn_shape_is_masked_even_inside_a_sentence():
    out, _ = _clean("<p>the member's tax id on file is 912-90-4415 per the record</p>")
    assert "912-90-4415" not in out
    assert "[MASKED-SSN]" in out


def test_non_sensitive_values_are_left_alone():
    out, _ = _clean(_SUB_FORM_STALE, live_field_values=[
        {"tag": "input", "type": "text", "key": "f1", "value": "Savings"},
        {"tag": "input", "type": "text", "key": "f2", "value": "500.00"},
        {"tag": "input", "type": "text", "key": "f3", "value": "keep me"},
    ])
    assert 'value="Savings"' in _field(out, "f1")
    assert 'value="500.00"' in _field(out, "f2")
    assert 'value="keep me"' in _field(out, "f3")       # "Notes" is not sensitive
