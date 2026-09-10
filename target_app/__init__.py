"""Local Flask demo app standing in for a legacy bank back-office UI with no API.

The app factory is `target_app.app.create_app`; the runnable entrypoint is
`target_app.__main__` (`python -m target_app`). This package module deliberately
imports neither, so running `app.py` as __main__ can't collide with it.
"""
