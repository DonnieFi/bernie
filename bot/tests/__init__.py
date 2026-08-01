"""Test package bootstrap — isolates test log output from production.

THE PROBLEM: tests run via `docker exec family-bot python -m unittest tests.*`
inside the deployed container. When a test imports bot code, bot.py calls
logging.basicConfig() which attaches a FileHandler pointed at /data/bot.log
— the live production log. Test ERROR/WARNING lines then leak into the file
Watchman audits every night, and any expected-failure code path in
eval_service / claude_service / etc. shows up the next morning as a
"production failure" in Bernie's daily report.

THE FIX: this __init__.py runs before any test module imports (because
`python -m unittest tests.foo` resolves `tests.foo` by importing `tests`
first). It pre-configures the root logger so the subsequent
logging.basicConfig() call inside bot.py is a no-op (basicConfig only
attaches handlers when the root logger has none).

LIMITATIONS:
- Only protects test runs that go through `tests.*` import paths. Direct
  `python bot/foo.py` invocations still touch the prod log.
- One log file is shared across parallel test sessions, but unittest runs
  serially so this is fine in practice.
- The deeper fix (separate test container / image) is tracked in
  `notes/2026-05-21-test-isolation-side-quest.md`.

Captured 2026-05-21 after Bernie surfaced a false-positive "AI Evaluation
Service failed overnight" caused by test runs writing to /data/bot.log.
"""
import logging
import os
import tempfile
import unittest


def _validate_test_log_path(path: str) -> None:
    """Refuse paths under /data/ — the bootstrap exists specifically to
    keep test ERROR/WARNING lines out of /data/bot.log (the file Watchman
    audits every night). Without this guard, an operator-supplied
    BERNIE_TEST_LOG under /data/ would recreate the very leak the
    bootstrap was written to prevent."""
    if path.startswith(("/data/", "/data")):
        raise RuntimeError(
            f"BERNIE_TEST_LOG={path!r} points into /data/; refusing to "
            "write test logs alongside production bot.log"
        )


# Idempotent guard — if the test runner re-imports the package the handler
# stack doesn't keep growing.
if not getattr(logging, "_bernie_test_bootstrap_done", False):
    test_log_path = os.environ.get("BERNIE_TEST_LOG") or os.path.join(
        tempfile.gettempdir(), "bernie_test.log"
    )
    _validate_test_log_path(test_log_path)

    root = logging.getLogger()
    # Drop any handlers that snuck on (shouldn't be any this early, but be safe).
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    root.setLevel(logging.WARNING)
    fh = logging.FileHandler(test_log_path)
    fh.setLevel(logging.WARNING)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    root.addHandler(fh)
    # No StreamHandler — unittest captures stdout/stderr itself, and
    # assertLogs in individual tests still works because it operates on
    # specific named loggers, not the root handler list.

    logging._bernie_test_bootstrap_done = True

# Suppress real Discord HITL approval DMs during unittest runs. Tier-3 hold tests
# call _notify_admins_for_hold → send_hitl_approval_dms; without this, a leaked
# inline notifier or cross-container notify can DM admins on every tier-3 test.
os.environ.setdefault("BERNIE_DISABLE_HITL_DM", "1")

# Pre-commit runs inside bernie-api (ROLE=api). db_writes.routed then POSTs to
# cognition instead of the test temp DB, so create-then-expire style tests get 0
# rows. Force in-process writes whenever this test package is imported.
# Must overwrite compose ROLE=api (setdefault is not enough).
os.environ["ROLE"] = "monolith"

# Reset process singletons before each test so patches on ToolGateway / registry
# do not leak a MagicMock gateway into later tests (40B review hardening).
_original_testcase_run = unittest.TestCase.run


def _testcase_run_with_singleton_reset(self, result=None):
    try:
        from tool_gateway import reset_tool_gateway_for_tests

        reset_tool_gateway_for_tests()
    except Exception:
        pass
    try:
        from eval.audit import _clear_grounding_tools_cache

        _clear_grounding_tools_cache()
    except Exception:
        pass
    return _original_testcase_run(self, result)


unittest.TestCase.run = _testcase_run_with_singleton_reset
