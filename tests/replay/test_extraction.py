"""The `next_cell` extraction strategy against a real flat account-detail table.

Needs chromium (set_content only). The table shape mirrors target_app's
`_DETAIL_BODY`: one <tr> per account, cells wrapped in <font>, value in the
column after the type label.
"""

import pytest

from replay.engine import _extract_one
from schema.capability import Extraction, OutputSpec
from surface.executor import SelectorResolutionError, resolve_locator

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

DETAIL_TABLE = """
<table border="1">
  <tr><td><font>Account</font></td><td><font>Type</font></td><td align="right"><font>Balance</font></td></tr>
  <tr><td><font>CHK-10003-01</font></td><td><font>Checking</font></td><td align="right"><font>6605.00</font></td></tr>
  <tr><td><font>SAV-10003-01</font></td><td><font>Savings</font></td><td align="right"><font>812.55</font></td></tr>
  <tr><td><font>MMK-10003-01</font></td><td><font>Money Market</font></td><td align="right"><font>44010.10</font></td></tr>
</table>
"""

# Two Savings rows -> "Savings" is ambiguous, must not silently pick the first.
AMBIGUOUS_TABLE = """
<table border="1">
  <tr><td>SUB-10003-01</td><td>Savings</td><td>10.00</td></tr>
  <tr><td>SUB-10003-02</td><td>Savings</td><td>20.00</td></tr>
</table>
"""


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            yield b
            b.close()
    except Exception as exc:  # chromium not installed / sandboxed
        pytest.skip(f"cannot launch chromium: {exc}")


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


def _next_cell(page, target):
    return resolve_locator(page, "next_cell", target).inner_text().strip()


def test_next_cell_reads_the_value_in_the_adjacent_column(page):
    page.set_content(DETAIL_TABLE)
    assert _next_cell(page, "Savings") == "812.55"
    assert _next_cell(page, "Checking") == "6605.00"
    assert _next_cell(page, "Money Market") == "44010.10"


def test_next_cell_unmatched_target_raises(page):
    page.set_content(DETAIL_TABLE)
    with pytest.raises(SelectorResolutionError, match="matched no table cell"):
        resolve_locator(page, "next_cell", "Brokerage")


def test_next_cell_ambiguous_target_raises(page):
    page.set_content(AMBIGUOUS_TABLE)
    with pytest.raises(SelectorResolutionError, match="matched 2 cells"):
        resolve_locator(page, "next_cell", "Savings")


def test_next_cell_last_column_has_no_next_sibling_raises(page):
    page.set_content(DETAIL_TABLE)
    with pytest.raises(SelectorResolutionError, match="no next <td> sibling"):
        resolve_locator(page, "next_cell", "812.55")  # Balance column, nothing after it


def test_extract_one_uses_next_cell_and_coerces_to_declared_type(page):
    page.set_content(DETAIL_TABLE)
    spec = OutputSpec(
        name="savings_balance", type="float", description="d",
        extraction=Extraction(strategy="next_cell", target="Savings"),
    )
    assert _extract_one(page, spec) == 812.55  # str "812.55" -> float
