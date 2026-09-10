"""`python -m agent` -> the discovery orchestrator CLI."""

import sys

from agent.orchestrator import main

if __name__ == "__main__":
    sys.exit(main())
