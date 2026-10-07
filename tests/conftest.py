"""Tests use only synthetic accounts and loopback servers."""
import socket
import os
from pathlib import Path
import tempfile
import pytest


@pytest.fixture(autouse=True)
def private_windows_temporary_directories(monkeypatch):
    if os.name != 'nt':
        return
    from claude_chatgpt_bridge.platforms import protect_windows
    original = tempfile.TemporaryDirectory
    class PrivateDirectory(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            protect_windows(Path(self.name))
    monkeypatch.setattr(tempfile, 'TemporaryDirectory', PrivateDirectory)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    resolve = socket.getaddrinfo

    def allowed(address):
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError('Tests must not connect to external services.')

    def safe_connect(sock, address):
        allowed(address)
        return connect(sock, address)

    def safe_connect_ex(sock, address):
        allowed(address)
        return connect_ex(sock, address)

    def safe_resolve(host, *args, **kwargs):
        if host not in (None, '127.0.0.1', '::1', 'localhost'):
            raise AssertionError('Tests must not resolve external services.')
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'connect', safe_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', safe_connect_ex)
    monkeypatch.setattr(socket, 'getaddrinfo', safe_resolve)
