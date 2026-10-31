#!/usr/bin/python3
"""FM tuner daemon: owns /dev/radio0, serves a token-protected web page + JSON API.

Run inside the Debian chroot after the Android side has started the FM firmware
(fm-firmware-up.sh). Config comes from /root/fm.env (FM_TOKEN, FM_BIND, FM_PORT).

  GET  /                  web page (no auth; it asks for the token)
  GET  /api/status        {freq_mhz, signal, stereo, stream_port, presets}
  POST /api/tune          {"freq_mhz": 93.5}
  POST /api/step          {"delta_mhz": 0.1}
  POST /api/presets       {"freq_mhz": 93.5, "name": "Radio X"}   (add / rename)
  POST /api/presets/delete {"freq_mhz": 93.5}
All /api/* calls need the header  X-Token: <FM_TOKEN>  and a private-network client.
"""
import hmac
import ipaddress
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fmchip  # noqa: E402
import yt_service  # noqa: E402

fmchip.CAL_FILE = os.path.join(HERE, 'Riva_fm_cal')
ENV_FILE = '/root/fm.env'
STATE_FILE = os.path.join(HERE, 'state.json')
NOW_FILE = '/run/fm/now.txt'
STEP_MHZ = 0.05


def load_env(path):
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


ENV = load_env(ENV_FILE)
TOKEN = ENV.get('FM_TOKEN', '')
BIND = ENV.get('FM_BIND', '127.0.0.1')
PORT = int(ENV.get('FM_PORT', '8080'))
STREAM_PORT = int(ENV.get('ICECAST_PORT', '8000'))

chip = fmchip.FmChip()
chip_lock = threading.Lock()
state_lock = threading.Lock()
state = {'last': 93.5, 'presets': []}
yt = None

# RDS (station name / RadioText) as decoded by the chip; cleared on every retune
rds_lock = threading.Lock()
rds = {'freq': None, 'ps': '', 'rt': '', 'pi': 0, 'pty': 0}
_written = {}


def fmt(freq):
    s = '%.2f' % freq
    return s[:-1] if s.endswith('0') else s


def load_state():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_state():
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def write_now(freq):
    try:
        os.makedirs(os.path.dirname(NOW_FILE), exist_ok=True)
        tmp = NOW_FILE + '.tmp'
        with open(tmp, 'w') as f:
            f.write('FM %s MHz' % fmt(freq))
        os.replace(tmp, NOW_FILE)
    except OSError:
        pass


def snap(freq):
    return round(round(freq / STEP_MHZ) * STEP_MHZ, 2)


def preset_name(freq):
    with state_lock:
        for p in state['presets']:
            if abs(p['freq_mhz'] - freq) < 0.01 and p['name'] != fmt(p['freq_mhz']):
                return p['name']
    return ''


def station_display():
    """Best available station name: RDS PS, else the user's preset name for this frequency."""
    with rds_lock:
        ps, freq = rds['ps'], rds['freq']
    return ps or (preset_name(freq) if freq else '')


def _write_if_changed(path, text):
    if _written.get(path) == text:
        return
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            f.write(text)
        os.replace(tmp, path)
        _written[path] = text
    except OSError:
        pass


def write_rds_files():
    """Overlay text files read live by the YouTube ffmpeg (drawtext reload=1)."""
    st = yt.settings if yt else {}
    with rds_lock:
        rt = rds['rt']
    station = station_display() if st.get('show_ps', True) else ''
    lines = textwrap.wrap(rt, 38)[:2] if st.get('show_rt', True) else []
    lines += [''] * (2 - len(lines))
    _write_if_changed(yt_service.STATION_FILE, station)
    _write_if_changed(yt_service.RT1_FILE, lines[0])
    _write_if_changed(yt_service.RT2_FILE, lines[1])


def rds_loop():
    while True:
        time.sleep(1.0)
        try:
            with chip_lock:
                upd = chip.read_rds()
        except Exception:  # noqa: BLE001 - chip may be shutting down
            continue
        if upd:
            with rds_lock:
                # RadioText must come from the same station (same PI code) as the name we already have
                if 'rt' in upd and rds['pi'] and upd.get('pi_rt') and upd['pi_rt'] != rds['pi']:
                    upd.pop('rt')
                for k in ('ps', 'rt', 'pi', 'pty'):
                    if k in upd:
                        rds[k] = upd[k]
        write_rds_files()


def do_tune(freq):
    freq = snap(float(freq))
    if not (fmchip.MIN_MHZ <= freq <= fmchip.MAX_MHZ):
        raise ValueError('frequency must be between %s and %s MHz' % (fmchip.MIN_MHZ, fmchip.MAX_MHZ))
    with chip_lock:
        chip.tune(freq)
        time.sleep(0.15)
        actual = chip.frequency()
        chip.drain_rds()
    with rds_lock:
        rds.update(freq=freq, ps='', rt='', pi=0, pty=0)
    with state_lock:
        state['last'] = freq
        save_state()
    write_now(freq)
    write_rds_files()
    return actual


def get_status():
    with chip_lock:
        freq = chip.frequency()
        st = chip.status()
    with state_lock:
        presets = list(state['presets'])
    with rds_lock:
        r = {'ps': rds['ps'], 'rt': rds['rt'], 'pi': rds['pi'], 'pty': rds['pty']}
    r['station'] = station_display()
    return {'freq_mhz': round(freq, 2), 'signal': st['signal'], 'stereo': st['stereo'],
            'stream_port': STREAM_PORT, 'mount': '/fm', 'presets': presets, 'rds': r}


class Handler(BaseHTTPRequestHandler):
    server_version = 'fm-tuner'

    def log_message(self, fmt_, *args):
        sys.stderr.write('%s %s\n' % (self.client_address[0], fmt_ % args))

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def _client_ok(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_private
        except ValueError:
            return False

    def _authorized(self):
        if not self._client_ok():
            self._send(403, {'error': 'local network only'})
            return False
        sent = self.headers.get('X-Token', '')
        if not TOKEN or not hmac.compare_digest(sent.encode(), TOKEN.encode()):
            time.sleep(0.5)
            self._send(401, {'error': 'bad or missing token'})
            return False
        return True

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length', '0'))
            return json.loads(self.rfile.read(min(n, 4096)) or b'{}')
        except (ValueError, OSError):
            return None

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            if not self._client_ok():
                return self._send(403, {'error': 'local network only'})
            with open(os.path.join(HERE, 'web', 'index.html'), 'rb') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8')
        if not self._authorized():
            return
        if path == '/api/status':
            try:
                return self._send(200, get_status())
            except OSError as e:
                return self._send(503, {'error': 'tuner unavailable: %s' % e})
        if path == '/api/yt/status':
            return self._send(200, yt.status())
        if path == '/api/yt/library':
            return self._send(200, {'items': yt.library()})
        if path.startswith('/api/yt/library/'):
            try:
                with open(yt.library_path(path[len('/api/yt/library/'):]), 'rb') as f:
                    return self._send(200, f.read(), 'image/jpeg')
            except (ValueError, OSError):
                return self._send(404, {'error': 'not found'})
        if path == '/api/yt/backdrop':
            try:
                yt.ensure_backdrop()
                with open(yt_service.BACKDROP, 'rb') as f:
                    return self._send(200, f.read(), 'image/jpeg')
            except (OSError, subprocess.SubprocessError):
                return self._send(404, {'error': 'no backdrop'})
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        if not self._authorized():
            return
        path = self.path.split('?')[0]
        if path == '/api/yt/backdrop':
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if n <= 0 or n > yt_service.MAX_IMAGE:
                    return self._send(413, {'error': 'image must be 1 byte - 5 MB'})
                yt.set_backdrop(self.rfile.read(n))
                return self._send(200, yt.status())
            except ValueError as e:
                return self._send(400, {'error': str(e)})
            except (OSError, subprocess.SubprocessError) as e:
                return self._send(500, {'error': 'image processing failed: %s' % e})
        body = self._body()
        if body is None:
            return self._send(400, {'error': 'invalid JSON'})
        try:
            if path == '/api/tune':
                do_tune(body['freq_mhz'])
            elif path == '/api/step':
                do_tune(get_status()['freq_mhz'] + float(body['delta_mhz']))
            elif path == '/api/presets':
                freq = snap(float(body['freq_mhz']))
                name = str(body.get('name') or fmt(freq))[:40]
                with state_lock:
                    state['presets'] = [p for p in state['presets'] if p['freq_mhz'] != freq]
                    state['presets'].append({'freq_mhz': freq, 'name': name})
                    state['presets'].sort(key=lambda p: p['freq_mhz'])
                    save_state()
            elif path == '/api/presets/delete':
                freq = snap(float(body['freq_mhz']))
                with state_lock:
                    state['presets'] = [p for p in state['presets'] if p['freq_mhz'] != freq]
                    save_state()
            elif path == '/api/yt/start':
                yt.start()
                return self._send(200, yt.status())
            elif path == '/api/yt/stop':
                yt.stop()
                return self._send(200, yt.status())
            elif path == '/api/yt/settings':
                yt.update_settings(body)
                return self._send(200, yt.status())
            elif path == '/api/yt/library/select':
                yt.select_library(str(body['name']))
                return self._send(200, yt.status())
            elif path == '/api/yt/key':
                yt.set_key(body['key'])
                return self._send(200, yt.status())
            else:
                return self._send(404, {'error': 'not found'})
            return self._send(200, get_status())
        except (KeyError, TypeError):
            return self._send(400, {'error': 'missing or invalid field'})
        except ValueError as e:
            return self._send(400, {'error': str(e)})
        except OSError as e:
            return self._send(503, {'error': 'tuner error: %s' % e})


def main():
    if not TOKEN or len(TOKEN) < 16:
        sys.exit('FM_TOKEN missing/short in %s' % ENV_FILE)
    global yt
    load_state()
    yt = yt_service.YouTube()
    chip.power_on(state['last'])
    time.sleep(0.4)
    with rds_lock:
        rds['freq'] = round(chip.frequency(), 2)
    write_now(chip.frequency())
    threading.Thread(target=rds_loop, daemon=True).start()
    addrs = [a for a in (BIND, ENV.get('FM_BIND2', '')) if a]
    servers = [ThreadingHTTPServer((a, PORT), Handler) for a in addrs]
    httpd = servers[0]

    def stop(*_):
        for s in servers:
            threading.Thread(target=s.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    if yt.should_resume():
        print('resuming YouTube stream (it was live before the restart)', flush=True)
        try:
            yt.start()
        except ValueError as e:
            print('resume failed: %s' % e, flush=True)
    print('fm-tuner on %s:%d, tuned %s MHz' % (','.join(addrs), PORT, fmt(chip.frequency())), flush=True)
    try:
        httpd.serve_forever()
    finally:
        # chip first: yt.shutdown() can take several seconds, and fm-tuner.sh sends SIGKILL after ~6 s,
        # which would leave the chip powered on with its firmware link gone (tune then fails with EINVAL)
        chip.power_off()
        yt.shutdown()
        print('fm-tuner stopped, chip off', flush=True)


if __name__ == '__main__':
    main()
