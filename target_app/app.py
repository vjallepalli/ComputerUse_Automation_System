"""Flask app factory for the legacy back-office stand-in.

Clean, RESTful-ish routes; deliberately legacy-hostile rendered HTML (nested
<table> layout, no id / data-testid attributes, generic control names, a
<span onclick> and a bare <input type=button> as submit controls). Full page
loads only -- no framesets, no AJAX. Thin session login on top.

Flow: /login -> /member (lookup) -> /member/<id> (detail) ->
/member/<id>/sub-account (form) -> confirmation.

Deterministic failure states (see tests/target_app/):
  * unknown member id            -> 404 "member not found"
  * bad input on sub-account form -> 400 validation error, same page re-rendered
  * restricted member            -> 403 permission denied (lookup or any action)
  * slow member detail page      -> artificial delay (wait/timeout exercise)
"""

from __future__ import annotations

import os
import re
import time

from flask import (
    Flask,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)

from target_app.data import (
    SLOW_LOAD_SECONDS,
    SLOW_MEMBER_ID,
    get_member,
    next_sub_account_number,
    reset,
)

SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
SUB_ACCOUNT_TYPES = {"savings", "checking", "money market"}

# --- Legacy-hostile page chrome -------------------------------------------------
# Everything renders inside nested tables. No id / class hooks worth the name.

_SHELL = """<!doctype html>
<html><head><title>CU Back Office</title></head>
<body bgcolor="#f4f2ec">
<table border="0" cellpadding="0" cellspacing="0" width="760" align="center"><tr><td>
<table border="0" cellpadding="6" cellspacing="0" width="100%" bgcolor="#20303f"><tr>
  <td><font face="Verdana" size="4" color="#ffffff"><b>Cascadia Mutual C.U.</b></font></td>
  <td align="right"><font face="Verdana" size="1" color="#c8d4df">
    {% if session.get('user') %}operator: {{ session['user'] }}
    &nbsp;|&nbsp; <a href="{{ url_for('logout') }}"><font color="#c8d4df">sign off</font></a>
    {% else %}not signed on{% endif %}
  </font></td>
</tr></table>
<table border="0" cellpadding="0" cellspacing="0" width="100%"><tr><td height="8"></td></tr></table>
<table border="1" cellpadding="12" cellspacing="0" width="100%" bgcolor="#ffffff"
       bordercolor="#c0b9a8"><tr><td>
<font face="Verdana" size="2" color="#20303f">
{{ body|safe }}
</font>
</td></tr></table>
<table border="0" cellpadding="4" cellspacing="0" width="100%"><tr>
  <td><font face="Verdana" size="1" color="#9a9384">
  Internal use only. Screen 3B-ACCT. Fake data.</font></td>
</tr></table>
</td></tr></table>
</body></html>"""


def _render(body: str, status: int = 200):
    return render_template_string(_SHELL, body=body), status


# --- App factory --------------------------------------------------------------


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET", "dev-only-not-a-secret")
    app.config["USERNAME"] = os.environ.get("TARGET_APP_USERNAME") or "clerk"
    app.config["PASSWORD"] = os.environ.get("TARGET_APP_PASSWORD") or "vault"

    @app.before_request
    def _require_login():
        if request.endpoint in {"login", "static", "debug_reset"} or session.get("user"):
            return None
        return redirect(url_for("login"))

    @app.route("/debug/reset", methods=["POST"])
    def debug_reset():
        # Local-only test hook: restore the pristine seed. Deliberately ungated
        # and login-exempt so a fresh client can reset before signing on.
        reset()
        return _render(render_template_string(_RESET_BODY))

    # --- Auth ---------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            if (
                request.form.get("u") == app.config["USERNAME"]
                and request.form.get("p") == app.config["PASSWORD"]
            ):
                session["user"] = request.form["u"]
                return redirect(url_for("member_lookup"))
            error = "Sign-on rejected. Check operator ID and passphrase."
        body = render_template_string(_LOGIN_BODY, error=error)
        return _render(body, 200 if not error else 401)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --- Member lookup ----------------------------------------------------

    @app.route("/")
    def index():
        return redirect(url_for("member_lookup"))

    @app.route("/member")
    def member_lookup():
        return _render(render_template_string(_LOOKUP_BODY, error=""))

    @app.route("/member/lookup")
    def member_lookup_submit():
        raw = (request.args.get("q") or "").strip()
        if not raw:
            body = render_template_string(_LOOKUP_BODY, error="Enter a member ID.")
            return _render(body, 400)
        member = get_member(raw)
        if member is None:
            body = render_template_string(_NOT_FOUND_BODY, queried=raw)
            return _render(body, 404)
        if member["status"] == "restricted":
            return _render(render_template_string(_DENIED_BODY, member=member), 403)
        return redirect(url_for("member_detail", member_id=member["member_id"]))

    # --- Account detail -------------------------------------------------

    @app.route("/member/<member_id>")
    def member_detail(member_id: str):
        member = get_member(member_id)
        if member is None:
            return _render(render_template_string(_NOT_FOUND_BODY, queried=member_id), 404)
        if member["status"] == "restricted":
            return _render(render_template_string(_DENIED_BODY, member=member), 403)
        if member["member_id"] == SLOW_MEMBER_ID:
            time.sleep(SLOW_LOAD_SECONDS)  # exercise a wait/timeout condition later
        total = sum(a["balance"] for a in member["accounts"])
        body = render_template_string(_DETAIL_BODY, member=member, total=total)
        return _render(body)

    # --- Open sub-account --------------------------------------------

    @app.route("/member/<member_id>/sub-account", methods=["GET", "POST"])
    def sub_account(member_id: str):
        member = get_member(member_id)
        if member is None:
            return _render(render_template_string(_NOT_FOUND_BODY, queried=member_id), 404)
        if member["status"] == "restricted":
            return _render(render_template_string(_DENIED_BODY, member=member), 403)

        values = {"f1": "", "f2": "", "f3": ""}
        if request.method == "GET":
            body = render_template_string(_SUB_FORM_BODY, member=member, errors=[], v=values)
            return _render(body)

        values = {k: (request.form.get(k) or "").strip() for k in values}
        errors = _validate_sub_account(values, member)
        if errors:
            body = render_template_string(
                _SUB_FORM_BODY, member=member, errors=errors, v=values
            )
            return _render(body, 400)

        number = next_sub_account_number(member)
        member["accounts"].append(
            {"number": number, "type": values["f1"].title(), "balance": float(values["f2"])}
        )
        body = render_template_string(
            _CONFIRM_BODY, member=member, number=number,
            kind=values["f1"].title(), deposit=float(values["f2"]),
        )
        return _render(body)

    return app


def _validate_sub_account(values: dict, member: dict) -> list[str]:
    errors: list[str] = []
    if not values["f1"]:
        errors.append("Sub-account type is required.")
    elif values["f1"].lower() not in SUB_ACCOUNT_TYPES:
        errors.append("Sub-account type must be Savings, Checking, or Money Market.")
    if not values["f2"]:
        errors.append("Initial deposit is required.")
    else:
        try:
            if float(values["f2"]) < 0:
                errors.append("Initial deposit cannot be negative.")
        except ValueError:
            errors.append("Initial deposit must be a number.")
    if not values["f3"]:
        errors.append("SSN verification is required.")
    elif not SSN_RE.match(values["f3"]):
        errors.append("SSN verification must be formatted NNN-NN-NNNN.")
    elif values["f3"] != member["ssn"]:
        errors.append("SSN verification does not match member of record.")
    return errors


# --- Page bodies (rendered into _SHELL) -------------------------------------

_RESET_BODY = """<b>Seed Restored</b>
<p><font face="Verdana" size="2">In-memory member data has been reset to its
pristine state. Sub-accounts opened this session are gone.</font></p>
<p><a href="/member"><font face="Verdana" size="2">Back to lookup</font></a></p>"""

_LOGIN_BODY = """<b>Operator Sign-On</b>
<form method="post" action="/login">
<table border="0" cellpadding="4" cellspacing="0">
<tr><td><font face="Verdana" size="2">Operator ID</font></td>
    <td><input type="text" name="u" size="24"></td></tr>
<tr><td><font face="Verdana" size="2">Passphrase</font></td>
    <td><input type="password" name="p" size="24"></td></tr>
<tr><td></td><td><input type="submit" name="ok" value="Sign On"></td></tr>
</table>
</form>
{% if error %}<p><font face="Verdana" size="2" color="#a01010">{{ error }}</font></p>{% endif %}
<p><font face="Verdana" size="1" color="#9a9384">Demo operator: clerk / vault</font></p>"""

_LOOKUP_BODY = """<b>Member Lookup</b>
{% if error %}
<p><font face="Verdana" size="2" color="#a01010">{{ error }}</font></p>
{% endif %}
<form method="get" action="/member/lookup">
<table border="0" cellpadding="4" cellspacing="0">
<tr><td><font face="Verdana" size="2">Member number</font></td>
    <td><input type="text" name="q" size="18"></td>
    <td>&nbsp;<span style="border:1px solid #20303f;background:#dfe6ec;padding:2px 10px;
        cursor:pointer;font-family:Verdana;font-size:11px"
        onclick="var f=this.closest('form'); if(f.q.value.trim()){f.submit();}else{alert('Enter a member ID.');}"
        >Retrieve</span></td></tr>
</table>
</form>
<p><font face="Verdana" size="1" color="#9a9384">Try 10001, 10003, 10006. 10004 is restricted.</font></p>"""

_NOT_FOUND_BODY = """<b>No Such Member</b>
<p><font face="Verdana" size="2">Member number
<b>{{ queried|e }}</b> is not on file. Re-key the number and retrieve again.</font></p>
<p><a href="/member"><font face="Verdana" size="2">Back to lookup</font></a></p>"""

_DENIED_BODY = """<b>Access Restricted</b>
<p><font face="Verdana" size="2" color="#a01010">Member
<b>{{ member.member_id }}</b> ({{ member.name|e }}) is flagged RESTRICTED.
Lookup and account maintenance are denied at this workstation.</font></p>
<p><a href="/member"><font face="Verdana" size="2">Back to lookup</font></a></p>"""

_DETAIL_BODY = """<b>Member Account Detail</b>
<table border="0" cellpadding="3" cellspacing="0">
<tr><td><font face="Verdana" size="2">Member</font></td>
    <td><font face="Verdana" size="2"><b>{{ member.name|e }}</b> &nbsp; #{{ member.member_id }}</font></td></tr>
<tr><td><font face="Verdana" size="2">Tax ID</font></td>
    <td><font face="Verdana" size="2">{{ member.ssn }}</font></td></tr>
<tr><td><font face="Verdana" size="2">Phone</font></td>
    <td><font face="Verdana" size="2">{{ member.phone }}</font></td></tr>
</table>
<table border="0" cellpadding="0" cellspacing="0"><tr><td height="8"></td></tr></table>
<table border="1" cellpadding="6" cellspacing="0" bordercolor="#c0b9a8">
<tr bgcolor="#ece7da">
  <td><font face="Verdana" size="1"><b>Account</b></font></td>
  <td><font face="Verdana" size="1"><b>Type</b></font></td>
  <td align="right"><font face="Verdana" size="1"><b>Balance</b></font></td></tr>
{% for a in member.accounts %}
<tr>
  <td><font face="Verdana" size="2">{{ a.number }}</font></td>
  <td><font face="Verdana" size="2">{{ a.type }}</font></td>
  <td align="right"><font face="Verdana" size="2">{{ "%.2f"|format(a.balance) }}</font></td></tr>
{% endfor %}
<tr bgcolor="#ece7da">
  <td colspan="2"><font face="Verdana" size="1"><b>Relationship total</b></font></td>
  <td align="right"><font face="Verdana" size="2"><b>{{ "%.2f"|format(total) }}</b></font></td></tr>
</table>
<p><a href="/member/{{ member.member_id }}/sub-account"><font face="Verdana" size="2">
Open sub-account</font></a>
&nbsp;&nbsp;|&nbsp;&nbsp;
<a href="/member"><font face="Verdana" size="2">Member lookup</font></a></p>"""

_SUB_FORM_BODY = """<b>Open Sub-Account &mdash; {{ member.name|e }} (#{{ member.member_id }})</b>
{% if errors %}
<table border="1" cellpadding="6" cellspacing="0" bordercolor="#a01010" bgcolor="#fbeaea"><tr><td>
<font face="Verdana" size="2" color="#a01010"><b>Cannot process:</b><br>
{% for e in errors %}&bull; {{ e }}<br>{% endfor %}</font>
</td></tr></table><br>
{% endif %}
<form method="post" action="/member/{{ member.member_id }}/sub-account">
<table border="0" cellpadding="4" cellspacing="0">
<tr><td><font face="Verdana" size="2">Sub-account type</font></td>
    <td><input type="text" name="f1" size="20" value="{{ v.f1|e }}"></td>
    <td><font face="Verdana" size="1" color="#9a9384">Savings | Checking | Money Market</font></td></tr>
<tr><td><font face="Verdana" size="2">Initial deposit</font></td>
    <td><input type="text" name="f2" size="20" value="{{ v.f2|e }}"></td></tr>
<tr><td><font face="Verdana" size="2">Verify member SSN</font></td>
    <td><input type="text" name="f3" size="20" value="{{ v.f3|e }}"></td>
    <td><font face="Verdana" size="1" color="#9a9384">NNN-NN-NNNN</font></td></tr>
<tr><td></td><td><input type="button" name="x7" value="Process"
    onclick="this.form.submit()"></td></tr>
</table>
</form>
<p><a href="/member/{{ member.member_id }}"><font face="Verdana" size="2">Cancel</font></a></p>"""

_CONFIRM_BODY = """<b>Sub-Account Opened</b>
<table border="1" cellpadding="8" cellspacing="0" bordercolor="#2a7a2a" bgcolor="#eef7ee"><tr><td>
<font face="Verdana" size="2">
New account number &nbsp; <b>{{ number }}</b><br>
Holder &nbsp; {{ member.name|e }} (#{{ member.member_id }})<br>
Type &nbsp; {{ kind }}<br>
Opening balance &nbsp; {{ "%.2f"|format(deposit) }}
</font>
</td></tr></table>
<p><a href="/member/{{ member.member_id }}"><font face="Verdana" size="2">
Return to account detail</font></a>
&nbsp;&nbsp;|&nbsp;&nbsp;
<a href="/member"><font face="Verdana" size="2">Member lookup</font></a></p>"""


# Module-level instance for `flask --app target_app.app` and test imports.
# The runnable entrypoint lives in target_app/__main__.py (`python -m target_app`).
app = create_app()
