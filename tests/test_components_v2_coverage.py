from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COGS = ROOT / "cogs"


def dotted_name(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class ComponentsV2CoverageTests(unittest.TestCase):
    def test_cogs_do_not_define_legacy_view_subclasses(self) -> None:
        offenders: list[str] = []
        for path in sorted(COGS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if any(dotted_name(base) == "discord.ui.View" for base in node.bases):
                    offenders.append(f"{path.name}:{node.lineno}:{node.name}")

        self.assertEqual(offenders, [], "legacy discord.ui.View subclasses remain")

    def test_no_call_combines_embed_with_an_interactive_view(self) -> None:
        offenders: list[str] = []
        for path in sorted(COGS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
                if not ({"embed", "embeds"} & keywords.keys()) or "view" not in keywords:
                    continue
                view_value = keywords["view"]
                if isinstance(view_value, ast.Constant) and view_value.value is None:
                    continue
                offenders.append(f"{path.name}:{node.lineno}")

        self.assertEqual(
            offenders,
            [],
            "Components V2 views cannot be sent together with legacy embeds",
        )


if __name__ == "__main__":
    unittest.main()
