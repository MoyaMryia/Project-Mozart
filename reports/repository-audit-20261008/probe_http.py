"""Do the loopback HTTP probe. The binary is an argument; no model assets are used."""
import json
import socket
import subprocess
import sys
import time

binary = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 18997
process = subprocess.Popen([binary, str(port)], stdin=subprocess.PIPE,
                           stdout=subprocess.DEVNULL)
slow = None
try:
    for _ in range(100):
        try:
            slow = socket.create_connection(('127.0.0.1', port), timeout=0.2)
            break
        except OSError:
            if process.poll() is not None:
                raise RuntimeError('Probe server failed to start')
            time.sleep(0.02)
    if slow is None:
        raise RuntimeError('Probe server did not start')
    slow.sendall(b'GET /api/health HTTP/1.1\r\nHost: localhost\r\n')
    time.sleep(0.1)
    with socket.create_connection(('127.0.0.1', port), timeout=1) as other:
        other.sendall(b'GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n')
        try:
            result = other.recv(4096)
            blocked = False
        except socket.timeout:
            blocked = True
        slow.close()
        slow = None
        other.settimeout(2)
        if blocked:
            result = other.recv(4096)
    slow = socket.create_connection(('127.0.0.1', port), timeout=1)
    slow.sendall(b'GET /api/health HTTP/1.1\r\nHost: localhost\r\n')
    time.sleep(0.1)
    process.stdin.write(b'\n')
    process.stdin.flush()
    try:
        process.wait(timeout=1)
        shutdown_blocked = False
    except subprocess.TimeoutExpired:
        shutdown_blocked = True
    slow.close()
    slow = None
    process.wait(timeout=2)
    print(json.dumps({
        'second_request_blocked_for_at_least_seconds': 1 if blocked else 0,
        'health_recovers_after_first_client_closes': b'200 OK' in result,
        'shutdown_blocked_until_client_closes': shutdown_blocked,
        'server_exit_code': process.returncode,
    }, indent=2))
finally:
    if slow is not None:
        slow.close()
    if process.poll() is None:
        process.kill()
        process.wait()
