#!/usr/bin/env python3
"""list.py — List all books in books-source/ with their status."""
import subprocess, sys
from pathlib import Path

subprocess.run(
    [sys.executable, Path(__file__).with_name("generate.py"), "--list"],
    check=False,
)
