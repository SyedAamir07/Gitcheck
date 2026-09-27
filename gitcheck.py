#!/usr/bin/env python3
"""Entry point that works without installing the package: python gitcheck.py <command>."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gitcheck.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
