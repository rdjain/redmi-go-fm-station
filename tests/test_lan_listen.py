"""Smoke tests for daemon/lan_listen.py (stdlib only). Run:  python -m unittest discover -s tests -v"""
import http.client
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'daemon'))
import lan_listen  # noqa: E402


class FakeIcecast(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != '/fm.flac':
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/ogg')
        self.end_headers()
        try:
            self.wfile.write(b'OggS' + b'\0' * 4092)
            time.sleep(1.5)                 # keep the listener "connected" for the cap test
        except OSError:
            pass


def serve(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


class LanListenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(('127.0.0.1', 0), FakeIcecast)
        serve(cls.up)
        lan_listen.UP_PORT = cls.up.server_address[1]
        lan_listen.UP_PATH = '/fm.flac'
        lan_listen.MAX_LISTENERS = 2
        cls.srv = lan_listen.Server(('127.0.0.1', 0), lan_listen.Handler)
        serve(cls.srv)
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.up.shutdown()

    def get(self, path, method='GET'):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request(method, path)
        return c, c.getresponse()

    def test_stream_is_proxied(self):
        c, r = self.get('/fm.flac')
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader('Content-Type'), 'application/ogg')
        self.assertEqual(r.read(4), b'OggS')
        c.close()

    def test_player_page(self):
        c, r = self.get('/')
        self.assertEqual(r.status, 200)
        self.assertIn(b'<audio', r.read())
        c.close()

    def test_only_the_stream_path_exists(self):
        for p in ('/admin/stats.xml', '/api/status', '/fm', '/status.xsl', '/fm.flac/../x'):
            c, r = self.get(p)
            self.assertEqual(r.status, 404, p)
            c.close()

    def test_only_get_is_allowed(self):
        for m in ('POST', 'PUT', 'DELETE', 'HEAD'):
            c, r = self.get('/fm.flac', m)
            self.assertEqual(r.status, 405, m)
            c.close()

    def test_listener_cap(self):
        held = []
        for _ in range(lan_listen.MAX_LISTENERS):
            c, r = self.get('/fm.flac')
            self.assertEqual(r.status, 200)
            r.read(4)
            held.append(c)
        c, r = self.get('/fm.flac')
        self.assertEqual(r.status, 503)
        c.close()
        for c in held:
            c.close()
        time.sleep(2)                       # let the proxied connections drain
        c, r = self.get('/fm.flac')
        self.assertEqual(r.status, 200)     # slots are released
        c.close()

    def test_client_filter(self):
        ok = ('192.168.1.5', '10.0.0.2', '172.16.9.9', '127.0.0.1', '169.254.1.1', '::1', '::ffff:192.168.1.5')
        bad = ('8.8.8.8', '1.1.1.1', '203.0.113.7', '2001:4860:4860::8888', 'not-an-ip')
        for a in ok:
            self.assertTrue(lan_listen.client_allowed(a), a)
        for a in bad:
            self.assertFalse(lan_listen.client_allowed(a), a)


if __name__ == '__main__':
    unittest.main()
