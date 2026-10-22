#!/usr/bin/env python3
"""Compatibility wrapper for the installed target-load command."""

from shardferry.commands.target_load import main


if __name__ == "__main__":
    raise SystemExit(main())
