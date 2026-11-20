"""Test-wide setup.

CI runners (GITHUB_ACTIONS, FORCE_COLOR) make Rich emit ANSI colour codes, which split
the words CLI tests look for. Force plain output before any console is created.
"""

import os

for _var in ("FORCE_COLOR", "CLICOLOR_FORCE", "GITHUB_ACTIONS", "TTY_INTERACTIVE"):
    os.environ.pop(_var, None)
os.environ["NO_COLOR"] = "1"
os.environ["TTY_COMPATIBLE"] = "0"
os.environ["TERM"] = "dumb"
