#!/usr/bin/env python3
"""codex_goals.py: list and brief OpenAI Codex /goal goals so Claude Code can resume them.

Read-only: it never writes to Codex's files. Python 3.9+, standard library only.
Run `codex_goals.py --help` for usage.
"""

import os
import sys

if sys.version_info < (3, 9):
    sys.stderr.write("codex_goals.py needs Python 3.9 or newer\n")
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from codex_goals.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
