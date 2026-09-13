"""
Quick checks for fall_episode.py. Run:  python test_fall_episode.py
No model, camera, or Streamlit needed.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from fall_episode import (FallEpisode, build_payload, classify_posture,
                          keypoint_motion, make_sample_episode, send_to_n8n)

THR = 0.15


def approx(a, b, tol=1e-6):
    assert abs(a - b) < tol, "{} != {}".format(a, b)


def test_posture():
    assert classify_posture(False, 90, 20, 60) == "not_visible"
    assert classify_posture(True, 85, 20, 60) == "lying"
    assert classify_posture(True, 60, 20, 60) == "lying"          # boundary inclusive
    assert classify_posture(True, 45, 20, 60) == "partially_upright"
    assert classify_posture(True, 5, 20, 60) == "partially_upright"  # upright but unconfirmed


def test_motion():
    prev = np.zeros((17, 3)); prev[:, 2] = 0.9
    curr = prev.copy()
    bbox = (0, 0, 300, 400)                    # diagonal 500
    assert keypoint_motion(prev, curr, bbox, 0.125) == 0.0
    curr[:, 0] += 10                            # every keypoint moved 10 px in 0.125 s
    approx(keypoint_motion(prev, curr, bbox, 0.125), 10 / 500 / 0.125)   # 0.16 /s
    # confidence-weighted: three trusted joints that stay put outweigh fourteen
    # untrusted ones that jump -> small motion, not the 10-px figure
    low = prev.copy(); low[:, 2] = 0.1; low[:3, 2] = 0.9
    mixed = curr.copy(); mixed[:3, 0] = prev[:3, 0]          # trusted joints did not move
    assert keypoint_motion(low, mixed, bbox, 0.125) < 0.08      # unweighted would be 0.16
    # almost nothing trusted at all -> unknown
    none = prev.copy(); none[:, 2] = 0.05
    assert keypoint_motion(none, none, bbox, 0.125) is None
    assert keypoint_motion(None, curr, bbox, 0.125) is None
    assert keypoint_motion(prev, curr, bbox, 0.0) is None
    assert keypoint_motion(prev, curr, (0, 0, 0, 0), 0.125) is None


def test_accounting_sums():
    ep = FallEpisode(10.0, {"detector": True}, {"level2_sec": 10}, {"mode": "upload", "name": "x.mp4"})
    t = 10.0
    for _ in range(80):        # 10 s lying, motionless
        t += 0.125; ep.update(t, "lying", 0.01, THR)
    for _ in range(8):         # 1 s not visible (motion None must NOT count as motionless)
        t += 0.125; ep.update(t, "not_visible", None, THR)
    for _ in range(16):        # 2 s partially upright, moving
        t += 0.125; ep.update(t, "partially_upright", 0.5, THR)
    for _ in range(4):         # 0.5 s visible but motion unknown
        t += 0.125; ep.update(t, "lying", None, THR)
    approx(ep.time_on_ground_sec, 13.5)
    approx(ep.posture_sec["lying"], 10.5)
    approx(ep.posture_sec["not_visible"], 1.0)
    approx(ep.posture_sec["partially_upright"], 2.0)
    approx(sum(ep.posture_sec.values()), ep.time_on_ground_sec)
    approx(ep.motionless_sec, 10.0)
    approx(ep.motion_unknown_sec, 0.5)
    assert ep.max_alert_level == 1 and ep.status == "on_ground"

    ep.add_alert(2, 20.0); ep.add_alert(3, 40.0)
    assert ep.max_alert_level == 3
    ep.close("recovered", t)
    assert ep.status == "recovered"
    approx(ep.time_on_ground_sec, 13.5)
    # closed episodes ignore further updates
    ep.update(t + 5, "lying", 0.0, THR)
    approx(ep.time_on_ground_sec, 13.5)


def test_unresolved_and_serialisable():
    ep = FallEpisode(0.0, {}, {}, {"mode": "camera", "name": "webcam"})
    ep.update(3.0, "lying", 0.0, THR)
    ep.close("unresolved", 3.0)
    d = build_payload("episode_closed", ep, include_snapshots=True)
    json.dumps(d)                              # must be JSON-serialisable (no numpy types)
    assert d["episode"]["status"] == "unresolved"
    assert d["episode"]["time_on_ground_sec"] == 3.0
    assert d["snapshots"]["fall_frame_jpeg_base64"] is None


def test_sample_episode():
    ep = make_sample_episode()
    d = ep.to_dict()
    approx(d["time_on_ground_sec"], 34.0)
    approx(d["posture_breakdown_sec"]["lying"], 28.0)
    approx(d["time_motionless_sec"], 26.0)
    assert d["max_alert_level"] == 3 and d["status"] == "recovered"
    json.dumps(build_payload("episode_closed", ep))


# ---------------------------------------------------------------------------
# v10: fall clip (short mp4 of the seconds around the fall) for Telegram
# ---------------------------------------------------------------------------

def _frame(i, w=160, h=90):
    """A tiny synthetic frame with a moving red block, so the clip has motion."""
    import cv2
    f = np.zeros((h, w, 3), dtype=np.uint8)
    cv2.rectangle(f, (i * 5 % w, 20), (i * 5 % w + 30, 60), (0, 0, 255), -1)
    return f


def test_clip_buffer_keeps_only_pre_window():
    from fall_episode import ClipBuffer
    buf = ClipBuffer(pre_sec=2.0)
    for i in range(40):                      # 0.0 .. 3.9 s at 10 fps
        jpeg = buf.push(i * 0.1, _frame(i))
        assert isinstance(jpeg, bytes) and jpeg[:2] == b"\xff\xd8"   # JPEG magic
    frames = buf.snapshot()
    approx(frames[-1][0], 3.9)
    assert frames[0][0] >= 1.9 - 1e-9, frames[0][0]          # nothing older than 2 s
    assert 19 <= len(frames) <= 21
    buf.clear()
    assert buf.snapshot() == []


def test_encode_clip_roundtrip():
    import base64, os, tempfile
    import cv2
    from fall_episode import ClipBuffer, encode_clip
    buf = ClipBuffer(pre_sec=10)
    for i in range(24):
        buf.push(i / 8.0, _frame(i))         # 3 s at 8 fps
    res = encode_clip(buf.snapshot())
    if res is None:
        print("SKIP test_encode_clip_roundtrip: no mp4 encoder available")
        return
    b64, meta = res
    data = base64.b64decode(b64)
    assert data[4:8] == b"ftyp", data[:12]                    # ISO-BMFF / mp4 container
    assert meta["frames"] == 24
    assert abs(meta["fps"] - 8.0) < 0.5, meta
    assert abs(meta["duration_sec"] - 3.0) < 0.3, meta
    assert meta["codec"] in ("h264", "avc1", "mp4v"), meta
    assert meta["width"] % 2 == 0 and meta["height"] % 2 == 0
    fd, path = tempfile.mkstemp(suffix=".mp4"); os.close(fd)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        cap = cv2.VideoCapture(path)
        n = 0
        while True:
            ok, _ = cap.read()
            if not ok:
                break
            n += 1
        cap.release()
    finally:
        os.unlink(path)
    assert n >= 20, "decoded only {} frames".format(n)
    # too little material -> None, never an exception
    assert encode_clip([]) is None
    assert encode_clip(buf.snapshot()[:1]) is None


def test_episode_clip_recording_and_payload():
    from fall_episode import ClipBuffer, encode_clip
    ep = make_sample_episode()
    ep2 = FallEpisode(5.0, {}, {}, {"mode": "upload", "name": "x.mp4"})
    # no clip yet -> payload has no "clip" key even when asked for
    d = build_payload("fall_analysed", ep2, include_clip=True)
    assert "clip" not in d and "snapshots" not in d
    buf = ClipBuffer(pre_sec=5.0)
    for i in range(40):                       # 0 .. 4.875 s before the fall
        buf.push(i / 8.0, _frame(i))
    ep2.start_clip(buf.snapshot(), post_sec=2.0)
    assert ep2.clip_b64 is None
    t = 5.0
    for i in range(40, 60):                   # 2.5 s after the fall
        jpeg = buf.push(t, _frame(i))
        ep2.record_clip_frame(t, jpeg)
        t += 0.125
    if encode_clip(buf.snapshot()) is None:
        print("SKIP clip payload checks: no mp4 encoder available")
        return
    assert ep2.clip_b64 is not None            # finalised automatically after post_sec
    assert ep2.clip_meta["pre_fall_sec"] >= 4.5 and 1.9 <= ep2.clip_meta["post_fall_sec"] <= 2.2, ep2.clip_meta
    d = build_payload("fall_analysed", ep2, include_clip=True)
    assert d["clip"]["mp4_base64"] == ep2.clip_b64
    assert d["clip"]["duration_sec"] == ep2.clip_meta["duration_sec"]
    assert d["clip"]["codec"] == ep2.clip_meta["codec"]
    json.dumps(d)
    # not asked for -> not included (payload size stays small)
    assert "clip" not in build_payload("fall_analysed", ep2)
    # finalize_clip() before post_sec: uses whatever has been collected so far
    ep3 = FallEpisode(5.0, {}, {}, {"mode": "upload", "name": "x.mp4"})
    ep3.start_clip([f for f in buf.snapshot() if f[0] < 5.0], post_sec=30.0)
    ep3.record_clip_frame(5.0, _frame(1) is not None and buf.push(99.0, _frame(1)))
    ep3.finalize_clip()
    assert ep3.clip_b64 is not None and ep3.clip_meta["post_fall_sec"] < 1.0
    ep3.finalize_clip()                        # idempotent
    # sample clip for the "Send test payload" button
    from fall_episode import make_sample_clip
    res = make_sample_clip()
    assert res is not None
    b64, meta = res
    assert meta["frames"] >= 16 and len(b64) > 100


class _Handler(BaseHTTPRequestHandler):
    received = []
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        _Handler.received.append(json.loads(self.rfile.read(n)))
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass


def test_send_to_n8n_background_thread():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:{}/webhook/fallguard".format(srv.server_port)
    log = []
    t0 = time.time()
    send_to_n8n(url, build_payload("test", make_sample_episode()), log=log)
    assert time.time() - t0 < 0.05          # must not block the caller
    for _ in range(50):
        if log: break
        time.sleep(0.05)
    assert log and log[0]["ok"] is True and log[0]["status"] == 200, log
    assert _Handler.received[0]["event_type"] == "test"
    # unreachable URL -> logged as error, still non-blocking
    log2 = []
    send_to_n8n("http://127.0.0.1:9/nope", {"event_type": "test", "episode": {}}, log=log2, timeout=1)
    for _ in range(60):
        if log2: break
        time.sleep(0.05)
    assert log2 and log2[0]["ok"] is False
    srv.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("all tests passed")
