"""Headless driver for the Qualcomm radio-iris FM chip (/dev/radio0) on the Redmi Go.

Replays the power-on sequence observed with strace on the stock com.caf.fmradio app.
The driver ties chip power to the open fd: keep this object alive, never open/close
/dev/radio0 from any other process while it is running.
"""
import ctypes
import fcntl
import os
import struct

VIDIOC_QUERYCAP = 0x80685600
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_G_TUNER = 0xC054561D
VIDIOC_S_TUNER = 0x4054561E
VIDIOC_G_FREQUENCY = 0xC02C5638
VIDIOC_S_FREQUENCY = 0x402C5639
VIDIOC_S_EXT_CTRLS = 0xC0185648

CID_STATE = 0x8000004          # 1 = FM receive on, 0 = off
CID_CAL = 0x0098092A           # decoded by strace as V4L2_CID_COLORFX_CBCR; carries Riva_fm_cal
CAL_FILE = '/data/misc/fm/Riva_fm_cal'

# (control id, value) pairs, in the order the stock app writes them after power-on
INIT_BEFORE_CAL = [(0x8000029, 0)]
INIT_AFTER_CAL = [(0x800000C, 0), (0x800000D, 0), (0x800000E, 0), (0x800002B, 0),
                  (0x8000007, 0), (0x8000007, 0), (0x8000011, 0), (0x8000006, 64),
                  (0x800000F, 1), (0x8000010, 239), (0x800000F, 1), (0x800001B, 1),
                  (0x8000012, 0), (0x800002F, 4)]

UNIT_PER_MHZ = 16000           # tuner reports in 62.5 Hz units (V4L2_TUNER_CAP_LOW)
MIN_MHZ, MAX_MHZ = 87.5, 107.9

# RDS: the driver queues decoded data in private buffers read with VIDIOC_DQBUF (index = buffer id).
VIDIOC_DQBUF = 0xC0445611
BUF_TYPE_PRIVATE = 0x80
IDX_RT, IDX_PS, IDX_RAW = 2, 3, 4   # RadioText, Programme Service name, raw groups (verified by probing)


def _clean_text(raw):
    """RDS text -> printable ASCII, single spaces."""
    s = ''.join(chr(c) if 32 <= c < 127 else ' ' for c in raw)
    return ' '.join(s.split())


class FmChip:
    def __init__(self, path='/dev/radio0'):
        self.path = path
        self.fd = None

    def _set_ctrl(self, cid, value):
        fcntl.ioctl(self.fd, VIDIOC_S_CTRL, struct.pack('Ii', cid, value))

    def _set_cal(self, blob):
        buf = ctypes.create_string_buffer(blob, len(blob))
        ctrl = struct.pack('<IIIQ', CID_CAL, len(blob), 0, ctypes.addressof(buf))[:20]
        ctrl_buf = ctypes.create_string_buffer(ctrl, len(ctrl))
        ext = struct.pack('<IIIIII', 0x980000, 1, 0, 0, 0, ctypes.addressof(ctrl_buf))
        fcntl.ioctl(self.fd, VIDIOC_S_EXT_CTRLS, bytearray(ext))

    def power_on(self, mhz=93.5):
        self.fd = os.open(self.path, os.O_RDONLY)
        self._set_ctrl(CID_STATE, 1)
        for cid, val in INIT_BEFORE_CAL:
            self._set_ctrl(cid, val)
        try:
            with open(CAL_FILE, 'rb') as f:
                self._set_cal(f.read(23))
        except (OSError, IOError):
            pass
        for cid, val in INIT_AFTER_CAL:
            self._set_ctrl(cid, val)
        self.tune(mhz)

    def power_off(self):
        if self.fd is not None:
            try:
                self._set_ctrl(CID_STATE, 0)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None

    def tune(self, mhz):
        if not (MIN_MHZ <= mhz <= MAX_MHZ):
            raise ValueError('frequency out of range 87.5-107.9 MHz')
        raw = int(round(mhz * UNIT_PER_MHZ))
        fcntl.ioctl(self.fd, VIDIOC_S_FREQUENCY, struct.pack('<III', 0, 1, raw).ljust(44, b'\0'))

    def frequency(self):
        buf = bytearray(44)
        fcntl.ioctl(self.fd, VIDIOC_G_FREQUENCY, buf)
        return struct.unpack_from('<I', buf, 8)[0] / UNIT_PER_MHZ

    def status(self):
        buf = bytearray(84)
        fcntl.ioctl(self.fd, VIDIOC_G_TUNER, buf)
        rx, aud, signal = struct.unpack_from('<IIi', buf, 52)
        return {'signal': signal, 'stereo': bool(rx & 2), 'audmode': aud}

    def _read_buf(self, index, length=256):
        """Non-blocking read of one RDS buffer (empty bytes when nothing new is queued)."""
        data = ctypes.create_string_buffer(length)
        b = bytearray(68)
        struct.pack_into('<II', b, 0, index, BUF_TYPE_PRIVATE)
        struct.pack_into('<I', b, 48, 2)                          # memory = V4L2_MEMORY_USERPTR
        struct.pack_into('<II', b, 52, ctypes.addressof(data), length)
        fcntl.ioctl(self.fd, VIDIOC_DQBUF, b)
        return bytes(data.raw[:struct.unpack_from('<I', b, 8)[0]])

    def drain_rds(self):
        """Throw away queued RDS data (call right after tuning so the old station's text is not shown)."""
        for idx in (IDX_PS, IDX_RT, IDX_RAW):
            for _ in range(32):
                try:
                    if not self._read_buf(idx):
                        break
                except OSError:
                    break

    def read_rds(self):
        """Return {'ps': str, 'rt': str, 'pi': int, 'pty': int} for whatever new data arrived (maybe empty).
        PS buffer: [0]=count [1]=PTY [2:4]=PI [4]=- [5:]=8-char name.  RT buffer: [0]=length [1]=PTY [2:4]=PI [4]=A/B [5:]=text."""
        out = {}
        for idx, key in ((IDX_PS, 'ps'), (IDX_RT, 'rt')):
            try:
                raw = self._read_buf(idx)
            except OSError:
                continue
            if len(raw) <= 5 or raw[1] > 31:           # PTY is 0-31; larger = a half-received/garbled group
                continue
            pi = (raw[2] << 8) | raw[3]
            if pi == 0x2020:                           # two spaces where the PI code should be = garbage
                continue
            body = raw[5:5 + 8] if key == 'ps' else raw[5:5 + max(0, min(raw[0], 64))]
            text = _clean_text(body)
            if text:
                out[key] = text
                out['pty'] = raw[1]
                if pi:
                    out['pi'] = pi
                    out['pi_' + key] = pi
        return out
