"""Gate: no direct DB writes on discord/api paths (40A-5b)."""
import unittest

from db_write_audit import scan


class TestDbWriteStragglers(unittest.TestCase):
    def test_no_discord_api_write_stragglers(self):
        stragglers, _ = scan()
        self.assertEqual(
            stragglers,
            [],
            msg="\n".join(f"{rel}:{i} {fn}" for rel, i, fn, _ in stragglers),
        )


if __name__ == "__main__":
    unittest.main()
