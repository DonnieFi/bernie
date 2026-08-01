"""Shared pytest fixtures.

The project's test suite uses unittest.IsolatedAsyncioTestCase, so most tests
do not import these fixtures. This file is kept as a stub for forward
compatibility when pytest_asyncio gets added.
"""

import os
import sys
import tempfile

# Ensure bot/ is on sys.path for any test that imports project modules.
_HERE = os.path.dirname(__file__)
_BOT_DIR = os.path.abspath(os.path.join(_HERE, ".."))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)


def make_temp_identity_db():
    """Create a temp directory and return a path for a throwaway DB.

    Helper used by unittest-based tests in test_identity.py. Returns
    (TemporaryDirectory handle, db_path) — caller is responsible for cleanup.
    """
    tmpdir = tempfile.TemporaryDirectory()
    return tmpdir, os.path.join(tmpdir.name, "test_identity.db")
