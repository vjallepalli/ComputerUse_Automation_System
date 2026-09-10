"""CLI shim: `python run.py --goal "..."` -> agent.orchestrator.main.

The loop itself lives in agent/orchestrator.py (per CLAUDE.md's layout table);
`python -m agent` works too.
"""

import sys

from agent.orchestrator import main

if __name__ == "__main__":
    sys.exit(main())
