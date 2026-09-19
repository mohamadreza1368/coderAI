"""
tests/test_url_ssrf.py - SSRF guard for fetch_url (P1 #7).

_is_url_safe used to check only the scheme and whether the hostname *string*
looked local/private, so a DNS name that resolves to a private/metadata address
(localtest.me -> 127.0.0.1) sailed through, and a 302 to 169.254.169.254 was
never re-checked. Now every resolved address is validated and every redirect
hop is re-validated.
"""

import socket
import urllib.parse

import pytest

from tools import _SSRFRedirectHandler, _is_url_safe, tool_fetch_url


def test_non_http_scheme_blocked():
    ok, reason = _is_url_safe("file:///etc/passwd")
    assert ok is False
    assert "scheme" in reason.lower()


def test_loopback_literal_blocked():
    ok, reason = _is_url_safe("http://127.0.0.1/admin")
    assert ok is False
    assert "loopback" in reason.lower()


def test_link_local_metadata_blocked():
    # The cloud instance-metadata endpoint.
    ok, reason = _is_url_safe("http://169.254.169.254/latest/meta-data/")
    assert ok is False
    assert "link-local" in reason.lower()


def test_private_ip_blocked():
    ok, reason = _is_url_safe("http://10.0.0.5/internal")
    assert ok is False
    assert "private" in reason.lower()


def test_hostname_resolving_to_loopback_is_blocked(monkeypatch):
    # The live vector: a *name* that getaddrinfo maps to 127.0.0.1.
    def fake_getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    ok, reason = _is_url_safe("http://localtest.me/")
    assert ok is False
    assert "127.0.0.1" in reason


def test_hostname_resolving_to_public_is_allowed(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    ok, reason = _is_url_safe("http://example.com/")
    assert ok is True
    assert reason == ""


def test_any_private_resolution_blocks_even_if_public_listed_first(monkeypatch):
    # A DNS rebinding style response: one public, one private. All must pass.
    def fake_getaddrinfo(host, port, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 0)),
        ]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    ok, reason = _is_url_safe("http://tricky.example/")
    assert ok is False


def test_unresolvable_hostname_blocked(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **k):
        raise socket.gaierror("name does not resolve")
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    ok, reason = _is_url_safe("http://does-not-exist.invalid/")
    assert ok is False
    assert "resolve" in reason.lower()


def test_redirect_handler_refuses_private_target(monkeypatch):
    # Patch _is_url_safe so the redirect target is treated as private/blocked
    # without needing real DNS.
    monkeypatch.setattr("tools._is_url_safe", lambda u: (False, "private"))
    handler = _SSRFRedirectHandler()

    base = urllib.parse.urlparse("http://public.example/page")
    orig_req = urllib.request.Request(base.geturl(), method="GET")

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        handler.redirect_request(
            orig_req, fp=None, code=302, msg="Found", headers={},
            newurl="http://169.254.169.254/latest/meta-data/",
        )
    assert excinfo.value.code == 302
    assert "blocked" in str(excinfo.value.msg).lower()


def test_fetch_url_blocks_before_connecting(monkeypatch):
    # If _is_url_safe says no, fetch_url must not attempt any network I/O.
    monkeypatch.setattr("tools._is_url_safe", lambda u: (False, "private"))
    def boom(*a, **k):
        raise AssertionError("network access attempted despite SSRF block")
    monkeypatch.setattr("urllib.request.build_opener", boom)
    out = tool_fetch_url("http://internal.example/secret")
    assert "Error" in out
    assert "private" in out.lower()
