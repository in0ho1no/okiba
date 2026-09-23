#!/usr/bin/env python3
"""
Unified entrypoint for Claude Code hooks.

This wrapper makes the hook set easier to copy to another repository by:
- resolving the repository root from the hook location
- switching the process working directory to the repository root
- dispatching to the existing pre/post hook implementation
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def find_repo_root(start_dir: Path) -> Path:
    current = start_dir
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return start_dir.parents[2]


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"pre", "post"}:
        print("Usage: entrypoint.py [pre|post]", file=sys.stderr)
        sys.exit(1)

    hook_dir = Path(__file__).resolve().parent
    repo_root = find_repo_root(hook_dir)
    os.chdir(repo_root)

    target = hook_dir / ("pre_tool_inspect.py" if sys.argv[1] == "pre" else "post_tool_inspect.py")
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()