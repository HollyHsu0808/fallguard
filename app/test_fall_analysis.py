"""
Checks for the v9 fall-analysis layer. Run:  python test_fall_analysis.py
Synthetic side-view skeletons (image y grows downward, floor at y = 450,
stature 300 px) built from the Drillis & Contini proportions.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from fall_episode import (ANALYSIS_PARAMS, FallAnalyser, TORSO_FRACTION_OF_HEIGHT,
                          build_payload, classify_prefall_posture, frame_geometry,
                          knee_angle_deg, make_sample_episode, send_to_n8n,
                          torso_angle_deg)

FLOOR, H = 450.0, 300.0
LYING_ANGLE = 60


def skel(points, conf=0.9):
    """points: dict index -> (x, y); everything else confidence 0."""
    k = np.zeros((17, 3))
    for i, (x, y) in points.items():
        k[i] = (x, y, conf)
    return k


def standing(x=200.0, head=True):
    pts = {5: (x, FLOOR - .818 * H), 6: (x + 6, FLOOR - .818 * H),
           11: (x, FLOOR - .530 * H), 12: (x + 6, FLOOR - .530 * H),
           13: (x, FLOOR - .285 * H), 14: (x + 6, FLOOR - .285 * H),
           15: (x, FLOOR - .039 * H), 16: (x + 6, FLOOR - .039 * H)}
    if head:
        pts.update({0: (x + 8, FLOOR - .936 * H), 1: (x + 4, FLOOR - .95 * H),
                    3: (x - 4, FLOOR - .94 * H)})
    return skel(pts)


def sitting(x=200.0):
    torso = TORSO_FRACTION_OF_HEIGHT * H
    hip_y = FLOOR - .27 * H
    pts = {11: (x, hip_y), 12: (x + 6, hip_y), 5: (x, hip_y - torso), 6: (x + 6, hip_y - torso),
           13: (x + 60, hip_y), 14: (x + 66, hip_y), 15: (x + 60, FLOOR - .039 * H), 16: (x + 66, FLOOR - .039 * H),
           0: (x + 8, hip_y - torso - 35), 1: (x + 4, hip_y - torso - 38)}
    return skel(pts)


def lying(x=100.0, y=FLOOR - 20):
    torso = TORSO_FRACTION_OF_HEIGHT * H
    pts = {0: (x, y), 1: (x + 3, y - 3), 5: (x + 40, y), 6: (x + 40, y + 6),
           11: (x + 40 + torso, y), 12: (x + 40 + torso, y + 6),
           13: (x + 40 + torso + 70, y), 14: (x + 40 + torso + 70, y + 6),
           15: (x + 40 + torso + 140, y), 16: (x + 40 + torso + 140, y + 6)}
    return skel(pts)


def rotated(theta_deg, x=200.0):
    """Standing skeleton rotated about the ankles by theta (0 = upright, 90 = flat)."""
    k = standing(x)
    th = np.radians(theta_deg)
    piv = np.array([x, FLOOR - .039 * H])
    for i in range(17):
        if k[i, 2] > 0:
            d = k[i, :2] - piv
            # rotate: vertical offset (-d[1], pointing up) becomes horizontal
            k[i, 0] = piv[0] + d[0] * np.cos(th) - d[1] * np.sin(th)
            k[i, 1] = piv[1] + d[0] * np.sin(th) + d[1] * np.cos(th)
    return k


def frames(seq, t0=0.0, dt=0.125):
    return [(t0 + i * dt, k, torso_angle_deg(k) if k is not None else None)
            for i, k in enumerate(seq)]


def approx(a, b, tol):
    assert a is not None and abs(a - b) <= tol, "{} vs {}".format(a, b)


def test_geometry():
    g = frame_geometry(standing())
    approx(g["torso_angle"], 0.0, 1.0)
    approx(g["knee_angle"], 180.0, 1.0)
    approx(g["hip_height_torso"], (.530 - .039) / .288, 0.05)
    assert g["head"] is not None
    approx(torso_angle_deg(lying()), 90.0, 1.0)
    approx(knee_angle_deg(sitting()), 90.0, 1.0)
    assert frame_geometry(skel({0: (1, 1)})) is None        # no torso -> None


def test_prefall_posture():
    P = ANALYSIS_PARAMS
    assert classify_prefall_posture([frame_geometry(standing())] * 4, LYING_ANGLE, P)[0] == "standing"
    assert classify_prefall_posture([frame_geometry(sitting())] * 4, LYING_ANGLE, P)[0] == "sitting"
    assert classify_prefall_posture([frame_geometry(lying())] * 4, LYING_ANGLE, P)[0] == "lying"
    assert classify_prefall_posture([frame_geometry(standing())] * 2, LYING_ANGLE, P)[0] == "unknown"


def test_fast_fall_is_high_risk_with_cm():
    # 2.5 s standing, 0.5 s fall (4 frames), then lying
    seq = [standing()] * 20 + [rotated(a) for a in (25, 50, 75, 90)] + [lying(x=200, y=FLOOR - 12)] * 4
    fr = frames(seq)
    t_trigger = fr[22][0]                       # detector fires two frames into the fall
    an = FallAnalyser(fr[:23], t_trigger, LYING_ANGLE, patient_height_cm=165)
    for t, k, a in fr[23:]:
        an.add_frame(t, k, a)
        if an.done:
            break
    assert an.done
    r = an.result()
    assert r["prefall"]["posture"] == "standing", r["prefall"]
    assert r["prefall"]["fall_height_category"] == "standing_height"
    approx(r["prefall"]["hip_height_cm"], (.530 - .039) * 165, 3.0)      # ~81 cm
    approx(r["prefall"]["head_height_cm"], (.936 - .039) * 165, 4.0)     # ~148 cm
    hi = r["head_impact"]
    assert hi["risk"] == "high", hi
    assert hi["head_reached_floor_level"] is True and hi["settled_lying"] is True
    assert hi["peak_descent_speed_torso_per_sec"] >= ANALYSIS_PARAMS["head_speed_high"]
    assert hi["peak_descent_speed_cm_per_sec"] is not None
    json.dumps(r)


def test_slow_lie_down_is_low_risk():
    # 2.5 s standing, then 5 s of gradual lowering (40 frames), then lying
    angles = np.linspace(4, 90, 40)
    seq = [standing()] * 20 + [rotated(a) for a in angles] + [lying(x=200, y=FLOOR - 12)] * 4
    fr = frames(seq)
    t_trigger = fr[56][0]                       # temporal rule fires late in a slow change
    an = FallAnalyser(fr[:57], t_trigger, LYING_ANGLE)
    for t, k, a in fr[57:]:
        an.add_frame(t, k, a)
        if an.done:
            break
    r = an.result()
    hi = r["head_impact"]
    assert hi["risk"] == "low", hi
    assert hi["peak_descent_speed_torso_per_sec"] < ANALYSIS_PARAMS["head_speed_medium"]
    assert r["prefall"]["hip_height_cm"] is None            # no height given -> no cm


def test_missing_head_uses_shoulder_proxy():
    # face keypoints never visible -> head position taken from the shoulders
    seq = [standing(head=False)] * 20 + [rotated(a) for a in (25, 50, 75, 90)] + [lying(x=200, y=FLOOR - 12)] * 4
    for k in seq:
        k[0:5, 2] = 0.0
    fr = frames(seq)
    an = FallAnalyser(fr[:22], fr[21][0], LYING_ANGLE)
    for t, k, a in fr[22:]:
        an.add_frame(t, k, a)
    r = an.result()
    hi = r["head_impact"]
    assert hi["risk"] == "high" and hi["head_source"] == "shoulders", hi
    assert any("shoulders" in x for x in hi["reasons"])
    assert r["prefall"]["head_height_cm"] is None            # true head height needs face keypoints

    # nobody visible before the fall -> posture unknown, analysis still returns
    fr2 = frames([None] * 20 + [rotated(90)] * 5)
    an2 = FallAnalyser(fr2[:21], fr2[20][0], LYING_ANGLE)
    for t, k, a in fr2[21:]:
        an2.add_frame(t, k, a)
    r2 = an2.result()
    assert r2["prefall"]["posture"] == "unknown" and r2["prefall"]["fall_height_category"] == "unknown"
    json.dumps(r2)


def test_scene_cut_is_skipped_not_counted_as_speed():
    # fast fall, but one frame in the middle shows a different person 300 px away
    seq = [standing()] * 20 + [rotated(25), rotated(50), standing(x=500), rotated(75), rotated(90)] \
          + [lying(x=200, y=FLOOR - 12)] * 4
    fr = frames(seq)
    an = FallAnalyser(fr[:22], fr[21][0], LYING_ANGLE)
    for t, k, a in fr[22:]:
        an.add_frame(t, k, a)
    hi = an.result()["head_impact"]
    assert hi["frames_skipped_as_cuts"] >= 2, hi                # into and out of the odd frame
    assert hi["peak_descent_speed_torso_per_sec"] < 12, hi     # no 300-px jump counted as speed
    assert hi["risk"] == "high", hi                            # the real fall frames still count


def test_quick_recovery_uses_feet_floor():
    # fast fall, hits the floor, but is back up within a second: never settles
    seq = [standing()] * 20 + [rotated(a) for a in (25, 50, 75, 90)] + [rotated(45), rotated(20)] + [standing()] * 12
    fr = frames(seq)
    an = FallAnalyser(fr[:22], fr[21][0], LYING_ANGLE)
    for t, k, a in fr[22:]:
        an.add_frame(t, k, a)
        if an.done:
            break
    hi = an.result()["head_impact"]
    assert hi["settled_lying"] is False and hi["floor_source"] == "feet", hi
    assert hi["risk"] == "high", hi
    assert any("feet" in x for x in hi["reasons"])


def test_tiny_person_is_unknown():
    # same fall scaled to a 12-px torso (a 240p clip with the person far away)
    def small(k):
        k2 = k.copy(); k2[:, :2] = k2[:, :2] * 0.14; return k2
    seq = [small(standing())] * 20 + [small(rotated(a)) for a in (25, 50, 75, 90)] + [small(lying(x=200, y=FLOOR - 12))] * 4
    fr = frames(seq)
    an = FallAnalyser(fr[:22], fr[21][0], LYING_ANGLE)
    for t, k, a in fr[22:]:
        an.add_frame(t, k, a)
    r = an.result()
    assert r["image_quality"]["torso_px"] < ANALYSIS_PARAMS["min_torso_px"]
    assert r["head_impact"]["risk"] == "unknown" and "too small" in r["head_impact"]["reasons"][0]
    assert r["prefall"]["posture"] == "standing"               # posture is scale-free, still reported


def test_floor_timing_starts_at_descent():
    # stood near the camera, fell further away: the lying body is higher in
    # the image, so before the descent gate the standing hips already counted
    # as "at floor level" and head_before_hip came out hugely negative
    far_lying = lying(x=150, y=250)
    seq = [standing()] * 20 + [far_lying] * 4
    fr = frames(seq)
    an = FallAnalyser(fr[:21], fr[20][0], LYING_ANGLE)
    for t, k, a in fr[21:]:
        an.add_frame(t, k, a)
    hi = an.result()["head_impact"]
    assert hi["settled_lying"] is True and hi["head_reached_floor_level"] is True, hi
    assert hi["descent_start_video_time_sec"] == fr[20][0], hi
    assert hi["head_before_hip_sec"] == 0.0, hi                 # not -2.5
    assert hi["risk"] == "high", hi


def test_fall_confidence_labels():
    from fall_episode import FallEpisode
    base = {"analysis_status": "done", "prefall": {"posture": "standing"},
            "head_impact": {"risk": "unknown", "settled_lying": False, "head_reached_floor_level": False}}
    # crouch that tripped the detector: upright again after 1.2 s, never lying, head never at floor
    ep = FallEpisode(10.0, {}, {}, {"mode": "upload", "name": "x.avi"})
    ep.update(11.2, "partially_upright", 0.5, 0.15)
    ep.set_analysis(dict(base))
    assert ep.event_features["fall_confidence"] == "unconfirmed"     # not recovered yet
    ep.close("recovered", 11.2)
    assert ep.event_features["fall_confidence"] == "likely_false_alarm"
    # same signals but the stream ended with the person still down -> unconfirmed
    ep2 = FallEpisode(10.0, {}, {}, {"mode": "upload", "name": "x.avi"})
    ep2.update(14.0, "partially_upright", 0.5, 0.15)
    ep2.close("unresolved", 14.0)
    ep2.set_analysis(dict(base))
    assert ep2.event_features["fall_confidence"] == "unconfirmed"
    # head reached floor level -> confirmed even with a quick recovery
    ep3 = FallEpisode(10.0, {}, {}, {"mode": "upload", "name": "x.avi"})
    ep3.update(11.0, "lying", 0.0, 0.15)
    ep3.close("recovered", 11.0)
    r = dict(base); r["head_impact"] = dict(base["head_impact"], head_reached_floor_level=True)
    ep3.set_analysis(r)
    assert ep3.event_features["fall_confidence"] == "confirmed"
    json.dumps(build_payload("episode_closed", ep3))


def test_timeout_and_finish():
    fr = frames([standing()] * 20 + [rotated(40)] * 30)     # never settles lying
    an = FallAnalyser(fr[:21], fr[20][0], LYING_ANGLE)
    for t, k, a in fr[21:]:
        an.add_frame(t, k, a)
    assert an.done                                          # via transition_timeout_sec
    r = an.result()
    assert r["head_impact"]["settled_lying"] is False and r["head_impact"]["risk"] == "unknown", r["head_impact"]
    an3 = FallAnalyser(fr[:21], fr[20][0], LYING_ANGLE)
    an3.finish()
    assert an3.done and an3.result()["analysis_status"] == "done"


class _Handler(BaseHTTPRequestHandler):
    seen = []
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        key = self.headers.get("X-FallGuard-Key")
        _Handler.seen.append((key, body["event_type"]))
        self.send_response(200 if key == "secret123" else 403)
        self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass


def test_auth_header():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:{}/webhook/fallguard".format(srv.server_port)
    payload = build_payload("test", make_sample_episode())
    log_ok, log_bad = [], []
    send_to_n8n(url, payload, log=log_ok, auth_header=("X-FallGuard-Key", "secret123"))
    send_to_n8n(url, payload, log=log_bad)                  # no header -> 403
    for _ in range(60):
        if log_ok and log_bad:
            break
        time.sleep(0.05)
    assert log_ok[0]["ok"] is True and log_ok[0]["status"] == 200, log_ok
    assert log_bad[0]["ok"] is False and log_bad[0]["status"] == 403, log_bad
    assert payload["episode"]["event_features"]["head_impact"]["risk"] == "high"
    srv.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("all tests passed")
