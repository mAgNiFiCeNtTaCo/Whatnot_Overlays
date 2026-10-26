"""Reader for Taco Cam's scanner feed: the iPhone's newest full-quality picture, straight from Taco Cam
(D:\\ClaudeProjects\\Webcam) through shared memory, without OBS and without JPEG compression.

Layout v2 (Taco Cam CONTRACT.md section 8, pc/tacocam/framefeed.py there): shared memory "TacoCamFeed2",
little-endian. 0 magic "TCF2" | 4 version u32 | 8 want f64 (the reader writes time.time() + 2 to ask for
pictures) | 16 latest u32 (slot 0/1 with the newest picture) | 64 + 32*i slot header: seq u64 (odd while
written), width u32, height u32, format u32 (2 = BGR24, already upright), pad u32, time f64 |
128 + i * 3840*2160*3 slot pixels.
"""
import struct
import time
from multiprocessing import shared_memory

import numpy as np

FEED_NAME = "TacoCamFeed2"
MAGIC = b"TCF2"
SLOT_BYTES = 3840 * 2160 * 3
DATA_OFF = 128
WANT_OFF, LATEST_OFF = 8, 16
SLOT_HDR = struct.Struct("<QIIIId")
FMT_BGR24 = 2


class FeedReader:
    def __init__(self, name=FEED_NAME):
        self.shm = shared_memory.SharedMemory(name=name)   # FileNotFoundError: Taco Cam isn't running
        if bytes(self.shm.buf[:4]) != MAGIC:
            self.shm.close()
            raise ValueError("not a Taco Cam feed (or an older Taco Cam)")

    def read(self, max_age=2.0, tries=5):
        """(BGR upright picture, picture id) of the newest picture, or None when there is no fresh one.
        Every call also tells Taco Cam to keep sending pictures for the next 2 seconds."""
        buf = self.shm.buf
        struct.pack_into("<d", buf, WANT_OFF, time.time() + 2.0)
        for _ in range(tries):
            (i,) = struct.unpack_from("<I", buf, LATEST_OFF)
            off = 64 + 32 * i
            s1, w, h, fmt, _pad, t = SLOT_HDR.unpack_from(buf, off)
            if s1 % 2 or not w or not h or fmt != FMT_BGR24:
                if not s1:
                    return None
                time.sleep(0.002)
                continue
            if time.time() - t > max_age:
                return None
            img = np.frombuffer(buf, np.uint8, count=w * h * 3, offset=DATA_OFF + i * SLOT_BYTES).reshape(h, w, 3).copy()
            if SLOT_HDR.unpack_from(buf, off)[0] == s1:
                return img, (i, s1)
        return None

    def close(self):
        self.shm.close()
