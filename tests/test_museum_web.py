from __future__ import annotations

import base64
import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MUSEUM_HTML = ROOT / "web" / "museum" / "index.html"
EXPECTED_NOTIFY_SOUND_BYTES = 8_301
EXPECTED_NOTIFY_SOUND_SHA256 = (
    "8F4D3150992CF86049B5A7BECA5D0E9343B78E8DD9243F6DBE4D03A310FFAEE1"
)


def _featured_events_source(html: str) -> str:
    match = re.search(
        r"\b(?:const|let|var)\s+FEATURED_EVENTS\s*=\s*\[(.*?)\n\s*\];",
        html,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("FEATURED_EVENTS JavaScript data was not found")
    return match.group(1)


def _embedded_mp3_bytes(html: str) -> bytes:
    direct_uri = re.search(
        r"data:audio/mpeg;base64,([A-Za-z0-9+/=]+)",
        html,
    )
    if direct_uri is not None:
        payload = direct_uri.group(1)
    else:
        payload_match = re.search(
            r"\bNOTIFY_SOUND_B64\s*=\s*[\"']([A-Za-z0-9+/=]+)[\"']",
            html,
        )
        if payload_match is None:
            raise AssertionError("embedded MP3 base64 payload was not found")
        if "data:audio/mpeg;base64," not in html:
            raise AssertionError("MP3 payload is not used as an audio/mpeg data URI")
        payload = payload_match.group(1)

    try:
        return base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise AssertionError("embedded MP3 payload is not valid base64") from exc


class MuseumWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = MUSEUM_HTML.read_text(encoding="utf-8")

    def test_document_is_html5_and_declares_utf8(self) -> None:
        self.assertRegex(
            self.html,
            re.compile(r"\A\s*<!doctype\s+html\s*>", re.IGNORECASE),
        )
        self.assertRegex(
            self.html,
            re.compile(
                r"<meta\b[^>]*\bcharset\s*=\s*[\"']?utf-8[\"']?[^>]*>",
                re.IGNORECASE,
            ),
        )

    def test_embedded_mp3_is_stable(self) -> None:
        audio = _embedded_mp3_bytes(self.html)
        self.assertEqual(len(audio), EXPECTED_NOTIFY_SOUND_BYTES)
        self.assertEqual(
            hashlib.sha256(audio).hexdigest().upper(),
            EXPECTED_NOTIFY_SOUND_SHA256,
        )

    def test_intro_autostarts_without_sound_ui_and_has_reading_time(self) -> None:
        self.assertNotIn('id="introStart"', self.html)
        self.assertNotIn('id="introSound"', self.html)
        self.assertNotIn("알림음 필수", self.html)
        self.assertIn("setSoundEnabled(true, true)", self.html)
        self.assertIn("playIntroSequence();", self.html)
        self.assertIn('document.addEventListener("pointerdown", unlockIntroAudio', self.html)
        self.assertIn('document.addEventListener("click", unlockIntroAudio', self.html)
        self.assertIn("messageHoldTime(msg)", self.html)
        self.assertRegex(
            self.html,
            re.compile(r"\bINTRO_SPEED\s*=.*?\:\s*4\.125\s*;"),
        )

    def test_featured_events_use_real_times_and_include_ddos_chat(self) -> None:
        events = _featured_events_source(self.html)

        self.assertRegex(
            events,
            re.compile(
                r"\b(?:time|timestamp|ts)\s*:\s*[\"']\d{2}:\d{2}:\d{2}(?:\s*KST)?[\"']"
            ),
        )
        self.assertRegex(
            events,
            re.compile(
                r"d\s*:\s*[\"']2026-07-22[\"'].*?디도스",
                re.DOTALL,
            ),
        )

    def test_discord_metadata_has_data_and_rendering_hooks(self) -> None:
        events = _featured_events_source(self.html)

        self.assertRegex(events, re.compile(r"\b(?:edited|editedAt)\s*:"))
        self.assertRegex(events, re.compile(r"\b(?:reply|replyTo)\s*:"))
        self.assertRegex(events, re.compile(r"\battachments?\s*:"))

        self.assertRegex(self.html, re.compile(r"\bmsg\.edited(?:At)?\b"))
        self.assertRegex(self.html, re.compile(r"\bmsg\.reply(?:To)?\b"))
        self.assertRegex(self.html, re.compile(r"\bmsg\.attachments?\b"))
        self.assertIn("discord-edited", self.html)
        self.assertIn("discord-reply", self.html)
        self.assertIn("discord-attachment", self.html)
        self.assertIn("수정됨", self.html)

    def test_intro_does_not_hardcode_admins_or_drop_old_messages(self) -> None:
        self.assertNotIn("ADMIN_NAMES", self.html)
        self.assertNotRegex(
            self.html,
            re.compile(r"introFeed\.children\.length\s*>\s*8"),
        )
        self.assertNotRegex(
            self.html,
            re.compile(r"introFeed\.removeChild\(\s*introFeed\.firstChild\s*\)"),
        )


if __name__ == "__main__":
    unittest.main()
