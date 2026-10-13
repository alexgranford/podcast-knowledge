#!/usr/bin/env python3
"""Public-HTTPS validation and DNS-pinned connections."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse


class NetworkSecurityError(RuntimeError):
    pass


class NetworkTransientError(RuntimeError):
    pass


def public_endpoint(url: str) -> tuple[urllib.parse.ParseResult, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise NetworkSecurityError("URL must use HTTPS")
    if parsed.username or parsed.password:
        raise NetworkSecurityError("Credentials in URLs are not allowed")
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise NetworkTransientError(f"DNS lookup failed: {parsed.hostname}") from error
    if not addresses:
        raise NetworkTransientError(f"Host resolved to no addresses: {parsed.hostname}")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        if not ip.is_global:
            raise NetworkSecurityError("Host resolved to a non-public address")
    return parsed, addresses[0][4][0].split("%", 1)[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection pinned to an address already approved by validation."""

    def __init__(self, hostname: str, address: str, port: int, timeout: int):
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._approved_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._approved_address, self.port), self.timeout, self.source_address,
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
