"""Delivery adapters for brokerage email and phone verification codes.

Code generation, expiry, hashing, and persistence deliberately live outside
this module.  The helpers here only validate a destination and deliver an
already-generated code using credentials supplied through the environment.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
import os
import re
import smtplib
import ssl
from urllib import parse, request


__all__ = [
    "ConfigurationError",
    "DeliveryError",
    "VerificationConfigurationError",
    "VerificationDeliveryError",
    "VerificationValidationError",
    "mask_email",
    "mask_e164_phone",
    "mask_phone",
    "normalize_email",
    "normalize_e164_phone",
    "normalize_phone",
    "send_email_code",
    "send_phone_code",
]


_EMAIL_LOCAL_PART = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z",
    re.ASCII,
)
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z", re.ASCII)
_E164_PHONE = re.compile(r"\+[1-9][0-9]{1,14}\Z", re.ASCII)
_FORMATTED_PHONE = re.compile(r"\+[0-9 ().-]+\Z", re.ASCII)
_VERIFICATION_CODE = re.compile(r"[A-Za-z0-9]{4,12}\Z", re.ASCII)
_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off"})
_NETWORK_TIMEOUT_SECONDS = 15


class VerificationConfigurationError(RuntimeError):
    """Raised when a delivery provider is missing or has invalid settings."""


class VerificationDeliveryError(RuntimeError):
    """Raised when a configured provider cannot deliver a verification code."""


class VerificationValidationError(ValueError):
    """Raised when a destination or verification code is invalid."""


# Short aliases are convenient for callers while the longer names remain
# unambiguous when several services are imported together.
ConfigurationError = VerificationConfigurationError
DeliveryError = VerificationDeliveryError


@dataclass(frozen=True, slots=True, repr=False)
class _SMTPConfig:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    use_tls: bool


@dataclass(frozen=True, slots=True, repr=False)
class _TwilioConfig:
    account_sid: str
    auth_token: str
    from_number: str


def _contains_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def normalize_email(value: str) -> str:
    """Return a canonical, ASCII SMTP address or raise a validation error.

    Surrounding whitespace is removed, the domain is IDNA encoded and
    lower-cased, and the local part keeps its original case.  Display names,
    comments, quoted local parts, address literals, and control characters are
    intentionally rejected so the result is safe to place in mail headers.
    """

    if not isinstance(value, str):
        raise VerificationValidationError("email address must be a string")

    if _contains_control_characters(value):
        raise VerificationValidationError("email address is invalid")
    candidate = value.strip()
    if not candidate:
        raise VerificationValidationError("email address is invalid")
    if candidate.count("@") != 1:
        raise VerificationValidationError("email address is invalid")

    local_part, unicode_domain = candidate.rsplit("@", 1)
    if len(local_part.encode("utf-8")) > 64 or not _EMAIL_LOCAL_PART.fullmatch(local_part):
        raise VerificationValidationError("email address is invalid")

    try:
        domain = unicode_domain.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise VerificationValidationError("email address is invalid") from None

    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > 253:
        raise VerificationValidationError("email address is invalid")
    if any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise VerificationValidationError("email address is invalid")

    normalized = f"{local_part}@{domain}"
    if len(normalized) > 254:
        raise VerificationValidationError("email address is invalid")
    return normalized


def mask_email(value: str) -> str:
    """Return a privacy-preserving representation of a valid email address."""

    email = normalize_email(value)
    local_part, domain = email.rsplit("@", 1)
    if len(local_part) == 1:
        masked_local = "*"
    elif len(local_part) == 2:
        masked_local = f"{local_part[0]}*"
    else:
        masked_local = f"{local_part[0]}***{local_part[-1]}"

    domain_labels = domain.split(".")
    first_label = domain_labels[0]
    masked_domain = f"{first_label[0]}***.{'.'.join(domain_labels[1:])}"
    return f"{masked_local}@{masked_domain}"


def normalize_phone(value: str) -> str:
    """Return a phone number in E.164 form.

    A leading ``+`` is mandatory.  Spaces, parentheses, periods, and hyphens
    are accepted as display formatting and removed; all other characters,
    national-only numbers, extensions, and more than 15 digits are rejected.
    """

    if not isinstance(value, str):
        raise VerificationValidationError("phone number must be a string")

    if _contains_control_characters(value):
        raise VerificationValidationError("phone number must use E.164 format")
    candidate = value.strip()
    if not candidate:
        raise VerificationValidationError("phone number must use E.164 format")
    if not _FORMATTED_PHONE.fullmatch(candidate):
        raise VerificationValidationError("phone number must use E.164 format")

    normalized = re.sub(r"[ ().-]", "", candidate)
    if not _E164_PHONE.fullmatch(normalized):
        raise VerificationValidationError("phone number must use E.164 format")
    return normalized


def mask_phone(value: str) -> str:
    """Return an E.164 phone number with only its last four digits visible."""

    phone = normalize_phone(value)
    digits = phone[1:]
    if len(digits) <= 4:
        return "+" + ("*" * len(digits))
    return "+" + ("*" * (len(digits) - 4)) + digits[-4:]


# Explicit aliases make the accepted phone format discoverable at call sites.
normalize_e164_phone = normalize_phone
mask_e164_phone = mask_phone


def _validate_code(value: str) -> str:
    if not isinstance(value, str) or not _VERIFICATION_CODE.fullmatch(value):
        raise VerificationValidationError(
            "verification code must contain 4 to 12 ASCII letters or digits"
        )
    return value


def _required_setting(environment: Mapping[str, str], name: str, *, preserve: bool = False) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip() or _contains_control_characters(value):
        raise VerificationConfigurationError(f"missing or invalid setting: {name}")
    return value if preserve else value.strip()


def _parse_boolean_setting(environment: Mapping[str, str], name: str) -> bool:
    value = _required_setting(environment, name).casefold()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise VerificationConfigurationError(f"invalid boolean setting: {name}")


def _load_smtp_config(environment: Mapping[str, str]) -> _SMTPConfig:
    host = _required_setting(environment, "BROKERAGE_SMTP_HOST")
    port_value = _required_setting(environment, "BROKERAGE_SMTP_PORT")
    username = _required_setting(environment, "BROKERAGE_SMTP_USERNAME")
    password = _required_setting(environment, "BROKERAGE_SMTP_PASSWORD", preserve=True)
    from_value = _required_setting(environment, "BROKERAGE_SMTP_FROM")
    use_tls = _parse_boolean_setting(environment, "BROKERAGE_SMTP_USE_TLS")

    try:
        port = int(port_value, 10)
    except ValueError:
        raise VerificationConfigurationError(
            "invalid integer setting: BROKERAGE_SMTP_PORT"
        ) from None
    if not 1 <= port <= 65535:
        raise VerificationConfigurationError("BROKERAGE_SMTP_PORT is outside the valid range")

    try:
        from_address = normalize_email(from_value)
    except VerificationValidationError:
        raise VerificationConfigurationError("invalid setting: BROKERAGE_SMTP_FROM") from None

    return _SMTPConfig(host, port, username, password, from_address, use_tls)


def _load_twilio_config(environment: Mapping[str, str]) -> _TwilioConfig:
    account_sid = _required_setting(environment, "BROKERAGE_TWILIO_ACCOUNT_SID")
    auth_token = _required_setting(
        environment,
        "BROKERAGE_TWILIO_AUTH_TOKEN",
        preserve=True,
    )
    from_value = _required_setting(environment, "BROKERAGE_TWILIO_FROM_NUMBER")
    try:
        from_number = normalize_phone(from_value)
    except VerificationValidationError:
        raise VerificationConfigurationError(
            "invalid setting: BROKERAGE_TWILIO_FROM_NUMBER"
        ) from None
    return _TwilioConfig(account_sid, auth_token, from_number)


def _deliver_email(config: _SMTPConfig, destination: str, code: str) -> None:
    message = EmailMessage()
    message["Subject"] = "DevilBlox verification code"
    message["From"] = config.from_address
    message["To"] = destination
    message.set_content(
        "Use the following code to verify your DevilBlox brokerage account:\n\n"
        f"{code}\n\n"
        "If you did not request this code, you can ignore this message."
    )

    with smtplib.SMTP(config.host, config.port, timeout=_NETWORK_TIMEOUT_SECONDS) as smtp:
        if config.use_tls:
            smtp.starttls(context=ssl.create_default_context())
        smtp.login(config.username, config.password)
        smtp.send_message(message)


def _deliver_phone(config: _TwilioConfig, destination: str, code: str) -> None:
    account_path = parse.quote(config.account_sid, safe="")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_path}/Messages.json"
    form_data = parse.urlencode(
        {
            "To": destination,
            "From": config.from_number,
            "Body": f"Your DevilBlox verification code is: {code}",
        }
    ).encode("ascii")
    credentials = f"{config.account_sid}:{config.auth_token}".encode("utf-8")
    authorization = base64.b64encode(credentials).decode("ascii")
    outgoing = request.Request(
        url,
        data=form_data,
        headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    with request.urlopen(outgoing, timeout=_NETWORK_TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if status is None or not 200 <= int(status) < 300:
            raise OSError("Twilio returned an unsuccessful status")


def _attempt_delivery(delivery, *args: object) -> bool:
    """Run a provider call without allowing its exception data to escape."""

    try:
        delivery(*args)
    except Exception:
        return False
    return True


async def send_email_code(
    destination: str,
    code: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Deliver ``code`` by SMTP and return only the masked email address."""

    normalized_destination = normalize_email(destination)
    validated_code = _validate_code(code)
    config = _load_smtp_config(os.environ if env is None else env)
    to_thread_failed = False
    try:
        delivered = await asyncio.to_thread(
            _attempt_delivery,
            _deliver_email,
            config,
            normalized_destination,
            validated_code,
        )
    except Exception:
        # Keep the provider/thread exception out of the exception eventually
        # exposed to callers.  Some provider exceptions include request data.
        delivered = False
        to_thread_failed = True
    if not delivered:
        error = VerificationDeliveryError("email verification delivery failed")
        if to_thread_failed:
            error.__context__ = None
        raise error
    return mask_email(normalized_destination)


async def send_phone_code(
    destination: str,
    code: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Deliver ``code`` through Twilio and return only the masked phone number."""

    normalized_destination = normalize_phone(destination)
    validated_code = _validate_code(code)
    config = _load_twilio_config(os.environ if env is None else env)
    to_thread_failed = False
    try:
        delivered = await asyncio.to_thread(
            _attempt_delivery,
            _deliver_phone,
            config,
            normalized_destination,
            validated_code,
        )
    except Exception:
        delivered = False
        to_thread_failed = True
    if not delivered:
        error = VerificationDeliveryError("phone verification delivery failed")
        if to_thread_failed:
            error.__context__ = None
        raise error
    return mask_phone(normalized_destination)
