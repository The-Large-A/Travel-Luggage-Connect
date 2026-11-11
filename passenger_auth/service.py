"""
Shared input parsing and messages for passenger authentication.

The original PNR + mobile OTP login (Feature 1's OtpService) was removed when
login became two layers — Aadhaar first, then the PNR (identity_service.py).
Layer 2 still parses and validates the PNR here.
"""
from .bookings import PnrFormatError, normalize_pnr
from .errors import AuthError
from .phone import PhoneFormatError, normalize_phone

INVALID_OTP_MESSAGE = "The OTP is incorrect or no longer valid. Please check it or request a new one."


def parse_pnr(raw) -> str:
    try:
        return normalize_pnr(raw)
    except PnrFormatError:
        raise AuthError(400, "invalid_request", "Please enter a valid PNR (e.g. T26100101).",
                        fields={"pnr": "invalid_format"})


def parse_phone(raw) -> str:
    try:
        return normalize_phone(raw)
    except PhoneFormatError:
        raise AuthError(400, "invalid_request", "Please enter a valid 10-digit Indian mobile number.",
                        fields={"phone": "invalid_format"})
