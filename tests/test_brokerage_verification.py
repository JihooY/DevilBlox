from __future__ import annotations

import asyncio
import base64
from email.message import EmailMessage
from urllib import parse
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services import brokerage_verification as verification


SMTP_ENV = {
    "BROKERAGE_SMTP_HOST": "smtp.example.com",
    "BROKERAGE_SMTP_PORT": "587",
    "BROKERAGE_SMTP_USERNAME": "brokerage-bot",
    "BROKERAGE_SMTP_PASSWORD": "smtp-secret-value",
    "BROKERAGE_SMTP_FROM": "verify@example.com",
    "BROKERAGE_SMTP_USE_TLS": "true",
}

TWILIO_ENV = {
    "BROKERAGE_TWILIO_ACCOUNT_SID": "AC" + ("0" * 32),
    "BROKERAGE_TWILIO_AUTH_TOKEN": "twilio-secret-value",
    "BROKERAGE_TWILIO_FROM_NUMBER": "+15551234567",
}


async def _run_to_thread_inline(function, *args, **kwargs):
    """Deterministically run a to_thread target after recording its use."""

    return function(*args, **kwargs)


class AddressValidationTests(unittest.TestCase):
    def test_email_is_normalized_and_masked(self):
        self.assertEqual(
            verification.normalize_email("  User.Name@EXAMPLE.COM  "),
            "User.Name@example.com",
        )
        self.assertEqual(
            verification.mask_email("User.Name@EXAMPLE.COM"),
            "U***e@e***.com",
        )
        self.assertEqual(
            verification.normalize_email("user@bücher.de"),
            "user@xn--bcher-kva.de",
        )

    def test_invalid_or_header_unsafe_email_is_rejected(self):
        invalid_addresses = (
            "user@example",
            ".user@example.com",
            "user..name@example.com",
            "User <user@example.com>",
            "user@example.com\nBcc: target@example.com",
            "user@example.com\r",
            "usér@example.com",
        )
        for address in invalid_addresses:
            with self.subTest(address=address):
                with self.assertRaises(verification.VerificationValidationError):
                    verification.normalize_email(address)

    def test_phone_is_normalized_as_e164_and_masked(self):
        self.assertEqual(
            verification.normalize_phone("  +65 (9123) 4567  "),
            "+6591234567",
        )
        self.assertEqual(verification.mask_phone("+65 9123 4567"), "+******4567")
        self.assertEqual(
            verification.normalize_e164_phone("+1-555-123-4567"),
            "+15551234567",
        )

    def test_non_e164_or_unsafe_phone_is_rejected(self):
        invalid_numbers = (
            "6591234567",
            "+0123456789",
            "+1 555 123 4567 ext 1",
            "+15551234567\n",
            "+１５５５１２３４５６７",
            "+1234567890123456",
        )
        for number in invalid_numbers:
            with self.subTest(number=number):
                with self.assertRaises(verification.VerificationValidationError):
                    verification.normalize_phone(number)


class VerificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_email_delivery_uses_smtp_tls_inside_to_thread(self):
        smtp_context = MagicMock()
        smtp = smtp_context.__enter__.return_value

        with (
            patch.object(verification.smtplib, "SMTP", return_value=smtp_context) as smtp_class,
            patch.object(
                verification.asyncio,
                "to_thread",
                new=AsyncMock(side_effect=_run_to_thread_inline),
            ) as to_thread,
        ):
            masked = await verification.send_email_code(
                " Buyer@EXAMPLE.COM ",
                "654321",
                env=SMTP_ENV,
            )

        self.assertEqual(masked, "B***r@e***.com")
        self.assertNotIn("654321", masked)
        self.assertNotIn(SMTP_ENV["BROKERAGE_SMTP_PASSWORD"], masked)
        to_thread.assert_awaited_once()
        self.assertIs(to_thread.await_args.args[0], verification._attempt_delivery)
        smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=15)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("brokerage-bot", "smtp-secret-value")
        smtp.send_message.assert_called_once()
        message = smtp.send_message.call_args.args[0]
        self.assertIsInstance(message, EmailMessage)
        self.assertEqual(message["To"], "Buyer@example.com")
        self.assertEqual(message["From"], "verify@example.com")
        self.assertIn("654321", message.get_content())

    async def test_email_without_tls_does_not_start_tls(self):
        smtp_context = MagicMock()
        smtp = smtp_context.__enter__.return_value
        environment = {**SMTP_ENV, "BROKERAGE_SMTP_USE_TLS": "off"}

        with (
            patch.object(verification.smtplib, "SMTP", return_value=smtp_context),
            patch.object(
                verification.asyncio,
                "to_thread",
                new=AsyncMock(side_effect=_run_to_thread_inline),
            ),
        ):
            await verification.send_email_code("buyer@example.com", "ABCD12", env=environment)

        smtp.starttls.assert_not_called()
        smtp.login.assert_called_once()

    async def test_invalid_smtp_configuration_fails_before_thread_or_network(self):
        environment = {key: value for key, value in SMTP_ENV.items() if key != "BROKERAGE_SMTP_HOST"}
        with patch.object(verification.asyncio, "to_thread", new=AsyncMock()) as to_thread:
            with self.assertRaises(verification.VerificationConfigurationError) as raised:
                await verification.send_email_code("buyer@example.com", "654321", env=environment)

        to_thread.assert_not_awaited()
        self.assertNotIn("smtp-secret-value", str(raised.exception))
        self.assertNotIn("654321", str(raised.exception))

    async def test_smtp_failure_is_sanitized(self):
        smtp_context = MagicMock()
        smtp_context.__enter__.side_effect = OSError(
            "smtp-secret-value for buyer@example.com with 654321"
        )

        with (
            patch.object(verification.smtplib, "SMTP", return_value=smtp_context),
            patch.object(
                verification.asyncio,
                "to_thread",
                new=AsyncMock(side_effect=_run_to_thread_inline),
            ),
        ):
            with self.assertRaises(verification.VerificationDeliveryError) as raised:
                await verification.send_email_code(
                    "buyer@example.com",
                    "654321",
                    env=SMTP_ENV,
                )

        rendered = str(raised.exception)
        self.assertEqual(rendered, "email verification delivery failed")
        self.assertNotIn("smtp-secret-value", rendered)
        self.assertNotIn("buyer@example.com", rendered)
        self.assertNotIn("654321", rendered)
        self.assertIsNone(raised.exception.__context__)

    async def test_phone_delivery_posts_twilio_form_inside_to_thread(self):
        response_context = MagicMock()
        response_context.__enter__.return_value.status = 201

        with (
            patch.object(verification.request, "urlopen", return_value=response_context) as urlopen,
            patch.object(
                verification.asyncio,
                "to_thread",
                new=AsyncMock(side_effect=_run_to_thread_inline),
            ) as to_thread,
        ):
            masked = await verification.send_phone_code(
                "+65 9123 4567",
                "654321",
                env=TWILIO_ENV,
            )

        self.assertEqual(masked, "+******4567")
        self.assertNotIn("654321", masked)
        self.assertNotIn(TWILIO_ENV["BROKERAGE_TWILIO_AUTH_TOKEN"], masked)
        to_thread.assert_awaited_once()
        self.assertIs(to_thread.await_args.args[0], verification._attempt_delivery)

        outgoing = urlopen.call_args.args[0]
        self.assertEqual(outgoing.get_method(), "POST")
        self.assertIn(TWILIO_ENV["BROKERAGE_TWILIO_ACCOUNT_SID"], outgoing.full_url)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 15})
        form = parse.parse_qs(outgoing.data.decode("ascii"))
        self.assertEqual(form["To"], ["+6591234567"])
        self.assertEqual(form["From"], ["+15551234567"])
        self.assertEqual(form["Body"], ["Your DevilBlox verification code is: 654321"])
        expected_auth = base64.b64encode(
            (
                f"{TWILIO_ENV['BROKERAGE_TWILIO_ACCOUNT_SID']}:"
                f"{TWILIO_ENV['BROKERAGE_TWILIO_AUTH_TOKEN']}"
            ).encode("utf-8")
        ).decode("ascii")
        self.assertEqual(outgoing.get_header("Authorization"), f"Basic {expected_auth}")

    async def test_twilio_failure_is_sanitized(self):
        response_context = MagicMock()
        response_context.__enter__.return_value.status = 401

        with (
            patch.object(verification.request, "urlopen", return_value=response_context),
            patch.object(
                verification.asyncio,
                "to_thread",
                new=AsyncMock(side_effect=_run_to_thread_inline),
            ),
        ):
            with self.assertRaises(verification.VerificationDeliveryError) as raised:
                await verification.send_phone_code(
                    "+6591234567",
                    "654321",
                    env=TWILIO_ENV,
                )

        rendered = str(raised.exception)
        self.assertEqual(rendered, "phone verification delivery failed")
        self.assertNotIn("twilio-secret-value", rendered)
        self.assertNotIn("654321", rendered)
        self.assertNotIn("+6591234567", rendered)
        self.assertIsNone(raised.exception.__context__)

    async def test_invalid_code_never_reaches_a_provider(self):
        with patch.object(verification.asyncio, "to_thread", new=AsyncMock()) as to_thread:
            with self.assertRaises(verification.VerificationValidationError):
                await verification.send_phone_code(
                    "+6591234567",
                    "123\n456",
                    env=TWILIO_ENV,
                )
        to_thread.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
