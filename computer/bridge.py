import os
import select
import socket
import sys


port = {'view': 5901, 'control': 5900}[sys.argv[1]]
with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
    connection.settimeout(None)
    while True:
        ready, _, _ = select.select([connection, sys.stdin.buffer], [], [])
        if connection in ready:
            payload = connection.recv(65536)
            if not payload:
                break
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        if sys.stdin.buffer in ready:
            payload = os.read(sys.stdin.fileno(), 65536)
            if not payload:
                break
            connection.sendall(payload)
