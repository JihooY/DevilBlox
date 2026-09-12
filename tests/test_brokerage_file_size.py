
from __future__ import annotations

import unittest
from pathlib import Path

from cogs.cogs_brokerage import BrokerageCog


ROOT = Path(__file__).resolve().parents[1]
MAX_ALLOWED_LINES = 699


class BrokerageFileSizePolicyTests(unittest.TestCase):
    def test_every_brokerage_python_file_stays_below_700_lines(self) -> None:
        roots = ("cogs", "database", "services", "tests")
        paths = sorted(
            path
            for root_name in roots
            for path in (ROOT / root_name).rglob("*.py")
            if "brokerage" in path.relative_to(ROOT).as_posix().casefold()
        )
        offenders = {
            path.relative_to(ROOT).as_posix(): len(
                path.read_text(encoding="utf-8").splitlines()
            )
            for path in paths
            if len(path.read_text(encoding="utf-8").splitlines())
            > MAX_ALLOWED_LINES
        }
        self.assertEqual(offenders, {})

    def test_split_cog_keeps_every_brokerage_command_registered(self) -> None:
        expected = {
            "거래중개패널",
            "거래중개관리패널",
            "신용도조정",
            "거래삭제",
            "거래문제처리",
            "거래문제해결",
            "거래후기수정",
            "거래중개설정",
            "거래인증코드",
        }
        actual = {command.name for command in BrokerageCog.__cog_app_commands__}

        self.assertEqual(actual, expected)

    def test_every_brokerage_command_is_guild_only(self) -> None:
        self.assertTrue(BrokerageCog.__cog_app_commands__)
        self.assertTrue(
            all(command.guild_only for command in BrokerageCog.__cog_app_commands__)
        )


if __name__ == "__main__":
    unittest.main()
