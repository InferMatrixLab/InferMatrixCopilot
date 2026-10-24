"""Per-execution CONNECT routing over a Unix socket, without model credentials.

The container-side listener exposes only loopback. The host-side listener maps
exact authorities to trusted destinations; request bytes never select a new
address. TLS and native authentication remain end-to-end. This controls network
destinations, not provider-side tools or paths inside an encrypted connection.
"""
from __future__ import annotations

from contextlib import contextmanager
import select
import socket
import socketserver
import threading


def _tunnel(left, right, stopped):
    sockets = (left, right)
    peer = {left: right, right: left}
    pending = {left: bytearray(), right: bytearray()}
    reading, closed_write = set(sockets), set()
    for connection in sockets:
        connection.setblocking(False)
    while not stopped.is_set() and (reading or any(pending.values())):
        readers = [s for s in reading if len(pending[peer[s]]) < 65536]
        writers = [s for s in sockets if pending[s]]
        ready, writable, _ = select.select(readers, writers, [], .1)
        for connection in ready:
            try:
                data = connection.recv(65536 - len(pending[peer[connection]]))
            except BlockingIOError:
                continue
            if data:
                pending[peer[connection]].extend(data)
            else:
                reading.remove(connection)
        for connection in writable:
            try:
                sent = connection.send(pending[connection])
            except BlockingIOError:
                continue
            if not sent:
                raise ConnectionError('relay closed during write')
            del pending[connection][:sent]
        for connection in sockets:
            if peer[connection] not in reading and not pending[connection] and connection not in closed_write:
                connection.shutdown(socket.SHUT_WR)
                closed_write.add(connection)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        server.track(self.request)
        upstream = None
        try:
            self.request.settimeout(5)
            upstream = server.connect(self.request)
            if upstream is not None:
                server.track(upstream)
                _tunnel(self.request, upstream, server.stopped)
        except (OSError, ValueError):
            # Protocol failures terminate only this connection. Never log request
            # contents: native clients may put credentials in HTTP headers.
            pass
        finally:
            for connection in (self.request, upstream):
                if connection is not None:
                    server.untrack(connection)
                    connection.close()


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, connect, max_connections):
        if type(max_connections) is not int or max_connections < 1:
            raise ValueError('connection limit must be positive')
        self.connect = connect
        self.stopped = threading.Event()
        self.active = set()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, _Handler)

    def track(self, connection):
        with self.lock:
            if self.stopped.is_set():
                connection.close()
                raise ConnectionError('relay closed')
            self.active.add(connection)

    def untrack(self, connection):
        with self.lock:
            self.active.discard(connection)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def close_connections(self):
        with self.lock:
            self.stopped.set()
            for connection in self.active:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class _UnixServer(_Server):
    address_family = socket.AF_UNIX


@contextmanager
def _serving(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.close_connections()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def unix_service(socket_path, connect, *, max_connections=8):
    """Bound Unix service lifetime and connections for host-selected protocols."""
    with _serving(_UnixServer(str(socket_path), connect, max_connections)) as address:
        yield address


@contextmanager
def model_proxy(socket_path, *, destinations, max_connections=8):
    """Serve a fixed authority -> (host, port) map on a private Unix socket.

    The host supplies destinations, filesystem permissions and per-execution
    lifetime. Existing sockets are never unlinked or adopted implicitly.
    """
    routes = {authority: tuple(target) for authority, target in destinations.items()}
    if not routes or any(not isinstance(k, str) or k != k.lower() or ':' not in k
                         or any(c.isspace() for c in k) for k in routes):
        raise ValueError('explicit lowercase CONNECT authorities are required')
    if any(len(target) != 2 or not isinstance(target[0], str)
           or type(target[1]) is not int or not 1 <= target[1] <= 65535 for target in routes.values()):
        raise ValueError('explicit host and port targets are required')
    if max_connections < 1:
        raise ValueError('connection limit must be positive')

    def connect(client):
        request = bytearray()
        while b'\r\n\r\n' not in request:
            block = client.recv(4096)
            if not block:
                return None
            request.extend(block)
            if len(request) > 16384:
                raise ValueError('proxy headers too large')
        header, tail = bytes(request).split(b'\r\n\r\n', 1)
        lines = header.decode('ascii').split('\r\n')
        method, authority, version = lines[0].split(' ')
        headers = [line.split(':', 1) for line in lines[1:]]
        if (method != 'CONNECT' or version not in ('HTTP/1.0', 'HTTP/1.1')
                or any(len(h) != 2 or h[0].strip().lower() == 'transfer-encoding'
                       or (h[0].strip().lower() == 'content-length' and h[1].strip() != '0') for h in headers)):
            client.sendall(b'HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n')
            return None
        target = routes.get(authority.lower())
        if target is None:
            client.sendall(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
            return None
        upstream = socket.create_connection(target, timeout=5)
        try:
            client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            if tail:
                upstream.sendall(tail)
            return upstream
        except BaseException:
            upstream.close()
            raise

    # Unix permissions are managed by the per-execution host; never make this
    # socket globally reachable just to accommodate a container UID.
    with unix_service(socket_path, connect, max_connections=max_connections) as address:
        yield address


@contextmanager
def loopback_proxy(socket_path, *, max_connections=8):
    """Bridge an ephemeral container-loopback port to the mounted Unix proxy."""
    def connect(_):
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.settimeout(5)
        try:
            upstream.connect(str(socket_path))
        except BaseException:
            upstream.close()
            raise
        return upstream

    with _serving(_Server(('127.0.0.1', 0), connect, max_connections)) as address:
        yield address
