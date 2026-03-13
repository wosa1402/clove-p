#!/usr/bin/env python3
import argparse
import ipaddress
import select
import socket
import socketserver
from typing import Any


BUFFER_SIZE = 65536


def relay_bidirectional(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    try:
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 60)
            if exceptional:
                return
            if not readable:
                continue
            for source in readable:
                target = right if source is left else left
                data = source.recv(BUFFER_SIZE)
                if not data:
                    return
                target.sendall(data)
    finally:
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FamilySocksHandler(socketserver.BaseRequestHandler):
    timeout = 30

    def handle(self) -> None:
        self.request.settimeout(self.timeout)
        upstream = None
        try:
            self._perform_handshake()
            host, port = self._parse_request()
            resolved_host, resolved_family = self._resolve_target(host, port)
            if self.server.proxy_mode == "direct":
                upstream = self._connect_direct(resolved_host, port, resolved_family)
            else:
                upstream = self._connect_upstream_proxy(resolved_host, port, resolved_family)
            self._send_success()
            relay_bidirectional(self.request, upstream)
            upstream = None
        except Exception:
            self._send_failure()
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

    def _recv_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            part = self.request.recv(size - len(chunks))
            if not part:
                raise ConnectionError("连接已关闭")
            chunks.extend(part)
        return bytes(chunks)

    def _perform_handshake(self) -> None:
        version, method_count = self._recv_exact(2)
        if version != 5:
            raise ValueError("仅支持 SOCKS5")
        _ = self._recv_exact(method_count)
        self.request.sendall(b"\x05\x00")

    def _parse_request(self) -> tuple[str, int]:
        version, command, _reserved, atyp = self._recv_exact(4)
        if version != 5 or command != 1:
            raise ValueError("仅支持 CONNECT")

        if atyp == 1:
            host = socket.inet_ntoa(self._recv_exact(4))
        elif atyp == 3:
            length = self._recv_exact(1)[0]
            host = self._recv_exact(length).decode("utf-8")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, self._recv_exact(16))
        else:
            raise ValueError("不支持的地址类型")

        port = int.from_bytes(self._recv_exact(2), "big")
        return host, port

    def _resolve_target(self, host: str, port: int) -> tuple[str, int]:
        if self.server.family_mode == "auto":
            forced_family = socket.AF_UNSPEC
        else:
            forced_family = socket.AF_INET if self.server.family_mode == "ipv4" else socket.AF_INET6

        try:
            parsed_ip = ipaddress.ip_address(host)
            ip_family = socket.AF_INET if parsed_ip.version == 4 else socket.AF_INET6
            if forced_family != socket.AF_UNSPEC and ip_family != forced_family:
                raise ValueError("目标地址族与当前 family 包装器不匹配")
            return host, ip_family
        except ValueError:
            pass

        results = socket.getaddrinfo(
            host,
            port,
            family=forced_family,
            type=socket.SOCK_STREAM,
        )
        if not results:
            raise ValueError("未解析到可用目标地址")
        target_family = results[0][0]
        target_host = results[0][4][0]
        return target_host, target_family

    def _connect_direct(
        self,
        target_host: str,
        target_port: int,
        target_family: int,
    ) -> socket.socket:
        upstream = socket.socket(target_family, socket.SOCK_STREAM)
        upstream.settimeout(self.timeout)
        upstream.connect((target_host, target_port))
        upstream.settimeout(None)
        return upstream

    def _connect_upstream_proxy(
        self, target_host: str, target_port: int, target_family: int
    ) -> socket.socket:
        upstream = socket.create_connection(
            (self.server.upstream_host, self.server.upstream_port),
            timeout=self.timeout,
        )
        upstream.settimeout(self.timeout)

        upstream.sendall(b"\x05\x01\x00")
        response = upstream.recv(2)
        if response != b"\x05\x00":
            upstream.close()
            raise ValueError("上游 SOCKS5 代理握手失败")

        if target_family == socket.AF_INET:
            atyp = b"\x01"
            packed_host = socket.inet_pton(socket.AF_INET, target_host)
        else:
            atyp = b"\x04"
            packed_host = socket.inet_pton(socket.AF_INET6, target_host)

        request = (
            b"\x05\x01\x00"
            + atyp
            + packed_host
            + target_port.to_bytes(2, "big")
        )
        upstream.sendall(request)

        header = self._recv_from_upstream(upstream, 4)
        if len(header) != 4 or header[1] != 0:
            upstream.close()
            raise ValueError("上游 SOCKS5 连接失败")

        atyp = header[3]
        if atyp == 1:
            _ = self._recv_from_upstream(upstream, 4)
        elif atyp == 3:
            length = self._recv_from_upstream(upstream, 1)[0]
            _ = self._recv_from_upstream(upstream, length)
        elif atyp == 4:
            _ = self._recv_from_upstream(upstream, 16)
        else:
            upstream.close()
            raise ValueError("上游 SOCKS5 返回了未知地址类型")

        _ = self._recv_from_upstream(upstream, 2)
        upstream.settimeout(None)
        return upstream

    def _recv_from_upstream(self, upstream: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            part = upstream.recv(size - len(chunks))
            if not part:
                raise ConnectionError("上游 SOCKS5 连接被关闭")
            chunks.extend(part)
        return bytes(chunks)

    def _send_success(self) -> None:
        self.request.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

    def _send_failure(self) -> None:
        try:
            self.request.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按 IPv4/IPv6 强制解析的 WARP SOCKS5 包装器")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--mode", choices=("socks", "direct"), default="socks")
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int)
    parser.add_argument("--family", choices=("auto", "ipv4", "ipv6"), required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.mode == "socks" and args.upstream_port is None:
        raise SystemExit("--mode socks 需要提供 --upstream-port")
    with ThreadingTCPServer((args.listen_host, args.listen_port), FamilySocksHandler) as server:
        server.proxy_mode = args.mode
        server.upstream_host = args.upstream_host
        server.upstream_port = args.upstream_port
        server.family_mode = args.family
        server.serve_forever()


if __name__ == "__main__":
    main()
