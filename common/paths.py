"""
common/paths.py

One source of truth for paths in the repo.

This file exists because of v1 bug #3: `DB_PATH = "data/trials.db"` was a
relative path, so running a script from any directory other than the repo
root silently created a *second, empty* database instead of failing. Nothing
crashed. Nothing logged. The script just worked against the wrong file.

The fix is to anchor every path to THIS file's location, never the working
directory, and to do it once here rather than in each module. v3 has no
local data, so the only path left is the repo root (used to find `.env`).
"""

from __future__ import annotations

from pathlib import Path

# common/paths.py -> common/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent


if __name__ == "__main__":
    # Sanity check: run this from anywhere and the output should be identical.
    #
    #   cd / && python /path/to/repo/common/paths.py
    #   cd /path/to/repo && python common/paths.py
    import os

    print(f"cwd:        {os.getcwd()}")
    print(f"REPO_ROOT:  {REPO_ROOT}")
