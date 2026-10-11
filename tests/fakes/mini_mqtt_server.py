"""Minimal in-process MQTT 3.1.1 server for wire-level tests.

Listens on 127.0.0.1 (ephemeral port) and speaks just enough of the
protocol for a real paho client: CONNECT/CONNACK, SUBSCRIBE/SUBACK,
UNSUBSCRIBE/UNSUBACK, PUBLISH (QoS 0 and 1), PINGREQ/PINGRESP and
DISCONNECT. Routing is exact-topic only (no wildcards, no retained
messages, no sessions).

Failure injection:
  - drop_all()              abruptly closes every client connection
                            (simulates a broker crash / network cut)
  - reject_connections      when True, new TCP connections are closed
                            immediately without a CONNACK
                            (simulates a broker that is down)
  - connack_rc              return code sent in every CONNACK

All threads are daemon threads; close() tears everything down.
"""

import socket
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple


class _Conn:
    def __init__(self, index: int, sock: socket.socket):
        self.index = index
        self.sock = sock
        self.client_id: Optional[str] = None
        self.topics: Set[str] = set()
        self.send_lock = threading.Lock()

    def send(self, data: bytes) -> None:
        with self.send_lock:
            self.sock.sendall(data)


def _encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _encode_str(s: str) -> bytes:
    b = s.encode('utf-8')
    return struct.pack('!H', len(b)) + b


class MiniMqttServer:
    def __init__(self, *, connack_rc: int = 0):
        self.connack_rc = connack_rc
        self.reject_connections = False

        self._lock = threading.Lock()
        self._closed = False
        self._conns: List[_Conn] = []
        # Observations (read via the snapshot helpers below).
        self.tcp_accepts = 0
        self.connects: List[Tuple[int, Optional[str]]] = []   # (conn index, client id)
        self.subscriptions: List[Tuple[int, str]] = []        # (conn index, topic)
        self.publishes: List[Tuple[int, str, bytes]] = []     # (conn index, topic, payload)

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(('127.0.0.1', 0))
        self._srv.listen(16)
        self.host = '127.0.0.1'
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept_loop, name='MiniMqtt-accept',
                         daemon=True).start()

    # ------------------------------------------------------------------
    # Observation helpers
    # ------------------------------------------------------------------

    def connect_count(self) -> int:
        with self._lock:
            return len(self.connects)

    def topics_of_connection(self, index: int) -> Set[str]:
        with self._lock:
            return {t for i, t in self.subscriptions if i == index}

    def last_connection_index(self) -> Optional[int]:
        with self._lock:
            return self.connects[-1][0] if self.connects else None

    def published(self) -> List[Tuple[str, bytes]]:
        with self._lock:
            return [(t, p) for _, t, p in self.publishes]

    @staticmethod
    def wait_until(predicate: Callable[[], bool], timeout: float = 10.0,
                   interval: float = 0.02) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    # ------------------------------------------------------------------
    # Failure injection / server-side actions
    # ------------------------------------------------------------------

    def drop_all(self) -> None:
        """Abruptly close every client connection (RST where possible)."""
        with self._lock:
            conns, self._conns = self._conns, []
        for c in conns:
            self._hard_close(c.sock)

    def publish(self, topic: str, payload: bytes) -> int:
        """Send a QoS 0 PUBLISH to every connection subscribed to
        `topic` (exact match). Returns the number of deliveries."""
        packet = self._publish_packet(topic, payload)
        with self._lock:
            targets = [c for c in self._conns if topic in c.topics]
        delivered = 0
        for c in targets:
            try:
                c.send(packet)
                delivered += 1
            except OSError:
                pass
        return delivered

    def close(self) -> None:
        with self._lock:
            self._closed = True
        # shutdown() before close(): on Linux, close() alone does not
        # wake an accept()/recv() blocked in another thread.
        try:
            self._srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._srv.close()
        except OSError:
            pass
        self.drop_all()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _hard_close(sock: socket.socket) -> None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack('ii', 1, 0))
        except OSError:
            pass
        # shutdown() before close(): on Linux, close() alone neither
        # wakes the _serve thread blocked in recv() nor puts a FIN/RST
        # on the wire while that recv() holds the file.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    @staticmethod
    def _publish_packet(topic: str, payload: bytes) -> bytes:
        body = _encode_str(topic) + payload
        return bytes([0x30]) + _encode_remaining_length(len(body)) + body

    def _accept_loop(self) -> None:
        index = 0
        while True:
            try:
                sock, _ = self._srv.accept()
            except OSError:
                return
            with self._lock:
                if self._closed:
                    self._hard_close(sock)
                    return
                self.tcp_accepts += 1
                reject = self.reject_connections
            if reject:
                self._hard_close(sock)
                continue
            index += 1
            conn = _Conn(index, sock)
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,),
                             name=f'MiniMqtt-conn-{index}', daemon=True).start()

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        buf = b''
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('peer closed')
            buf += chunk
        return buf

    def _read_packet(self, sock: socket.socket) -> Tuple[int, bytes]:
        first = self._recv_exact(sock, 1)[0]
        multiplier, length = 1, 0
        while True:
            byte = self._recv_exact(sock, 1)[0]
            length += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        body = self._recv_exact(sock, length) if length else b''
        return first, body

    def _serve(self, conn: _Conn) -> None:
        try:
            while True:
                first, body = self._read_packet(conn.sock)
                ptype = first >> 4
                if ptype == 1:      # CONNECT
                    # On a refused CONNACK keep reading: the client closes
                    # the connection itself, so the CONNACK is never lost
                    # to an early RST from our side.
                    self._on_connect_packet(conn, body)
                elif ptype == 3:    # PUBLISH
                    self._on_publish_packet(conn, first, body)
                elif ptype == 8:    # SUBSCRIBE
                    self._on_subscribe_packet(conn, body)
                elif ptype == 10:   # UNSUBSCRIBE
                    self._on_unsubscribe_packet(conn, body)
                elif ptype == 12:   # PINGREQ
                    conn.send(b'\xd0\x00')
                elif ptype == 14:   # DISCONNECT
                    return
        except (OSError, ConnectionError):
            pass
        finally:
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            self._hard_close(conn.sock)

    def _on_connect_packet(self, conn: _Conn, body: bytes) -> None:
        # Variable header: protocol name (str), level (1), flags (1),
        # keepalive (2); payload starts with the client id (str).
        name_len = struct.unpack('!H', body[:2])[0]
        pos = 2 + name_len + 4
        cid_len = struct.unpack('!H', body[pos:pos + 2])[0]
        conn.client_id = body[pos + 2:pos + 2 + cid_len].decode('utf-8', 'replace')
        with self._lock:
            self.connects.append((conn.index, conn.client_id))
        conn.send(bytes([0x20, 0x02, 0x00, self.connack_rc]))

    def _on_subscribe_packet(self, conn: _Conn, body: bytes) -> None:
        pid = body[:2]
        pos, granted, topics = 2, bytearray(), []
        while pos < len(body):
            tlen = struct.unpack('!H', body[pos:pos + 2])[0]
            topic = body[pos + 2:pos + 2 + tlen].decode('utf-8')
            pos += 2 + tlen + 1     # skip requested QoS byte
            topics.append(topic)
            granted.append(0)
        with self._lock:
            conn.topics.update(topics)
            self.subscriptions.extend((conn.index, t) for t in topics)
        conn.send(bytes([0x90]) + _encode_remaining_length(2 + len(granted))
                  + pid + bytes(granted))

    def _on_unsubscribe_packet(self, conn: _Conn, body: bytes) -> None:
        pid = body[:2]
        pos = 2
        with self._lock:
            while pos < len(body):
                tlen = struct.unpack('!H', body[pos:pos + 2])[0]
                conn.topics.discard(body[pos + 2:pos + 2 + tlen].decode('utf-8'))
                pos += 2 + tlen
        conn.send(b'\xb0\x02' + pid)

    def _on_publish_packet(self, conn: _Conn, first: int, body: bytes) -> None:
        qos = (first >> 1) & 0x03
        tlen = struct.unpack('!H', body[:2])[0]
        topic = body[2:2 + tlen].decode('utf-8')
        pos = 2 + tlen
        pid = b''
        if qos:
            pid, pos = body[pos:pos + 2], pos + 2
        payload = body[pos:]
        with self._lock:
            self.publishes.append((conn.index, topic, payload))
        if qos == 1:
            conn.send(b'\x40\x02' + pid)
        self.publish(topic, payload)
