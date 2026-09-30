#!/usr/bin/env python3
"""Isolated-mode launcher: only this runtime's directory is added to sys.path."""
import os
import sys
from pathlib import Path

os.umask(0o077)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_pushover.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
