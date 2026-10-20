"""Keep BaoStock's public protocol while surfacing bounded transport errors."""
from __future__ import annotations

import socket
import time
import zlib
from contextlib import contextmanager


class BaoTransportError(ConnectionError):
    pass


class Transport:
    def __init__(self, host, port, timeout=12):
        self.host, self.port, self.timeout = host, port, timeout
        self.connection = None
        self.pending = b''

    def error(self, stage, exc):
        code = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
        detail = f'{type(exc).__name__}' + (f' / 系统代码 {code}' if code else '')
        return BaoTransportError(f'BaoStock {stage}失败 [{self.host}:{self.port}]：{detail}；{exc}')

    def connect(self):
        try:
            self.connection = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise self.error('DNS/TCP 连接', exc) from exc
        return self.connection

    def exchange(self, message):
        from baostock.common import contants as constants
        deadline = time.monotonic() + self.timeout
        stage = '发送'
        try:
            self.connection.settimeout(self.timeout)
            self.connection.sendall((message+'\n').encode('utf-8'))
            stage = '接收'
            marker = b'<![CDATA[]]>\n'
            received = self.pending
            while marker not in received:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('响应超过时限')
                self.connection.settimeout(remaining)
                chunk = self.connection.recv(8192)
                if not chunk:
                    raise ConnectionError(f'服务器提前断开连接，已接收 {len(received)} 字节')
                received += chunk
                if len(received) > 16*1024*1024:
                    raise ValueError('响应超过 16 MiB 上限')
            end = received.index(marker)+len(marker)
            frame, self.pending = received[:end], received[end:]
            stage = '解析响应'
            header = frame[:constants.MESSAGE_HEADER_LENGTH].decode('utf-8')
            fields = header.split(constants.MESSAGE_SPLIT)
            if fields[1] in constants.COMPRESSED_MESSAGE_TYPE_TUPLE:
                size = int(fields[2])
                body = frame[constants.MESSAGE_HEADER_LENGTH:constants.MESSAGE_HEADER_LENGTH+size]
                if size < 0 or len(body) != size:
                    raise ValueError('响应长度不完整')
                return header+zlib.decompress(body).decode('utf-8')
            return frame.decode('utf-8')
        except (OSError, ValueError, IndexError, zlib.error) as exc:
            raise self.error(stage, exc) from exc

    def close(self):
        if self.connection is not None:
            self.connection.close()


@contextmanager
def safe_baostock_transport():
    # Only the isolated provider worker is patched, never site-packages or other apps.
    from baostock.util import socketutil
    from baostock.common import context, contants
    original_connect, original_send = socketutil.SocketUtil.connect, socketutil.send_msg
    transport = None

    def connect(_self, api_key):
        nonlocal transport
        host = contants.BAOSTOCK_VIP_SERVER_IP if api_key.startswith('bs-') else contants.BAOSTOCK_SERVER_IP
        transport = Transport(host, contants.BAOSTOCK_SERVER_PORT)
        context.default_socket = transport.connect()

    def send(message):
        if transport is None or transport.connection is None:
            raise BaoTransportError('BaoStock 尚未建立连接')
        return transport.exchange(message)

    socketutil.SocketUtil.connect, socketutil.send_msg = connect, send
    try:
        yield
    finally:
        socketutil.SocketUtil.connect, socketutil.send_msg = original_connect, original_send
        if transport:
            transport.close()
        context.default_socket = None
