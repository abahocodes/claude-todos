"""Network defaults for every connector.

Python's urllib and imaplib try resolved addresses in order and wait out each connect timeout,
so on a network with broken IPv6 every request stalls ~30s per IPv6 address before falling back
(measured: 3+ minutes for one Google Tasks sync). Ordering IPv4 first keeps syncs fast either way.
"""
import socket

_orig_getaddrinfo = socket.getaddrinfo
_installed = False


def _ipv4_first(*args, **kwargs):
    infos = _orig_getaddrinfo(*args, **kwargs)
    return sorted(infos, key=lambda i: 0 if i[0] == socket.AF_INET else 1)


def prefer_ipv4() -> None:
    global _installed
    if not _installed:
        socket.getaddrinfo = _ipv4_first
        _installed = True
