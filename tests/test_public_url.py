"""QR codes must open on a phone: never a loopback address, and scanned links reduce to the code."""
from urllib.parse import urlsplit

from flask import Flask

import public_url as pu


def test_configured_wifi_address_is_used(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://192.168.1.5:5003")
    assert pu.public_base_url() == "http://192.168.1.5:5003"
    link = pu.pickup_link("TNSTC2.abc")
    assert link == "http://192.168.1.5:5003/staff/claims-desk#pickup=TNSTC2.abc"
    assert pu.tag_link("TAG-ABCD2345").endswith("/staff/claims-desk#tag=TAG-ABCD2345")


def test_loopback_is_never_put_in_a_qr(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:5003")
    monkeypatch.setattr(pu, "lan_ip", lambda: "10.0.0.7")
    with Flask("t").test_request_context(base_url="http://localhost:5003"):
        assert pu.public_base_url() == "http://10.0.0.7:5003"


def test_the_request_address_is_used_when_it_is_reachable(monkeypatch):
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    with Flask("t").test_request_context(base_url="http://192.168.0.20:5003"):
        assert urlsplit(pu.public_base_url()).hostname == "192.168.0.20"


def test_scanned_links_reduce_to_the_code():
    assert pu.scanned_value("http://x:5003/staff/claims-desk#pickup=TNSTC2.abc", pu.PICKUP_PREFIX) == "TNSTC2.abc"
    assert pu.scanned_value("http://x:5003/staff/claims-desk#tag=TAG-ABCD2345", pu.TAG_PREFIX) == "TAG-ABCD2345"
    assert pu.scanned_value("A7X2-K9P4", pu.PICKUP_PREFIX) == "A7X2-K9P4"         # typed codes unchanged
    assert pu.scanned_value(None, pu.TAG_PREFIX) is None
