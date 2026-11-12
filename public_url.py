"""
The address a phone can open when it scans one of the app's QR codes.

A QR that points at http://127.0.0.1:5003 opens the *phone's own* 127.0.0.1, so
it never works. Order of preference:

  1. APP_BASE_URL, when it is set to something other than a loopback address
     (start.py sets it to this computer's Wi-Fi address);
  2. the address the current browser request came in on, when that is not
     loopback (someone opened the app via the Wi-Fi address already);
  3. this computer's LAN address and PORT.

Phones must be on the same Wi-Fi, and the app must listen on 0.0.0.0
(start.py does both).
"""
import os
import socket
from urllib.parse import urlsplit

from flask import has_request_context, request

LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0", None, ""}

# Scanned QR links carry their data after '#', which browsers never send to the server.
PICKUP_PREFIX = "#pickup="
TAG_PREFIX = "#tag="


def lan_ip() -> str:
    """This computer's address on the local network (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def public_base_url() -> str:
    configured = (os.environ.get("APP_BASE_URL") or "").strip().rstrip("/")
    if configured and urlsplit(configured).hostname not in LOOPBACK:
        return configured
    if has_request_context() and urlsplit(request.host_url).hostname not in LOOPBACK:
        return request.host_url.rstrip("/")
    scheme = "https" if os.environ.get("HTTPS_ADHOC") == "1" else "http"
    return f"{scheme}://{lan_ip()}:{os.environ.get('PORT', '5003')}"


def pickup_link(payload: str) -> str:
    """Pickup QR: opens the depot's claims desk with the code filled in."""
    return f"{public_base_url()}/staff/claims-desk{PICKUP_PREFIX}{payload}"


def tag_link(tag: str) -> str:
    """Item tag QR: opens the depot's claims desk with the item tag filled in."""
    return f"{public_base_url()}/staff/claims-desk{TAG_PREFIX}{tag}"


def scanned_value(text: str, prefix: str) -> str:
    """The code inside a scanned QR link (a USB scanner types the whole link)."""
    if isinstance(text, str) and prefix in text:
        return text.split(prefix, 1)[1].strip()
    return text
