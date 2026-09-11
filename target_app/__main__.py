"""Runnable entrypoint: `python -m target_app`.

Kept separate from app.py so importing the package (which re-exports
create_app) doesn't collide with running app.py as __main__.
"""

import os

from target_app.app import create_app


def _load_dotenv() -> None:
    # BUG (found via a fresh-clone setup test): this entrypoint read
    # TARGET_APP_PORT straight from os.environ with no load_dotenv() call, so a
    # value set ONLY in .env (not exported in the shell) was silently ignored --
    # unlike agent/orchestrator.py and replay/cli.py, which already load .env
    # before reading their env vars. create_app() (below) reads
    # TARGET_APP_USERNAME / TARGET_APP_PASSWORD the same unguarded way and had
    # the identical bug, just masked because the demo defaults happen to match
    # .env.example. Same fix, same pattern as the other two entrypoints.
    # Regression: tests/target_app/test_env_loading.py.
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def main() -> None:
    _load_dotenv()
    # Port 5000 is often taken on macOS (Control Center / AirPlay Receiver),
    # so default to 5001. Override with TARGET_APP_PORT (.env or the shell).
    port = int(os.environ.get("TARGET_APP_PORT") or 5001)
    create_app().run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
