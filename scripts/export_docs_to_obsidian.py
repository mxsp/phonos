#!/usr/bin/env python3
"""Export agent docs to Obsidian-friendly markdown."""

import os
from pathlib import Path

OBSIDIAN_VAULT = os.environ.get('OBSIDIAN_VAULT', './obsidian-export')
SRC_DOCS = [
    'AGENTS.md',
    'CLAUDE.md',
    '.claude/skills/explore-codebase.md',
    '.claude/skills/review-changes.md',
    '.claude/skills/refactor-safely.md',
    '.claude/skills/debug-issue.md',
]

def export():
    Path(OBSIDIAN_VAULT).mkdir(exist_ok=True)
    for doc in SRC_DOCS:
        src = Path(doc)
        if src.exists():
            dst = Path(OBSIDIAN_VAULT) / src.name
            dst.write_text(src.read_text())
            print(f"Exported: {doc} -> {dst}")

if __name__ == '__main__':
    export()