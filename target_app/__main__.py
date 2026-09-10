"""Runnable entrypoint: `python -m target_app`.

Kept separate from app.py so importing the package (which re-exports
create_app) doesn't collide with running app.py as __main__.
"""

import os

from target_app.app import create_app


def main() -> None:
    # Port 5000 is often taken on macOS (Control Center / AirPlay Receiver),
    # so default to 5001. Override with TARGET_APP_PORT.
    port = int(os.environ.get("TARGET_APP_PORT") or 5001)
    create_app().run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
