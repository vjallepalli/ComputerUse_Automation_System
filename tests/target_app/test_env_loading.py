"""BUG (found via a fresh-clone setup test): target_app/__main__.py read
TARGET_APP_PORT straight from os.environ with no load_dotenv() call, so a value
set ONLY in .env (not exported in the shell) was silently ignored -- unlike
agent/orchestrator.py, which already loads .env before reading its env vars.
create_app() reads TARGET_APP_USERNAME / TARGET_APP_PASSWORD the same unguarded
way and had the identical bug, just masked because the demo defaults happen to
match .env.example.

These tests prove the FILE mechanism, not just "some env var got set somehow":
`dotenv.load_dotenv` is monkeypatched to the real load_dotenv pinned at a temp
.env file, so target_app/__main__.py's own load_dotenv() call (with no args) is
what has to do the work.

No real server is started -- create_app() is stubbed to a fake with a `.run()`
that records its kwargs instead of binding a port.
"""

import os

import dotenv
import pytest

from target_app import __main__ as target_main
from target_app.app import create_app


@pytest.fixture
def isolated_environ():
    """load_dotenv() mutates os.environ directly, bypassing monkeypatch's
    setenv/delenv tracking -- snapshot + restore by hand so these tests can't
    leak TARGET_APP_* values into the rest of the suite."""
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


class _FakeFlaskApp:
    def __init__(self):
        self.run_kwargs = None

    def run(self, **kwargs):
        self.run_kwargs = kwargs


def _pin_dotenv_to(monkeypatch, env_file):
    """Make the module's `from dotenv import load_dotenv` (a lazy, no-arg call)
    resolve against `env_file` instead of whatever .env real cwd/frame discovery
    would otherwise find -- deterministic, and proves the real dotenv mechanism."""
    real_load_dotenv = dotenv.load_dotenv
    monkeypatch.setattr(dotenv, "load_dotenv",
                        lambda *a, **k: real_load_dotenv(dotenv_path=env_file))


# --- target_app/__main__.py: TARGET_APP_PORT --------------------------


def test_main_picks_up_a_port_set_only_in_dotenv(monkeypatch, tmp_path, isolated_environ):
    env_file = tmp_path / ".env"
    env_file.write_text("TARGET_APP_PORT=8080\n")
    os.environ.pop("TARGET_APP_PORT", None)          # NOT in the shell env
    _pin_dotenv_to(monkeypatch, env_file)

    fake_app = _FakeFlaskApp()
    monkeypatch.setattr(target_main, "create_app", lambda: fake_app)

    target_main.main()

    assert fake_app.run_kwargs["port"] == 8080
    assert fake_app.run_kwargs["host"] == "127.0.0.1"


def test_main_falls_back_to_5001_with_no_port_anywhere(monkeypatch, tmp_path, isolated_environ):
    env_file = tmp_path / ".env"
    env_file.write_text("")                          # .env exists but sets nothing
    os.environ.pop("TARGET_APP_PORT", None)
    _pin_dotenv_to(monkeypatch, env_file)

    fake_app = _FakeFlaskApp()
    monkeypatch.setattr(target_main, "create_app", lambda: fake_app)

    target_main.main()

    assert fake_app.run_kwargs["port"] == 5001


def test_an_explicit_shell_env_var_still_wins_over_dotenv(monkeypatch, tmp_path, isolated_environ):
    # matches python-dotenv's default override=False, and the same precedence
    # agent/orchestrator.py already relies on: an inline/exported var beats .env.
    env_file = tmp_path / ".env"
    env_file.write_text("TARGET_APP_PORT=8080\n")
    os.environ["TARGET_APP_PORT"] = "9999"
    _pin_dotenv_to(monkeypatch, env_file)

    fake_app = _FakeFlaskApp()
    monkeypatch.setattr(target_main, "create_app", lambda: fake_app)

    target_main.main()

    assert fake_app.run_kwargs["port"] == 9999


# --- create_app(): TARGET_APP_USERNAME / TARGET_APP_PASSWORD ----------


def test_main_loads_dotenv_before_create_app_reads_credentials(
    monkeypatch, tmp_path, isolated_environ
):
    # create_app() itself never calls load_dotenv() -- it's main()'s job to load
    # .env BEFORE create_app() runs. Exercise that ordering through main(),
    # then call the real create_app() and check what it actually picked up.
    env_file = tmp_path / ".env"
    env_file.write_text("TARGET_APP_USERNAME=opuser\nTARGET_APP_PASSWORD=s3cret\n")
    os.environ.pop("TARGET_APP_USERNAME", None)
    os.environ.pop("TARGET_APP_PASSWORD", None)
    _pin_dotenv_to(monkeypatch, env_file)

    fake_app = _FakeFlaskApp()
    monkeypatch.setattr(target_main, "create_app", lambda: fake_app)
    target_main.main()                                # loads .env, then "creates" (fake) app

    real_app = create_app()                            # env is now populated; read it for real
    assert real_app.config["USERNAME"] == "opuser"
    assert real_app.config["PASSWORD"] == "s3cret"


def test_create_app_still_falls_back_to_clerk_vault_with_no_dotenv_values(
    monkeypatch, tmp_path, isolated_environ
):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    os.environ.pop("TARGET_APP_USERNAME", None)
    os.environ.pop("TARGET_APP_PASSWORD", None)
    _pin_dotenv_to(monkeypatch, env_file)

    target_main._load_dotenv()
    app = create_app()

    assert app.config["USERNAME"] == "clerk"
    assert app.config["PASSWORD"] == "vault"


def test_load_dotenv_helper_is_a_no_op_when_python_dotenv_is_unavailable(monkeypatch):
    # matches agent/orchestrator.py's _load_dotenv(): missing the optional
    # dependency degrades quietly rather than crashing the entrypoint.
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *a, **k):
        if name == "dotenv":
            raise ImportError("simulated: python-dotenv not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    target_main._load_dotenv()  # must not raise
