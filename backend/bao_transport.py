"""Keep BaoStock's public protocol while surfacing bounded transport errors."""
from __future__ import annotations

import socket
import re
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
        self.close()
        try:
            self.connection = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise self.error('DNS/TCP 连接', exc) from exc
        return self.connection

    def exchange(self, message):
        from baostock.common import contants as constants
        if self.connection is None:
            raise BaoTransportError('BaoStock 连接已关闭，需重新登录后重试')
        deadline = time.monotonic() + self.timeout
        stage = '发送'
        try:
            self.connection.settimeout(self.timeout)
            self.connection.sendall((message+'\n').encode('utf-8'))
            stage = '接收'
            marker = b'<![CDATA[]]>\n'
            received = self.pending
            self.pending = b''
            limit = 16*1024*1024
            def receive():
                nonlocal received
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('响应超过时限')
                self.connection.settimeout(remaining)
                chunk = self.connection.recv(8192)
                if not chunk:
                    raise ConnectionError(f'服务器提前断开连接，已接收 {len(received)} 字节')
                received += chunk
                if len(received) > limit+constants.MESSAGE_HEADER_LENGTH+8192:
                    raise ValueError('响应超过 16 MiB 上限')
            while len(received)<constants.MESSAGE_HEADER_LENGTH:receive()
            stage = '解析响应'
            raw_header=received[:constants.MESSAGE_HEADER_LENGTH]
            if not re.fullmatch(rb'[0-9.]{7}\x01[0-9]{2}\x01[0-9]{10}',raw_header):
                raise ValueError('报文头无效，连接已丢弃，等待重新登录')
            header = raw_header.decode('ascii')
            fields = header.split(constants.MESSAGE_SPLIT)
            size=int(fields[2])
            if size>limit:raise ValueError('声明的响应长度超过 16 MiB 上限')
            compressed=fields[1] in constants.COMPRESSED_MESSAGE_TYPE_TUPLE
            # Compressed payload is binary: its contents cannot delimit a frame.
            boundary=constants.MESSAGE_HEADER_LENGTH+size if compressed else constants.MESSAGE_HEADER_LENGTH
            stage = '接收'
            while len(received)<boundary or received.find(marker,boundary)<0:
                if compressed and len(received)>boundary+128:raise ValueError('响应结束标记异常')
                receive()
            end=received.index(marker,boundary)+len(marker)
            frame,self.pending=received[:end],received[end:]
            stage = '解析响应'
            if compressed:
                body=frame[constants.MESSAGE_HEADER_LENGTH:boundary]
                decoded=zlib.decompress(body)
                if len(decoded)>limit:raise ValueError('解压后的响应超过 16 MiB 上限')
                return header+decoded.decode('utf-8')
            return frame.decode('utf-8')
        except (OSError, ValueError, IndexError, zlib.error) as exc:
            # An incomplete reply may arrive later. Never send another query on it.
            self.close()
            raise self.error(stage, exc) from exc

    def close(self):
        connection,self.connection=self.connection,None
        self.pending=b''
        if connection is not None:
            try:connection.close()
            except OSError:pass


@contextmanager
def safe_baostock_transport():
    # Only the isolated provider worker is patched, never site-packages or other apps.
    from baostock.util import socketutil
    from baostock.common import context, contants
    original_connect, original_send = socketutil.SocketUtil.connect, socketutil.send_msg
    transport = None

    def connect(_self, api_key):
        nonlocal transport
        if transport:transport.close()
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
