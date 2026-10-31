"""YouTube Live streaming service: supervises one ffmpeg that pushes backdrop image + text overlay
+ the lossless FM audio (Icecast /fm.flac) to YouTube over RTMP(S).

- stream key is write-only: stored in KEY_FILE (mode 600), never returned, redacted from logs
- settings (non-secret) live in SETTINGS_FILE
- ffmpeg is restarted automatically (backoff 2..15 s) until stop() is called; a watchdog kills a stalled one
"""
import collections
import json
import os
import re
import shutil
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, 'yt.json')
KEY_FILE = '/root/yt.key'
BACKDROP = os.path.join(HERE, 'backdrop.jpg')
LIB_DIR = os.path.join(HERE, 'library')
LIB_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,60}\.jpg$')
RUN_DIR = '/run/fm'
OVERLAY_FILE = RUN_DIR + '/overlay.txt'
OVERLAY2_FILE = RUN_DIR + '/overlay2.txt'
NOW_FILE = RUN_DIR + '/now.txt'
STATION_FILE = RUN_DIR + '/station.txt'   # RDS station name (or preset name) - written by fm_tuner
RT1_FILE = RUN_DIR + '/rt1.txt'           # RDS RadioText, first line
RT2_FILE = RUN_DIR + '/rt2.txt'           # RDS RadioText, second line
AUDIO_URL = 'http://127.0.0.1:8000/fm.flac'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
MAX_IMAGE = 5 * 1024 * 1024

RESOLUTIONS = {'640x360': (640, 360), '854x480': (854, 480), '1280x720': (1280, 720)}
FPS_CHOICES = (2, 5, 10, 15, 24, 30)
AUDIO_KBPS = (128, 192, 256, 320)
DEFAULTS = {
    'rtmp_url': 'rtmps://a.rtmps.youtube.com:443/live2',
    'resolution': '854x480',
    'fps': 5,
    'video_kbps': 800,
    'audio_kbps': 256,
    'text': '',
    'show_freq': True,
    'show_ps': True,    # RDS station name on the video
    'show_rt': True,    # RDS RadioText (e.g. current song) on the video
    'resume': False,    # True while the user wants the stream live; survives daemon restarts/reboots
}
URL_RE = re.compile(r'^rtmps?://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9_./-]*)?$')
STALL_SECONDS = 20


def _clean_text(s):
    s = ''.join(ch for ch in str(s) if ch == '\n' or ch >= ' ')
    return '\n'.join(s.split('\n')[:2])[:200]    # at most two lines


class YouTube:
    def __init__(self):
        self.lock = threading.Lock()
        self.settings = dict(DEFAULTS)
        self.log = collections.deque(maxlen=60)
        self.desired = False
        self.proc = None
        self.thread = None
        self.state = 'stopped'
        self.started_at = None
        self.restarts = 0
        self.last_error = ''
        self.progress = {}
        self.last_progress = 0.0
        self._load()
        os.makedirs(RUN_DIR, exist_ok=True)
        self._write_overlay()
        for p in (NOW_FILE, STATION_FILE, RT1_FILE, RT2_FILE, OVERLAY2_FILE):
            if not os.path.exists(p):
                open(p, 'w').close()

    # ---- persistence -------------------------------------------------
    def _load(self):
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            for k in DEFAULTS:
                if k in data:
                    self.settings[k] = data[k]
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = SETTINGS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.settings, f)
        os.replace(tmp, SETTINGS_FILE)

    def _write_overlay(self):
        """Caption = up to two lines; each line has its own file so drawtext can centre it."""
        lines = (self.settings['text'].split('\n') + ['', ''])[:2]
        for path, line in ((OVERLAY_FILE, lines[0]), (OVERLAY2_FILE, lines[1])):
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(line.strip())
            os.replace(tmp, path)

    # ---- secret key --------------------------------------------------
    def _key(self):
        try:
            with open(KEY_FILE) as f:
                return f.read().strip()
        except OSError:
            return ''

    def set_key(self, key):
        key = str(key).strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', key):
            raise ValueError('stream key looks invalid (letters, digits, - and _ only)')
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(key)
        os.chmod(KEY_FILE, 0o600)

    def key_hint(self):
        k = self._key()
        return {'set': bool(k), 'last4': k[-4:] if len(k) >= 12 else ''}

    def _redact(self, line):
        k = self._key()
        if k:
            line = line.replace(k, '****')
        return re.sub(r'(rtmps?://[^/\s]+/[^/\s]+/)[^\s]+', r'\1****', line)

    # ---- settings ----------------------------------------------------
    def update_settings(self, d):
        new = dict(self.settings)
        if 'rtmp_url' in d:
            url = str(d['rtmp_url']).strip().rstrip('/')
            m = URL_RE.match(url)
            host = re.sub(r'^rtmps?://([^/:]+).*$', r'\1', url)
            if not m or not host.endswith('.youtube.com'):
                raise ValueError('RTMP URL must be rtmp(s)://<something>.youtube.com/...')
            new['rtmp_url'] = url
        if 'resolution' in d:
            if d['resolution'] not in RESOLUTIONS:
                raise ValueError('resolution must be one of %s' % ', '.join(RESOLUTIONS))
            new['resolution'] = d['resolution']
        if 'fps' in d:
            if int(d['fps']) not in FPS_CHOICES:
                raise ValueError('fps must be one of %s' % (FPS_CHOICES,))
            new['fps'] = int(d['fps'])
        if 'video_kbps' in d:
            v = int(d['video_kbps'])
            if not 200 <= v <= 4000:
                raise ValueError('video bitrate must be 200-4000 kbps')
            new['video_kbps'] = v
        if 'audio_kbps' in d:
            if int(d['audio_kbps']) not in AUDIO_KBPS:
                raise ValueError('audio bitrate must be one of %s' % (AUDIO_KBPS,))
            new['audio_kbps'] = int(d['audio_kbps'])
        if 'text' in d:
            new['text'] = _clean_text(d['text'])
        for flag in ('show_freq', 'show_ps', 'show_rt'):
            if flag in d:
                new[flag] = bool(d[flag])
        with self.lock:
            changed_encode = any(new[k] != self.settings[k] for k in
                                 ('rtmp_url', 'resolution', 'fps', 'video_kbps', 'audio_kbps', 'show_freq'))
            self.settings = new
            self._save()
            self._write_overlay()   # text is reloaded live by drawtext, no restart needed
        if changed_encode and self.desired:
            self._kill_proc()       # supervisor restarts it with the new settings

    # ---- backdrop image ---------------------------------------------
    def set_backdrop(self, data):
        if len(data) > MAX_IMAGE:
            raise ValueError('image too large (max 5 MB)')
        if not (data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n'):
            raise ValueError('only JPEG or PNG images are accepted')
        src = os.path.join(RUN_DIR, 'upload.bin')
        out = BACKDROP + '.tmp.jpg'
        with open(src, 'wb') as f:
            f.write(data)
        try:
            r = subprocess.run(['ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-i', src,
                                '-vf', "scale='min(1280,iw)':-2", '-frames:v', '1', '-q:v', '3', out],
                               capture_output=True, timeout=60)
            if r.returncode != 0 or not os.path.exists(out):
                raise ValueError('could not decode that image')
            os.replace(out, BACKDROP)
        finally:
            for p in (src, out):
                try:
                    os.remove(p)
                except OSError:
                    pass
        if self.desired:
            self._kill_proc()       # restart so the new image is used

    # ---- built-in backdrop library -------------------------------------
    def library(self):
        try:
            return sorted(n for n in os.listdir(LIB_DIR) if LIB_NAME_RE.match(n))
        except OSError:
            return []

    def library_path(self, name):
        if name not in self.library():          # exact match against the directory listing: no path tricks
            raise ValueError('unknown library image')
        return os.path.join(LIB_DIR, name)

    def select_library(self, name):
        shutil.copyfile(self.library_path(name), BACKDROP + '.tmp.jpg')
        os.replace(BACKDROP + '.tmp.jpg', BACKDROP)
        if self.desired:
            self._kill_proc()                   # restart so the new image is used

    def ensure_backdrop(self):
        if os.path.exists(BACKDROP):
            return
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'color=c=0x101820:s=1280x720', '-frames:v', '1', BACKDROP],
                       capture_output=True, timeout=60)

    # ---- ffmpeg ------------------------------------------------------
    def _prerender(self, w, h):
        """Scale/pad the backdrop to the output size once, so ffmpeg does not decode and
        rescale the full-size image for every frame (that alone cost ~35 ms of CPU per frame)."""
        self.ensure_backdrop()
        out = os.path.join(HERE, 'backdrop_%dx%d.jpg' % (w, h))
        try:
            if os.path.getmtime(out) >= os.path.getmtime(BACKDROP):
                return out
        except OSError:
            pass
        tmp = out + '.tmp.jpg'
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-i', BACKDROP, '-vf',
                        'scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black'
                        % (w, h, w, h), '-frames:v', '1', '-q:v', '2', tmp], capture_output=True, timeout=60)
        os.replace(tmp, out)
        return out

    def _cmd(self):
        s = self.settings
        w, h = RESOLUTIONS[s['resolution']]
        fps = s['fps']
        vb = s['video_kbps']
        fs_txt = max(14, h // 16)
        fs_freq = max(18, h // 10)
        image = self._prerender(w, h)
        base = "[0:v]scale=in_range=full:out_range=limited,format=yuv420p"
        def dt(path, size, y, border):
            # expansion=none: the text comes from files that contain untrusted over-the-air RDS data,
            # so %{...} sequences must never be interpreted by drawtext
            return (",drawtext=fontfile=%s:textfile=%s:reload=1:expansion=none:fontsize=%d:fontcolor=white:"
                    "borderw=%d:bordercolor=black:x=(w-text_w)/2:y=%s" % (FONT, path, size, border, y))
        fs_ps = max(18, h // 11)
        fs_rt = max(14, h // 22)
        graph = base + dt(OVERLAY_FILE, fs_txt, 'h*0.04', 2)
        graph += dt(OVERLAY2_FILE, int(fs_txt * 0.8), 'h*0.04+%d' % int(fs_txt * 1.35), 2)
        graph += dt(STATION_FILE, fs_ps, 'h*0.50', 3)
        graph += dt(RT1_FILE, fs_rt, 'h*0.64', 2)
        graph += dt(RT2_FILE, fs_rt, 'h*0.64+%d' % int(fs_rt * 1.3), 2)
        if s['show_freq']:
            graph += dt(NOW_FILE, fs_freq, 'h-text_h-h*0.06', 3)
        graph += "[v]"
        url = s['rtmp_url'] + '/' + self._key()
        return ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning', '-progress', 'pipe:1', '-nostats',
                '-re', '-loop', '1', '-framerate', str(fps), '-i', image,
                '-thread_queue_size', '1024', '-i', AUDIO_URL,
                '-filter_complex', graph, '-map', '[v]', '-map', '1:a',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'stillimage', '-pix_fmt', 'yuv420p',
                '-threads', '1',
                '-r', str(fps), '-g', str(fps * 2), '-keyint_min', str(fps * 2), '-sc_threshold', '0',
                '-b:v', '%dk' % vb, '-maxrate', '%dk' % vb, '-bufsize', '%dk' % (vb * 2),
                '-c:a', 'aac', '-aac_coder', 'fast', '-b:a', '%dk' % s['audio_kbps'], '-ar', '48000', '-ac', '2',
                '-rw_timeout', '10000000', '-f', 'flv', url]

    def _kill_proc(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def _read_stdout(self, p):
        block = {}
        for line in p.stdout:
            line = line.strip()
            if '=' in line:
                k, v = line.split('=', 1)
                block[k] = v
                if k == 'progress':
                    self.progress = block
                    self.last_progress = time.time()
                    block = {}

    def _read_stderr(self, p):
        for line in p.stderr:
            line = self._redact(line.rstrip())
            if line and 'deprecated pixel format' not in line:
                self.log.append(time.strftime('%H:%M:%S ') + line)
                self.last_error = line

    def _supervise(self):
        backoff = 2
        while self.desired:
            if not self._key():
                self.state = 'error'
                self.last_error = 'no stream key set'
                self.desired = False
                break
            try:
                self.ensure_backdrop()
                cmd = self._cmd()
                self.state = 'starting'
                self.progress = {}
                self.last_progress = time.time()
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            except Exception as e:  # noqa: BLE001
                self.last_error = self._redact(str(e))
                time.sleep(backoff)
                continue
            self.proc = p
            try:
                with open('/proc/%d/oom_score_adj' % p.pid, 'w') as f:
                    f.write('-800')
            except OSError:
                pass
            t0 = time.time()
            threading.Thread(target=self._read_stdout, args=(p,), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(p,), daemon=True).start()
            while p.poll() is None and self.desired:
                if self.progress and self.state != 'live':
                    self.state = 'live'
                if time.time() - self.last_progress > STALL_SECONDS:
                    self.log.append(time.strftime('%H:%M:%S ') + 'watchdog: no progress, restarting ffmpeg')
                    p.kill()
                    break
                time.sleep(1)
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
            if not self.desired:
                break
            self.restarts += 1
            self.state = 'retrying'
            if time.time() - t0 > 60:
                backoff = 2
            time.sleep(backoff)
            backoff = min(15, backoff * 2)
        self.state = 'stopped' if self.state != 'error' else 'error'
        self.proc = None
        self.started_at = None

    # ---- public control ----------------------------------------------
    def start(self):
        with self.lock:
            if self.desired and self.thread and self.thread.is_alive():
                return
            if not self._key():
                raise ValueError('set the stream key first')
            self.desired = True
            self.restarts = 0
            self.last_error = ''
            self.started_at = time.time()
            self.settings['resume'] = True
            self._save()
            self.thread = threading.Thread(target=self._supervise, daemon=True)
            self.thread.start()

    def _halt(self):
        self.desired = False
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
        if self.thread:
            self.thread.join(10)
        self.state = 'stopped'

    def stop(self):
        """User pressed Stop: also forget that the stream should come back after a restart."""
        self._halt()
        self.settings['resume'] = False
        self._save()

    def shutdown(self):
        """Daemon is exiting: stop ffmpeg but keep the 'resume' flag so the stream returns on next start."""
        self._halt()

    def should_resume(self):
        return bool(self.settings.get('resume')) and bool(self._key())

    def status(self):
        pr = self.progress or {}
        kbps = pr.get('bitrate', '')
        return {
            'state': self.state,
            'desired': self.desired,
            'uptime_s': int(time.time() - self.started_at) if self.started_at and self.state in ('live', 'starting', 'retrying') else 0,
            'restarts': self.restarts,
            'fps': pr.get('fps', ''),
            'bitrate': kbps,
            'speed': pr.get('speed', ''),
            'dropped': pr.get('drop_frames', ''),
            'last_error': self.last_error,
            'key': self.key_hint(),
            'settings': dict(self.settings),
            'choices': {'resolutions': list(RESOLUTIONS), 'fps': list(FPS_CHOICES), 'audio_kbps': list(AUDIO_KBPS)},
            'log': list(self.log)[-25:],
        }
