"""Allowlist for Admin › Config personality editor (docs/ only).

Keeps deploy/ADR/archive trees off the dashboard; allows soul/bernie/person notes.
"""
from __future__ import annotations

import pathlib

# Always offered when present (behaviour pack / hot memory).
CORE_PERSONALITY_FILES: tuple[str, ...] = (
    "soul.md",
    "bernie.md",
    "family.md",
    "context.md",
    "capabilities_index.md",
    "capabilities.md",
    "USER_OVERRIDE.md",
)

# Top-level docs/*.md never exposed via the dashboard editor.
BLOCKED_TOP_LEVEL_DOCS: frozenset[str] = frozenset({
    "README.md",
    "deploy.md",
    "db-schema.md",
    "discord-onboarding.md",
    "google-oauth.md",
    "migration.md",
    "tech-modernization-plan.md",
    "testing-coordination.md",
    "USER_OVERRIDE.example.md",
})


def normalize_docs_rel(path: str) -> str:
    return path.strip().lstrip("/").replace("\\", "/")


def is_editable_personality_rel(rel: str) -> bool:
    """True if rel is a safe personality/family doc path under docs/."""
    if not rel or ".." in rel.split("/") or rel.startswith("/"):
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    name = parts[-1]
    if not name.endswith(".md") or name.startswith("."):
        return False

    if len(parts) == 1:
        if name in BLOCKED_TOP_LEVEL_DOCS:
            return False
        if name in CORE_PERSONALITY_FILES:
            return True
        # Person docs: dad.md, mom.md, …
        stem = name[:-3]
        if stem and stem[0].isalpha() and stem.replace("_", "").replace("-", "").isalnum():
            return True
        return False

    # docs/family/*.md — OSS generic pack; not family/README
    if len(parts) == 2 and parts[0] == "family":
        if name == "README.md":
            return False
        stem = name[:-3]
        return bool(stem) and stem.replace("_", "").replace("-", "").isalnum()

    return False


def list_editable_personality_files(root: pathlib.Path) -> list[str]:
    """Ordered list of editable relative paths that exist on disk."""
    found: list[str] = []
    seen: set[str] = set()

    def add(rel: str) -> None:
        if rel in seen or not is_editable_personality_rel(rel):
            return
        if (root / rel).is_file():
            found.append(rel)
            seen.add(rel)

    for name in CORE_PERSONALITY_FILES:
        add(name)
    if root.is_dir():
        for p in sorted(root.glob("*.md")):
            add(p.name)
        fam = root / "family"
        if fam.is_dir():
            for p in sorted(fam.glob("*.md")):
                add(f"family/{p.name}")
    return found
