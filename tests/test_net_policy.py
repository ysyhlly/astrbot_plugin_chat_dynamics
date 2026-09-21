"""The shared host rule: what counts as "the local host" is decided by parsing."""

import pytest

from astrbot_plugin_chat_dynamics.core.integrations.net_policy import is_loopback_host


@pytest.mark.parametrize("hostname", [
    "127.0.0.1", "127.2.3.4", "::1", "[::1]", "localhost", "api.localhost", "LOCALHOST",
    # Unspecified addresses name the local host on every stack.
    "0.0.0.0", "::",
])
def test_local_addresses_and_localhost_names_are_local(hostname):
    assert is_loopback_host(hostname)


@pytest.mark.parametrize("hostname", [
    # A name that merely starts like a loopback literal resolves wherever its
    # owner points it, which is exactly why the address is parsed.
    "127.example.com", "localhost.example.com", "example.com", "1.1.1.1", "10.0.0.1",
    "", "   ",
])
def test_remote_names_and_addresses_are_not_local(hostname):
    assert not is_loopback_host(hostname)
