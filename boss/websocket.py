"""Minimal RFC 6455 client: text frames, ping/pong, close. Stdlib only."""
import base64
import hashlib
import os
import socket
import ssl
import struct
import urllib.parse


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Closed(Exception):
    pass


class WebSocket:
    def __init__(self, url, timeout=30):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise ValueError("websocket URL must be ws:// or wss://")
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        raw = socket.create_connection((parts.hostname, port), timeout=timeout)
        if parts.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname)
        self.sock, self.buffer = raw, b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self._until(b"\r\n\r\n").decode("latin-1")
        status = head.split("\r\n", 1)[0]
        if " 101 " not in status + " ":
            raise ConnectionError("websocket upgrade refused: " + status[:80])
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if f"sec-websocket-accept: {expected}".lower() not in head.lower():
            raise ConnectionError("websocket upgrade had a bad accept key")

    def _until(self, marker):
        while marker not in self.buffer:
            self._fill()
        head, _, self.buffer = self.buffer.partition(marker)
        return head

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise Closed("connection closed")
        self.buffer += chunk

    def _take(self, size):
        while len(self.buffer) < size:
            self._fill()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _send(self, opcode, payload):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        self._send(0x1, text.encode())

    def receive(self):
        """Next complete text message. Answers pings; raises Closed on close."""
        message, opcode = b"", None
        while True:
            first, second = self._take(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._take(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._take(8))[0]
            mask = self._take(4) if second & 0x80 else None
            payload = self._take(size)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            code = first & 0x0F
            if code == 0x9:
                self._send(0xA, payload)
            elif code == 0x8:
                raise Closed("server closed the stream")
            elif code in (0x1, 0x2, 0x0):
                opcode = opcode if code == 0x0 else code
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self):
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        self.sock.close()
