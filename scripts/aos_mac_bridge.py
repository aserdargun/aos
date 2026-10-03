import argparse
import asyncio
import ipaddress
import re
from contextlib import suppress


TAILSCALE_NETWORK = ipaddress.ip_network('100.64.0.0/10')
UPSTREAM_HOST = '127.0.0.1'
UPSTREAM_PORT = 8765
HEADER_LIMIT = 32768
BODY_LIMIT = 2 * 1024 * 1024
CONNECTION_LIMIT = 24
HEADER_TIMEOUT = 10
IO_TIMEOUT = 120
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'origin',
               'forwarded', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto'}


class BridgeDenied(ValueError):
    def __init__(self, status):
        self.status = status


class MacBridge:
    def __init__(self, bind_address, peer_address, port):
        for address in (bind_address, peer_address):
            if ipaddress.ip_address(address) not in TAILSCALE_NETWORK:
                raise ValueError('An exact Tailscale IPv4 address is required')
        if bind_address == peer_address or not 1024 <= port <= 65535 or port == UPSTREAM_PORT:
            raise ValueError('Distinct peer/bind addresses and a separate unprivileged port are required')
        self.peer_address = peer_address
        self.public_host = f'{bind_address}:{port}'
        self.public_origin = 'http://' + self.public_host
        self.active_connections = 0

    def request(self, raw):
        if len(raw) > HEADER_LIMIT:
            raise BridgeDenied(431)
        lines = raw.decode('latin-1').split('\r\n')
        parts = lines[0].split(' ')
        if len(parts) != 3:
            raise BridgeDenied(400)
        method, target, protocol = parts
        if method not in {'GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE'}:
            raise BridgeDenied(405)
        if (protocol != 'HTTP/1.1' or not target.startswith('/') or target.startswith('//')
                or any(ord(character) <= 32 or ord(character) == 127 for character in target)):
            raise BridgeDenied(400)
        headers = {}
        for line in lines[1:-2]:
            name, separator, value = line.partition(':')
            if not separator or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None:
                raise BridgeDenied(400)
            name = name.lower()
            if name in headers or any(ord(character) < 32 and character != '\t' for character in value):
                raise BridgeDenied(400)
            headers[name] = value.strip()
        origin = headers.get('origin')
        if (headers.get('host') != self.public_host or origin not in (None, self.public_origin)
                or method not in {'GET', 'HEAD', 'OPTIONS'} and origin != self.public_origin):
            raise BridgeDenied(403)
        if 'transfer-encoding' in headers or 'expect' in headers:
            raise BridgeDenied(400)
        length = headers.get('content-length', '0')
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            raise BridgeDenied(400)
        length = int(length)
        if length > BODY_LIMIT:
            raise BridgeDenied(413)
        connection_names = {name.strip().lower() for name in headers.get('connection', '').split(',')}
        upgraded = 'upgrade' in headers
        if upgraded and (headers['upgrade'].lower() != 'websocket' or 'upgrade' not in connection_names
                         or method != 'GET' or target.split('?', 1)[0] != '/websockify'
                         or length or origin != self.public_origin):
            raise BridgeDenied(403)
        excluded = HOP_HEADERS | connection_names | {'content-length'}
        forwarded = [(name, value) for name, value in headers.items() if name not in excluded]
        forwarded.append(('Host', f'{UPSTREAM_HOST}:{UPSTREAM_PORT}'))
        if origin is not None:
            forwarded.append(('Origin', f'http://{UPSTREAM_HOST}:{UPSTREAM_PORT}'))
        if length or 'content-length' in headers:
            forwarded.append(('Content-Length', str(length)))
        forwarded.append(('Connection', 'Upgrade' if upgraded else 'close'))
        if upgraded:
            forwarded.append(('Upgrade', 'websocket'))
        encoded = ('\r\n'.join([f'{method} {target} HTTP/1.1',
                    *(f'{name}: {value}' for name, value in forwarded)]) + '\r\n\r\n').encode('latin-1')
        return encoded, length, upgraded

    @staticmethod
    async def reject(writer, status):
        writer.write(f'HTTP/1.1 {status} Bridge boundary\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode())
        with suppress(OSError):
            await writer.drain()

    @staticmethod
    async def relay(reader, writer):
        while content := await asyncio.wait_for(reader.read(65536), IO_TIMEOUT):
            writer.write(content)
            await asyncio.wait_for(writer.drain(), IO_TIMEOUT)

    async def handle(self, reader, writer):
        peer = writer.get_extra_info('peername')
        admitted = False
        upstream_writer = None
        response_started = False
        tasks = []
        try:
            if not peer or peer[0] != self.peer_address:
                raise BridgeDenied(403)
            if self.active_connections >= CONNECTION_LIMIT:
                raise BridgeDenied(503)
            self.active_connections += 1
            admitted = True
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), HEADER_TIMEOUT)
            headers, length, upgraded = self.request(raw)
            body = await asyncio.wait_for(reader.readexactly(length), HEADER_TIMEOUT) if length else b''
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT, limit=HEADER_LIMIT), HEADER_TIMEOUT)
            upstream_writer.write(headers + body)
            await asyncio.wait_for(upstream_writer.drain(), HEADER_TIMEOUT)
            if upgraded:
                response = await asyncio.wait_for(upstream_reader.readuntil(b'\r\n\r\n'), HEADER_TIMEOUT)
                if len(response) > HEADER_LIMIT:
                    raise BridgeDenied(502)
                writer.write(response)
                response_started = True
                await writer.drain()
                if response.split(b'\r\n', 1)[0].split(b' ')[1:2] == [b'101']:
                    tasks = [asyncio.create_task(self.relay(reader, upstream_writer)),
                             asyncio.create_task(self.relay(upstream_reader, writer))]
                    completed, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in completed:
                        task.result()
                    for task in pending:
                        task.cancel()
                    return
            response_started = True
            await self.relay(upstream_reader, writer)
        except BridgeDenied as error:
            if not response_started:
                await self.reject(writer, error.status)
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            if not response_started:
                await self.reject(writer, 400)
        except (OSError, TimeoutError):
            if not response_started:
                await self.reject(writer, 502)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for connection in (upstream_writer, writer):
                if connection is not None:
                    connection.close()
                    with suppress(OSError, TimeoutError):
                        await asyncio.wait_for(connection.wait_closed(), 5)
            if admitted:
                self.active_connections -= 1


async def serve(arguments):
    bridge = MacBridge(arguments.bind_address, arguments.peer_address, arguments.port)
    server = await asyncio.start_server(bridge.handle, arguments.bind_address, arguments.port,
                                        limit=HEADER_LIMIT, backlog=32)
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description='One authorized Tailscale peer to the existing loopback AOS UI')
    parser.add_argument('--bind-address', required=True)
    parser.add_argument('--peer-address', required=True)
    parser.add_argument('--port', type=int, default=8766)
    asyncio.run(serve(parser.parse_args()))


if __name__ == '__main__':
    main()
