"""The target_app's deterministic failure states must actually trigger.

These are the fixtures later replay-error-handling work leans on: a not-found
result, a re-rendered validation error, and a permission-denied page. A couple
of happy-path assertions are included so the failure assertions mean something.
"""

import re

import pytest

from target_app.app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    c = app.test_client()
    resp = c.post("/login", data={"u": "clerk", "p": "vault"})
    assert resp.status_code == 302  # signed on -> redirected to lookup
    return c


# --- failure states ---------------------------------------------------------


def test_unknown_member_id_returns_not_found(client):
    resp = client.get("/member/lookup", query_string={"q": "99999"})
    assert resp.status_code == 404
    assert b"No Such Member" in resp.data
    assert b"99999" in resp.data


@pytest.mark.parametrize("q", ["", "   ", None])
def test_empty_member_id_re_renders_lookup_with_a_prompt(client, q):
    params = {} if q is None else {"q": q}
    resp = client.get("/member/lookup", query_string=params)
    assert resp.status_code == 400
    assert b"Enter a member ID." in resp.data
    # stays on the lookup form; not the not-found page
    assert b'action="/member/lookup"' in resp.data
    assert b"No Such Member" not in resp.data


def test_empty_sub_account_form_re_renders_validation_error(client):
    resp = client.post("/member/10001/sub-account", data={"f1": "", "f2": "", "f3": ""})
    assert resp.status_code == 400
    assert b"Cannot process" in resp.data
    assert b"required" in resp.data
    # re-rendered on the same page, not a redirect
    assert b'action="/member/10001/sub-account"' in resp.data


def test_malformed_ssn_is_a_validation_error(client):
    resp = client.post(
        "/member/10001/sub-account",
        data={"f1": "Savings", "f2": "100", "f3": "not-an-ssn"},
    )
    assert resp.status_code == 400
    assert b"NNN-NN-NNNN" in resp.data


def test_restricted_member_lookup_is_denied(client):
    resp = client.get("/member/lookup", query_string={"q": "10004"})
    assert resp.status_code == 403
    assert b"Access Restricted" in resp.data


def test_restricted_member_action_is_denied(client):
    resp = client.post(
        "/member/10004/sub-account",
        data={"f1": "Savings", "f2": "100", "f3": "912-77-3301"},
    )
    assert resp.status_code == 403
    assert b"RESTRICTED" in resp.data


def test_login_required_redirects_to_sign_on():
    resp = create_app().test_client().get("/member")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# --- markup: submit controls must sit inside their <form> -----------------
# Regression: a <form> placed as a direct child of <table> (before <tr>) is
# foster-parented out by browser parsers, orphaning the submit control so
# this.closest('form') / this.form is null. The form must wrap the table.


def _assert_form_wraps_submit(html: str, action: str, needle: str):
    open_form = html.index(f'<form method=')
    open_table = html.index("<table", open_form)
    ctrl = html.index(needle, open_table)
    close_table = html.index("</table>", ctrl)
    close_form = html.index("</form>", close_table)
    assert open_form < open_table < ctrl < close_table < close_form
    assert action in html[open_form:close_form]
    # no stray <form> wedged between <table> and its first <tr>
    assert not re.search(r"<table[^>]*>\s*<form", html)


def test_lookup_submit_span_is_inside_the_form(client):
    html = client.get("/member").get_data(as_text=True)
    _assert_form_wraps_submit(html, "/member/lookup", ">Retrieve</span>")


def test_sub_account_submit_button_is_inside_the_form(client):
    html = client.get("/member/10001/sub-account").get_data(as_text=True)
    _assert_form_wraps_submit(html, "/member/10001/sub-account", "this.form.submit()")


# --- happy path (contrast) ------------------------------------------------


def test_happy_path_lookup_detail_and_open_sub_account(client):
    hit = client.get("/member/lookup", query_string={"q": "10001"})
    assert hit.status_code == 302
    assert hit.headers["Location"].endswith("/member/10001")

    detail = client.get("/member/10001")
    assert detail.status_code == 200
    assert b"2841.19" in detail.data  # a seeded balance

    done = client.post(
        "/member/10001/sub-account",
        data={"f1": "Savings", "f2": "250.00", "f3": "912-04-5510"},
    )
    assert done.status_code == 200
    assert b"Sub-Account Opened" in done.data
    assert b"SUB-10001-" in done.data


def test_detail_and_confirmation_link_back_to_lookup_without_signing_off(client):
    detail = client.get("/member/10001")
    assert b'href="/member"' in detail.data

    done = client.post(
        "/member/10001/sub-account",
        data={"f1": "Savings", "f2": "1", "f3": "912-04-5510"},
    )
    assert b'href="/member"' in done.data


# --- reset hook + reproducible sub-account numbering ---------------------


def _open_sub_account(client, member_id, ssn):
    resp = client.post(
        f"/member/{member_id}/sub-account",
        data={"f1": "Savings", "f2": "10", "f3": ssn},
    )
    assert resp.status_code == 200
    return re.search(r"SUB-\d+-\d+", resp.get_data(as_text=True)).group(0)


def test_first_sub_account_is_01_regardless_of_seeded_account_count(client):
    client.post("/debug/reset")
    # member 10003 seeds THREE accounts; the counter must still start at 01
    assert _open_sub_account(client, "10003", "912-18-2247") == "SUB-10003-01"
    assert _open_sub_account(client, "10003", "912-18-2247") == "SUB-10003-02"


def test_debug_reset_restores_seed_and_rewinds_the_counter(client):
    client.post("/debug/reset")
    assert _open_sub_account(client, "10001", "912-04-5510") == "SUB-10001-01"
    assert _open_sub_account(client, "10001", "912-04-5510") == "SUB-10001-02"

    done = client.post("/debug/reset")
    assert done.status_code == 200
    assert b"Seed Restored" in done.data

    # opened accounts are gone from the detail page, counter is back to 01
    detail = client.get("/member/10001").get_data(as_text=True)
    assert "SUB-10001-" not in detail
    assert _open_sub_account(client, "10001", "912-04-5510") == "SUB-10001-01"


def test_debug_reset_needs_no_login():
    resp = create_app().test_client().post("/debug/reset")
    assert resp.status_code == 200
