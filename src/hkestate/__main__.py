"""`python -m hkestate <command>`: the same as the `hkestate` command.

The scheduled tasks use this form because Windows Application Control (Device Guard) can
block the generated `hkestate.exe` launcher while still allowing `python.exe`.
"""

from hkestate.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
