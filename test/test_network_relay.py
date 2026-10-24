import hashlib
import socket
import socketserver
import threading

import pytest

from infermatrix_copilot.providers.network_relay import loopback_proxy, model_proxy


@pytest.fixture
def target():
    received = []
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            data = bytearray()
            while block := self.request.recv(8192):
                data.extend(block)
            received.append(bytes(data))
            self.request.sendall(hashlib.sha256(data).hexdigest().encode())
    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server.server_address, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def read_all(connection):
    chunks = []
    while data := connection.recv(4096):
        chunks.append(data)
    return b''.join(chunks)


def test_allowed_tunnel_preserves_large_binary_payload_and_half_close(tmp_path, target):
    address, received = target
    path = tmp_path / 'proxy.sock'
    payload = bytes(range(256)) * 8192
    with model_proxy(path, destinations={'model.test:443': address}):
        with loopback_proxy(path) as listener, socket.create_connection(listener, timeout=5) as client:
            client.sendall(b'CONNECT model.test:443 HTTP/1.1\r\nHost: model.test:443\r\n\r\n')
            assert client.recv(4096) == b'HTTP/1.1 200 Connection Established\r\n\r\n'
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            assert read_all(client) == hashlib.sha256(payload).hexdigest().encode()
    assert received == [payload]


@pytest.mark.parametrize('wire_request', [
    b'CONNECT other.test:443 HTTP/1.1\r\n\r\n',
    b'CONNECT model.test:80 HTTP/1.1\r\n\r\n',
    b'CONNECT model.test:443@other.test:443 HTTP/1.1\r\n\r\n',
    b'GET https://model.test/ HTTP/1.1\r\n\r\n',
    b'CONNECT model.test:443 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n',
    b'CONNECT model.test:443 HTTP/1.1\r\nContent-Length: 5\r\n\r\n',
])
def test_denied_routes_and_ambiguous_requests_never_connect(tmp_path, target, wire_request):
    address, received = target
    path = tmp_path / 'proxy.sock'
    with model_proxy(path, destinations={'model.test:443': address}):
        with loopback_proxy(path) as listener, socket.create_connection(listener, timeout=5) as client:
            client.sendall(wire_request)
            reply = read_all(client)
            assert reply.startswith((b'HTTP/1.1 400 ', b'HTTP/1.1 403 '))
    assert received == []


def test_closing_execution_revokes_established_tunnel(tmp_path, target):
    address, received = target
    path = tmp_path / 'proxy.sock'
    with model_proxy(path, destinations={'model.test:443': address}):
        with loopback_proxy(path) as listener:
            client = socket.create_connection(listener, timeout=5)
            client.sendall(b'CONNECT model.test:443 HTTP/1.1\r\n\r\n')
            assert client.recv(4096).startswith(b'HTTP/1.1 200 ')
        try:
            assert client.recv(4096) == b''
        finally:
            client.close()


def test_existing_socket_is_not_adopted_or_removed(tmp_path):
    path = tmp_path / 'proxy.sock'
    with socket.socket(socket.AF_UNIX) as owner:
        owner.bind(str(path))
        original = path.stat().st_ino
        with pytest.raises(OSError):
            with model_proxy(path, destinations={'model.test:443': ('127.0.0.1', 1)}):
                pytest.fail('adopted existing socket')
        assert path.stat().st_ino == original
