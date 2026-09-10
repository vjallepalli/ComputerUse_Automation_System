"""execute_on_page against the real nested-table markup, driven by a live server.

This is the sanity check on selector resolution before an LLM is in the loop:
which of label / role_text / text_contains actually resolve against this UI, and
that zero / multiple matches fail loudly. The `label` strategy only works once
clean_html has synthesised aria-labels from the adjacent cells, so those tests
run against the cleaned DOM loaded back into the page.
"""

import re
import socket
import threading

import pytest

from schema.action import ClickAction, DoneAction, Target, TypeAction
from surface.dom import apply_synthetic_labels, get_cleaned_dom
from surface.executor import SelectorResolutionError, execute_on_page

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def base_url():
    from werkzeug.serving import make_server

    from target_app.app import create_app

    port = _free_port()
    server = make_server("127.0.0.1", port, create_app())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            yield b
            b.close()
    except Exception as exc:  # chromium not installed, sandbox, ...
        pytest.skip(f"cannot launch chromium: {exc}")


@pytest.fixture
def page(browser, base_url):
    ctx = browser.new_context(base_url=base_url)
    pg = ctx.new_page()
    # sign on with raw selectors -- login is setup here, not the thing under test
    pg.goto("/login")
    pg.fill("input[name='u']", "clerk")
    pg.fill("input[name='p']", "vault")
    pg.click("input[value='Sign On']")
    pg.wait_for_url("**/member")
    yield pg
    ctx.close()


# --- what resolves -------------------------------------------------------------


def test_click_link_by_role_text(page):
    page.goto("/member/10001")
    out = execute_on_page(
        page,
        ClickAction(action="click",
                    target=Target(strategy="role_text", value="link:Open sub-account")),
    )
    assert out["status"] == "ok"
    assert page.url.endswith("/member/10001/sub-account")


def test_click_submit_input_by_role_text(page):
    page.context.clear_cookies()
    page.goto("/login")
    page.fill("input[name='u']", "clerk")
    page.fill("input[name='p']", "vault")
    execute_on_page(
        page,
        ClickAction(action="click",
                    target=Target(strategy="role_text", value="button:Sign On")),
    )
    page.wait_for_url("**/member")


def test_click_span_submit_by_text_contains(page):
    page.goto("/member")
    page.fill("input[name='q']", "10001")
    execute_on_page(
        page,
        ClickAction(action="click",
                    target=Target(strategy="text_contains", value="Retrieve")),
    )
    page.wait_for_url("**/member/10001")


def test_type_into_sole_textbox_by_role_text(page):
    page.goto("/member")
    out = execute_on_page(
        page,
        TypeAction(action="type",
                   target=Target(strategy="role_text", value="textbox"),
                   text="10006"),
    )
    assert out == {
        "status": "ok", "action": "type", "strategy": "role_text",
        "value": "textbox", "text": "10006", "resolved_count": 1,
    }
    assert page.input_value("input[name='q']") == "10006"


def test_done_is_a_noop(page):
    out = execute_on_page(page, DoneAction(action="done", reason="finished"))
    assert out == {"status": "done", "action": "done", "reason": "finished"}


# --- label strategy: dead on raw markup, live once the pairs are pushed -------


def test_label_strategy_is_dead_on_raw_markup(page):
    # <td>text</td><td><input></td> with no real <label> -> nothing to resolve
    page.goto("/member")
    with pytest.raises(SelectorResolutionError, match="resolved to 0"):
        execute_on_page(
            page,
            TypeAction(action="type",
                       target=Target(strategy="label", value="Member number"),
                       text="10001"),
        )


def test_full_chain_clean_then_push_then_label_execute(page):
    # string clean -> live-DOM push -> execute, all against the same live page
    page.goto("/member/10001/sub-account")
    dom = get_cleaned_dom(page)
    assert 'aria-label="Initial deposit"' in dom.html          # what the model sees
    assert apply_synthetic_labels(page, dom.synthetic_labels) == []  # all matched live

    for label_text, field in [
        ("Sub-account type", "f1"),
        ("Initial deposit", "f2"),
        ("Verify member SSN", "f3"),
    ]:
        out = execute_on_page(
            page,
            TypeAction(action="type",
                       target=Target(strategy="label", value=label_text),
                       text=f"val-{field}"),
        )
        assert out["resolved_count"] == 1, label_text
        assert page.input_value(f"input[name='{field}']") == f"val-{field}"


def test_apply_synthetic_labels_is_a_noop_for_stale_selectors(page, caplog):
    import logging

    page.goto("/member")
    with caplog.at_level(logging.WARNING, logger="surface.dom"):
        missing = apply_synthetic_labels(
            page,
            [{"selector": "#gone", "match_index": 0, "label": "X"},
             {"selector": 'input[name="q"]', "match_index": 0, "label": "Member number"}],
        )
    assert missing == ["#gone[0]"]
    assert "no live element for #gone[0]" in caplog.text
    assert page.get_attribute("input[name='q']", "aria-label") == "Member number"


def test_ambiguous_role_text_raises_with_count(page):
    page.goto("/member/10001/sub-account")  # three unlabelled text inputs
    with pytest.raises(SelectorResolutionError, match="resolved to 3"):
        execute_on_page(
            page,
            TypeAction(action="type",
                       target=Target(strategy="role_text", value="textbox"),
                       text="x"),
        )


def test_missing_text_target_raises(page):
    page.goto("/member/10001")
    with pytest.raises(SelectorResolutionError, match="resolved to 0"):
        execute_on_page(
            page,
            ClickAction(action="click",
                        target=Target(strategy="text_contains", value="Wire transfer")),
        )


def _f(dom, name):
    return re.search(rf'<input[^>]*name="{name}"[^>]*/?>', dom).group(0)


def test_get_cleaned_dom_reflects_live_input_values_not_stale_attributes(page):
    # page.content() serialises value="" for these even after .fill(); the fix
    # overlays the live .value so the model doesn't see filled fields as blank.
    page.goto("/member/10003/sub-account")
    page.fill("input[name='f1']", "Savings")       # raw fill -- setup, not under test
    page.fill("input[name='f2']", "500.00")        # f3 (Verify member SSN) left EMPTY

    dom = get_cleaned_dom(page).html
    assert 'value="Savings"' in _f(dom, "f1")
    assert 'value="500.00"' in _f(dom, "f2")
    # an EMPTY sensitive field must stay visibly empty, NOT [MASKED] -- else the
    # model treats it as filled and skips the human hand-off.
    assert 'value=""' in _f(dom, "f3")
    assert "[MASKED]" not in _f(dom, "f3")


def test_get_cleaned_dom_masks_a_sensitive_field_once_it_has_a_real_value(page):
    page.goto("/member/10003/sub-account")
    page.fill("input[name='f3']", "912-18-2247")   # a real SSN typed in
    dom = get_cleaned_dom(page).html
    assert 'value="[MASKED]"' in _f(dom, "f3")
    assert "912-18-2247" not in dom                # never anywhere in the model's view
