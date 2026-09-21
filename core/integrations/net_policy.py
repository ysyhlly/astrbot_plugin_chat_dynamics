"""Shared host rules for optional loopback-capable HTTP integrations.

A configured integration may send a credential to a host. The one rule both
clients apply is that a key never travels in clear text to a remote host, and the
decision depends on what the hostname actually *is*, not on how it looks.
"""

from __future__ import annotations

import ipaddress


def is_loopback_host(hostname: str) -> bool:
    """True only for a literal local address or a `.localhost` name.

    A prefix test on "127." would also accept an attacker-chosen name such as
    "127.example.com", which resolves wherever its owner points it, so the
    address is parsed instead of pattern-matched. Unspecified addresses
    ("0.0.0.0", "::") name the local host on every stack and stay accepted, and
    RFC 6761 reserves the `.localhost` suffix for loopback, so that suffix stays
    accepted too.
    """
    host = str(hostname or "").strip().lower().strip("[]")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified
