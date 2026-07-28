from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProjectConfigurationTests(unittest.TestCase):
    def test_async_driver_and_components_versions_are_locked(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = project["project"]["dependencies"]

        self.assertIn("discord.py>=2.6.0", dependencies)
        self.assertIn("pymongo>=4.13.0", dependencies)
        self.assertFalse(any(item.casefold().startswith("motor") for item in dependencies))

        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        packages = lock["package"]
        names = {package["name"] for package in packages}
        self.assertIn("pymongo", names)
        self.assertNotIn("motor", names)

        root_package = next(package for package in packages if package["name"] == "devilblox")
        requirements = {
            item["name"]: item.get("specifier")
            for item in root_package["metadata"]["requires-dist"]
        }
        self.assertEqual(requirements["discord-py"], ">=2.6.0")
        self.assertEqual(requirements["pymongo"], ">=4.13.0")

    def test_ci_uses_locked_uv_install_and_the_full_suite(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("uses: actions/checkout@v6", workflow)
        self.assertIn("uv sync --locked", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)


if __name__ == "__main__":
    unittest.main()
