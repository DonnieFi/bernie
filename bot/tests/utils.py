import os
import pathlib

# Layout-aware paths. In the container bot/ is mounted at /app (so this test
# lives at /app/tests/), with web/ at /app/web. On the host the tree is
# <repo>/bot/tests/ with web/ at <repo>/web. _BOT_DIR is the parent of tests/
_BOT_DIR = pathlib.Path(__file__).resolve().parent.parent

def _bot(*p):
    """Resolve path relative to bot directory."""
    return _BOT_DIR.joinpath(*p)

def _web(*p):
    """Resolve path relative to web directory."""
    here = _BOT_DIR.joinpath("web", *p)
    return here if here.exists() else _BOT_DIR.parent.joinpath("web", *p)

def _root(*p):
    """Resolve path relative to repo root (or /app in container).

    Set ``BERNIE_TEST_CONFIG`` to an absolute path to force which config.json
    tests read (preferred over filesystem heuristics).

    Without that env var, prefers a non-empty file at the bot/ location and
    falls back to repo root. This guards against stale empty artifacts (e.g.
    an empty bot/config.json left by a container side-effect on bernie-host).
    """
    if p == ("config.json",):
        explicit = os.environ.get("BERNIE_TEST_CONFIG")
        if explicit:
            return pathlib.Path(explicit)
    here = _BOT_DIR.joinpath(*p)
    if here.exists() and here.stat().st_size > 0:
        return here
    return _BOT_DIR.parent.joinpath(*p)
