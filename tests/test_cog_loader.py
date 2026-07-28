from __future__ import annotations

import unittest

from core.cog_loader import discover_extensions


class CogDiscoveryTests(unittest.TestCase):
    def test_discovers_cog_entrypoints_but_not_support_modules(self) -> None:
        extensions = discover_extensions("cogs")

        self.assertIn("cogs.cogs_vending_archive", extensions)
        self.assertNotIn("cogs.vending_views", extensions)
        self.assertTrue(
            all(name.rsplit(".", 1)[-1].startswith("cogs_") for name in extensions)
        )


if __name__ == "__main__":
    unittest.main()
