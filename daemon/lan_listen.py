#!/usr/bin/env python3
"""Optional LAN listen-only proxy for the FM audio (opt-in: FM_LAN_LISTEN=1 in fm.env).

Icecast itself stays on loopback (its admin and source endpoints are never exposed). This process serves ONE read-only
URL on the local network and forwards it to the loopback mount:

  GET /          tiny player page
  GET /fm.flac   the live stream (Ogg FLAC, lossless)
  anything else  404; any other method 405

Only private-network clients are served (RFC 1918, loopback, link-local, IPv6 ULA) and the number of simultaneous listeners is
capped, because the phone has 1 GB of RAM and a modest Wi-Fi uplink. Config comes from /root/fm.env:
  FM_LAN_PORT (default 8081)   FM_LAN_MAX (default 5)   ICECAST_PORT (default 8000)   FM_MOUNT (default /fm)
"""
import http.client
import ipaddress
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV_FILE = '/root/fm.env'


def load_env(path=ENV_FILE):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


ENV = load_env()
PORT = int(ENV.get('FM_LAN_PORT', '8081'))
MAX_LISTENERS = int(ENV.get('FM_LAN_MAX', '5'))
UP_HOST = '127.0.0.1'
UP_PORT = int(ENV.get('ICECAST_PORT', '8000'))
UP_PATH = ENV.get('FM_MOUNT', '/fm') + '.flac'
STREAM_PATH = '/fm.flac'

PAGE = (b'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        b'<title>FM</title><body style="font:16px system-ui;margin:2rem;max-width:30rem">'
        b'<h1>Live FM</h1><audio controls autoplay src="/fm.flac" style="width:100%"></audio>'
        b'<p>Lossless Ogg FLAC. If your browser will not play it, open <code>/fm.flac</code> in VLC.</p>')

_lock = threading.Lock()
_listeners = 0


# explicit allow-list: ipaddress.is_private also covers reserved/documentation ranges, which are not "my LAN"
ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', '169.254.0.0/16',
    '::1/128', 'fe80::/10', 'fc00::/7')]


def client_allowed(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in ALLOWED_NETS if net.version == ip.version)


class Handler(BaseHTTPRequestHandler):
    server_version = 'fm-lan'
    sys_version = ''

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s\n' % (self.client_address[0], fmt % args))

    def _plain(self, code, msg):
        body = (msg + '\n').encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global _listeners
        if not client_allowed(self.client_address[0]):
            return self._plain(403, 'private network only')
        path = self.path.split('?', 1)[0]
        if path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
            return
        if path != STREAM_PATH:
            return self._plain(404, 'not found')
        with _lock:
            if _listeners >= MAX_LISTENERS:
                return self._plain(503, 'listener limit reached')
            _listeners += 1
        up = None
        try:
            up = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=10)
            up.request('GET', UP_PATH, headers={'User-Agent': 'fm-lan'})
            r = up.getresponse()
            if r.status != 200:
                return self._plain(503, 'stream not available')
            self.send_response(200)
            self.send_header('Content-Type', r.getheader('Content-Type', 'application/ogg'))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            while True:
                chunk = r.read1(8192) if hasattr(r, 'read1') else r.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            pass    # listener went away or the source restarted: just end this response
        finally:
            if up is not None:
                up.close()
            with _lock:
                _listeners -= 1

    def do_HEAD(self):
        self._plain(405, 'GET only')

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET


def main():
    srv = Server(('0.0.0.0', PORT), Handler)
    sys.stderr.write('fm-lan: listening on port %d, max %d listeners, upstream %s:%d%s\n'
                     % (PORT, MAX_LISTENERS, UP_HOST, UP_PORT, UP_PATH))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
