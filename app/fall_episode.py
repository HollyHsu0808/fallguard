"""
fall_episode.py — FallGuard post-fall episode tracking + n8n integration.

v9.3 (after three Le2i clips): fall_confidence label (confirmed /
unconfirmed / likely_false_alarm) derived from the analysis and the recovery
time, so n8n can suppress alerts for crouches and bends that trip the
detector; floor-level timings only start once the head has actually dropped
(perspective had put a standing person's hips "at floor level"); the feet-based
floor line uses the frames nearest the fall spot.

v9.2 (after the first side-view clip): the "settled lying" test now uses the
same body angle as the state machine (the confidence-filtered geometry had
dropped lying frames and reported "did not settle" on a clip with 3 s of
lying); body-joint confidence threshold 0.5 -> 0.3; the movement measure is
confidence-weighted over all joints instead of requiring five confident ones.

v9.1 (after the first run on real footage): head position falls back to the
shoulder midpoint when the face keypoints are not visible; the floor line
falls back to where the feet were when the person never settles lying;
frame pairs with implausible jumps (scene cuts, another person picked up by
the pose model) are skipped; a person too small in the frame gets "unknown"
instead of a noisy rating; sitting vs standing is decided by the knee angle
first, because a high camera foreshortens the hip height.

This module has NO Streamlit or YOLO dependency, so it can be imported and
unit-tested on its own (see test_fall_episode.py).

What it provides
----------------
FallEpisode        One fall, from the frame the fall is detected until the
                   person is confirmed upright (or the video/camera stops).
                   Accumulates:
                     - time_on_ground_sec  : fall -> recovery (or -> end of stream)
                     - posture_sec         : how that time splits into
                                             lying / partially_upright / not_visible
                     - motionless_sec      : seconds with (almost) no keypoint movement
                     - alerts              : the Level 1/2/3 escalations that fired
                     - event_features      : filled by FallAnalyser (see below)
classify_posture   Maps one frame's pose geometry to a posture bucket.
keypoint_motion    Normalised keypoint movement between two processed frames.
encode_snapshot    Frame -> small base64 JPEG for the webhook payload.
ClipBuffer         Rolling buffer of the last few seconds of frames (JPEG bytes)
                   so the seconds BEFORE a fall can go into the clip.
encode_clip        Frames -> short H.264 mp4 (base64) for Telegram sendVideo.
                   Uses the ffmpeg bundled with imageio-ffmpeg; falls back to
                   OpenCV (avc1, then mp4v) and finally to None (no clip).
build_payload      Episode -> JSON-serialisable dict (the n8n contract).
send_to_n8n        Fire-and-forget HTTP POST in a background thread.
make_sample_episode  A realistic fake episode for testing the n8n workflow.
FallAnalyser       Around one fall: pre-fall posture (standing / sitting /
                   lying -> fall-height category, cm if the patient's height
                   is known) and head-impact risk (high / medium / low /
                   unknown) from the head keypoints. Rule-based, no training.
"""

import base64
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta

import cv2
import numpy as np
import requests

SCHEMA_VERSION = "1.0"

POSTURES = ("lying", "partially_upright", "not_visible")

ALERT_LABELS = {
    1: "Fall Detected",
    2: "Person Still Down",
    3: "EMERGENCY: No Recovery",
}


def now_iso():
    """Local wall-clock time as ISO-8601 with UTC offset, e.g. 2026-09-12T14:03:22+10:00"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Per-frame helpers
# ---------------------------------------------------------------------------

def classify_posture(person_detected, body_angle, recovery_angle, lying_angle):
    """
    Bucket one processed frame into a posture while a fall episode is active.

    body_angle is the torso angle from vertical in degrees (0 = upright,
    90 = horizontal, as computed by analyze_pose in the app).

      not_visible        pose model found nobody in the frame
      lying              body_angle >= lying_angle   (torso near horizontal)
      partially_upright  everything else: sitting, kneeling, in the middle of
                         standing up, or upright but not yet confirmed by the
                         recovery streak (recovery needs several consecutive
                         upright frames, see the state machine in the app)

    Known limitation: a person on all fours has a horizontal torso and will be
    bucketed as "lying" — 2-D keypoints cannot separate the two reliably.
    """
    if not person_detected:
        return "not_visible"
    if body_angle >= lying_angle:
        return "lying"
    return "partially_upright"


def keypoint_motion(prev_kpts, curr_kpts, bbox, dt, min_weight=0.25):
    """
    Movement between two consecutive processed frames, expressed as a fraction
    of the person's bounding-box diagonal PER SECOND, so it is independent of
    image resolution, distance from the camera, and processing speed.

    prev_kpts, curr_kpts : (17, 3) arrays of x, y, confidence
    bbox                 : (x1, y1, x2, y2) of the current frame
    dt                   : seconds between the two frames

    The displacement of every keypoint is averaged with weight
    conf_prev x conf_curr, so uncertain joints (a lying person often has
    several) count little instead of dropping the whole frame. Returns None
    only when it cannot be computed: missing frame, zero-size box, dt <= 0, or
    total weight below min_weight (almost no joint is trusted at all).
    None means "unknown", and the caller must not count it as motionless.
    """
    if prev_kpts is None or curr_kpts is None or dt is None or dt <= 0:
        return None
    prev_kpts = np.asarray(prev_kpts, dtype=float)
    curr_kpts = np.asarray(curr_kpts, dtype=float)
    if prev_kpts.shape != curr_kpts.shape or prev_kpts.shape[1] < 3:
        return None
    w = np.clip(prev_kpts[:, 2], 0, 1) * np.clip(curr_kpts[:, 2], 0, 1)
    if w.sum() < min_weight:
        return None
    x1, y1, x2, y2 = [float(v) for v in bbox]
    diag = float(np.hypot(x2 - x1, y2 - y1))
    if diag <= 0:
        return None
    disp = np.linalg.norm(curr_kpts[:, :2] - prev_kpts[:, :2], axis=1)
    return float((disp * w).sum() / w.sum() / diag / dt)


def _jpeg_bytes(frame_bgr, max_width=640, quality=70):
    """Downscale to max_width (keeping even dimensions, which H.264 needs) and
    JPEG-encode. Returns bytes, or None when the frame is unusable."""
    if frame_bgr is None:
        return None
    h, w = frame_bgr.shape[:2]
    if w > max_width:
        scale = max_width / w
        w, h = max_width, int(h * scale)
    w, h = max(2, w - w % 2), max(2, h - h % 2)
    if (w, h) != tuple(frame_bgr.shape[1::-1]):
        frame_bgr = cv2.resize(frame_bgr, (w, h), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    return buf.tobytes() if ok else None


def encode_snapshot(frame_bgr, max_width=640, quality=70):
    """Downscale + JPEG-encode a frame and return it as a base64 string."""
    data = _jpeg_bytes(frame_bgr, max_width, quality)
    return base64.b64encode(data).decode("ascii") if data else None


# ---------------------------------------------------------------------------
# Fall clip: the seconds before and after the fall as a short mp4
# ---------------------------------------------------------------------------

class ClipBuffer:
    """
    Rolling buffer of the last `pre_sec` seconds of processed frames, stored as
    JPEG bytes (about 30-60 KB each at 640 px) so memory stays small. Push every
    processed frame; when a fall is called, snapshot() gives the pre-fall
    material for the clip.
    """

    def __init__(self, pre_sec=5.0, max_width=640, quality=75):
        self.pre_sec = float(pre_sec)
        self.max_width = int(max_width)
        self.quality = int(quality)
        self._frames = deque()          # (video_time, jpeg_bytes)

    def push(self, video_time, frame_bgr):
        """Store the frame; returns its JPEG bytes (reusable by the recorder)."""
        jpeg = _jpeg_bytes(frame_bgr, self.max_width, self.quality)
        if jpeg is None:
            return None
        t = float(video_time)
        self._frames.append((t, jpeg))
        while self._frames and self._frames[0][0] < t - self.pre_sec:
            self._frames.popleft()
        return jpeg

    def snapshot(self):
        return list(self._frames)

    def clear(self):
        self._frames.clear()


class ClipRecorder:
    """Collects the frames of one clip: the pre-fall frames handed over at the
    start, then every frame pushed until `post_sec` after the fall."""

    def __init__(self, pre_frames, fall_video_time, post_sec=3.0, max_frames=240):
        self.fall_video_time = float(fall_video_time)
        self.post_sec = float(post_sec)
        self.max_frames = int(max_frames)
        self.frames = [(float(t), j) for (t, j) in pre_frames if j]
        self.done = False

    def push(self, video_time, jpeg):
        if self.done or jpeg is None:
            return self.done
        t = float(video_time)
        if not self.frames or t > self.frames[-1][0]:
            self.frames.append((t, jpeg))
        if t - self.fall_video_time >= self.post_sec or len(self.frames) >= self.max_frames:
            self.done = True
        return self.done


def _ffmpeg_exe():
    """Path of an ffmpeg binary: the one bundled with imageio-ffmpeg
    (pip install imageio-ffmpeg) or one on PATH; None when neither exists."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _encode_ffmpeg(exe, frames, fps, crf):
    """JPEG frames -> H.264 mp4 bytes via ffmpeg image2pipe. Raises on failure."""
    fd, out = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        cmd = [exe, "-y", "-loglevel", "error",
               "-f", "image2pipe", "-framerate", "{:.3f}".format(fps), "-i", "-",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", out]
        proc = subprocess.run(cmd, input=b"".join(j for _, j in frames),
                              capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-400:])
        with open(out, "rb") as fh:
            data = fh.read()
        if len(data) < 100:
            raise RuntimeError("ffmpeg produced an empty file")
        return data
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _encode_cv2(frames, fps, size):
    """Fallback without ffmpeg: OpenCV VideoWriter, avc1 (H.264) if the build
    has it, else mp4v (plays on Telegram desktop, may not inline-play on phones)."""
    for fourcc in ("avc1", "mp4v"):
        fd, out = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            wr = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*fourcc), fps, size)
            if not wr.isOpened():
                continue
            for _, j in frames:
                img = cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                if img.shape[1::-1] != size:
                    img = cv2.resize(img, size)
                wr.write(img)
            wr.release()
            with open(out, "rb") as fh:
                data = fh.read()
            if len(data) > 100:
                return data, fourcc
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass
    return None, None


def encode_clip(frames, fps=None, crf=28):
    """
    frames : list of (video_time, jpeg_bytes), oldest first (ClipBuffer /
             ClipRecorder output). Needs at least 2 frames.
    fps    : playback rate; derived from the timestamps when None.
    Returns (mp4_base64, meta) or None when no encoder could produce a file.
    meta = {frames, fps, duration_sec, width, height, codec, encoder}.
    """
    frames = [(float(t), j) for (t, j) in (frames or []) if j]
    if len(frames) < 2:
        return None
    t0, t1 = frames[0][0], frames[-1][0]
    if fps is None:
        fps = (len(frames) - 1) / (t1 - t0) if t1 > t0 else 8.0
    fps = float(min(30.0, max(2.0, fps)))
    first = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR)
    if first is None:
        return None
    h, w = first.shape[:2]
    w, h = max(2, w - w % 2), max(2, h - h % 2)
    data, codec, encoder = None, None, None
    exe = _ffmpeg_exe()
    if exe:
        try:
            data, codec, encoder = _encode_ffmpeg(exe, frames, fps, crf), "h264", "ffmpeg"
        except Exception:
            data = None
    if data is None:
        data, fourcc = _encode_cv2(frames, fps, (w, h))
        if data is None:
            return None
        codec, encoder = fourcc, "opencv"
    meta = {"frames": len(frames), "fps": round(fps, 2),
            "duration_sec": round(len(frames) / fps, 2),
            "width": w, "height": h, "codec": codec, "encoder": encoder}
    return base64.b64encode(data).decode("ascii"), meta


# ---------------------------------------------------------------------------
# Episode tracker
# ---------------------------------------------------------------------------

class FallEpisode:
    """
    One fall episode. Create it on the frame the fall is detected, call
    update() on every processed frame while the person is down, add_alert()
    when Level 2/3 fires, and close() on recovery or end of stream.

    Time accounting: every processed frame carries the interval since the
    previous processed frame (dt). That interval is attributed to the posture
    observed in the current frame, and additionally to motionless_sec when the
    movement measure is below threshold. So:

        lying + partially_upright + not_visible  ==  time_on_ground_sec
        motionless_sec  <=  lying + partially_upright   (never counted when not visible)
    """

    def __init__(self, fall_video_time, trigger_signals, thresholds, source,
                 fall_wall_time=None):
        self.episode_id = "ep-{}-{}".format(
            datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4])
        self.fall_video_time = float(fall_video_time)
        self.fall_wall_time = fall_wall_time or now_iso()
        self.trigger_signals = dict(trigger_signals)
        self.thresholds = dict(thresholds)
        self.source = dict(source)

        self.status = "on_ground"           # on_ground | recovered | unresolved
        self.end_video_time = None
        self.end_wall_time = None
        self.last_video_time = self.fall_video_time

        self.posture_sec = {p: 0.0 for p in POSTURES}
        self.motionless_sec = 0.0
        self.motion_unknown_sec = 0.0       # visible, but movement could not be measured
        self.frames_processed = 0
        self.current_posture = None

        self.alerts = []
        self.add_alert(1, self.fall_video_time, self.fall_wall_time)

        self.event_features = {}            # reserved: head_impact_risk, fall_height, ...
        self.fall_snapshot_b64 = None
        self.latest_snapshot_b64 = None
        self.clip_b64 = None                # short mp4 around the fall (base64)
        self.clip_meta = None               # {duration_sec, fps, pre_fall_sec, ...}
        self._clip_rec = None               # ClipRecorder while collecting

    # --- per frame -------------------------------------------------------
    def update(self, video_time, posture, motion, motion_threshold):
        """
        video_time : timestamp of this processed frame (seconds)
        posture    : one of POSTURES (from classify_posture)
        motion     : keypoint_motion() result, or None when unknown
        """
        if self.status != "on_ground":
            return
        if posture not in POSTURES:
            raise ValueError("unknown posture: {}".format(posture))
        dt = max(0.0, float(video_time) - self.last_video_time)
        self.last_video_time = float(video_time)
        self.frames_processed += 1
        self.current_posture = posture
        self.posture_sec[posture] += dt
        if posture != "not_visible":
            if motion is None:
                self.motion_unknown_sec += dt
            elif motion < motion_threshold:
                self.motionless_sec += dt

    def add_alert(self, level, video_time, wall_time=None):
        self.alerts.append({
            "level": int(level),
            "label": ALERT_LABELS.get(int(level), "Level {}".format(level)),
            "time": wall_time or now_iso(),
            "video_time_sec": round(float(video_time), 2),
            "time_on_ground_sec": round(float(video_time) - self.fall_video_time, 2),
        })

    def close(self, status, video_time, wall_time=None):
        """status: 'recovered' (person confirmed upright) or 'unresolved'
        (video ended / camera stopped while the person was still down)."""
        if status not in ("recovered", "unresolved"):
            raise ValueError("status must be recovered or unresolved")
        self.status = status
        self.end_video_time = float(video_time)
        self.end_wall_time = wall_time or now_iso()
        self.last_video_time = max(self.last_video_time, float(video_time))
        self._update_fall_confidence()

    # --- fall clip -----------------------------------------------------------
    def start_clip(self, pre_frames, post_sec=3.0):
        """Begin collecting the clip: `pre_frames` from a ClipBuffer.snapshot()
        taken at the fall, then frames via record_clip_frame() until post_sec
        after the fall. Encoded once; every later event carries the same clip."""
        if self.clip_b64 is None:
            self._clip_rec = ClipRecorder(pre_frames, self.fall_video_time, post_sec)

    def record_clip_frame(self, video_time, jpeg):
        """Feed one processed frame (JPEG bytes, e.g. the return value of
        ClipBuffer.push). Finalises the clip automatically after post_sec."""
        if self._clip_rec is None:
            return
        if self._clip_rec.push(video_time, jpeg):
            self.finalize_clip()

    def finalize_clip(self):
        """Encode whatever has been collected so far (called early by the app
        when fall_analysed is about to be sent). Safe to call repeatedly."""
        rec, self._clip_rec = self._clip_rec, None
        if rec is None or self.clip_b64 is not None:
            return
        res = encode_clip(rec.frames)
        if res is None:
            return
        b64, meta = res
        pre = [t for t, _ in rec.frames if t < rec.fall_video_time]
        post = [t for t, _ in rec.frames if t >= rec.fall_video_time]
        meta["pre_fall_sec"] = round(rec.fall_video_time - pre[0], 2) if pre else 0.0
        meta["post_fall_sec"] = round(post[-1] - rec.fall_video_time, 2) if post else 0.0
        self.clip_b64, self.clip_meta = b64, meta

    def set_analysis(self, result):
        """Store a FallAnalyser result and derive how sure we are that a fall
        to the floor actually happened (see _update_fall_confidence)."""
        self.event_features = dict(result)
        self._update_fall_confidence()

    def _update_fall_confidence(self, p=None):
        """
        confirmed           the body settled in a lying posture, or the head
                            reached floor level
        unconfirmed         neither seen (yet): still on the ground, or a
                            posture / camera angle the rules cannot read
        likely_false_alarm  neither seen AND the person was upright again
                            within false_alarm_max_ground_sec: a crouch, bend
                            or sit that tripped the detector / pose rule
        """
        p = p or ANALYSIS_PARAMS
        ef = self.event_features
        if not ef or ef.get("analysis_status") != "done":
            return
        hi = ef.get("head_impact", {})
        if hi.get("settled_lying") or hi.get("head_reached_floor_level"):
            label = "confirmed"
        elif self.status == "recovered" and self.time_on_ground_sec < p["false_alarm_max_ground_sec"]:
            label = "likely_false_alarm"
        else:
            label = "unconfirmed"
        ef["fall_confidence"] = label

    # --- derived ---------------------------------------------------------
    @property
    def max_alert_level(self):
        return max((a["level"] for a in self.alerts), default=1)

    @property
    def time_on_ground_sec(self):
        end = self.end_video_time if self.end_video_time is not None else self.last_video_time
        return max(0.0, end - self.fall_video_time)

    @property
    def time_lying_sec(self):
        return self.posture_sec["lying"]

    def to_dict(self):
        return {
            "episode_id": self.episode_id,
            "source": self.source,
            "status": self.status,
            "fall_time": self.fall_wall_time,
            "fall_video_time_sec": round(self.fall_video_time, 2),
            "end_time": self.end_wall_time,
            "end_video_time_sec": (round(self.end_video_time, 2)
                                   if self.end_video_time is not None else None),
            "time_on_ground_sec": round(self.time_on_ground_sec, 2),
            "posture_breakdown_sec": {k: round(v, 2) for k, v in self.posture_sec.items()},
            "time_lying_sec": round(self.time_lying_sec, 2),
            "time_motionless_sec": round(self.motionless_sec, 2),
            "time_motion_unknown_sec": round(self.motion_unknown_sec, 2),
            "current_posture": self.current_posture,
            "max_alert_level": self.max_alert_level,
            "alerts": list(self.alerts),
            "trigger_signals": self.trigger_signals,
            "thresholds": self.thresholds,
            "event_features": self.event_features,
            "frames_processed": self.frames_processed,
        }


# ---------------------------------------------------------------------------
# Fall analysis: pre-fall posture (fall height) + head-impact risk
# All rule-based on the 17 COCO keypoints of YOLOv8-Pose. No training.
# ---------------------------------------------------------------------------

# COCO keypoint indices
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SH, R_SH, L_HIP, R_HIP = 5, 6, 11, 12
L_KNEE, R_KNEE, L_ANK, R_ANK = 13, 14, 15, 16
HEAD_IDX = [NOSE, L_EYE, R_EYE, L_EAR, R_EAR]

# Shoulder-to-hip length as a fraction of standing height, from the
# Drillis & Contini (1966) body-segment proportions (shoulder 0.818 H,
# hip 0.530 H). Used only to turn "torso lengths" into centimetres when the
# patient's height is known; the pixel scale cancels out.
TORSO_FRACTION_OF_HEIGHT = 0.288

ANALYSIS_PARAMS = {
    # pre-fall posture is judged on frames this many seconds BEFORE the
    # trigger, excluding the last second (which contains the fall itself)
    "prefall_window_sec": (3.0, 1.0),
    "prefall_min_frames": 3,
    # torso angle from vertical (deg): below -> upright torso
    "upright_torso_deg": 30.0,
    # hip-knee-ankle angle (deg): at/above -> straight leg
    "straight_knee_deg": 150.0,
    # hip height above ankle, in torso lengths (standing ~1.7, chair ~0.8)
    "standing_hip_height": 1.25,
    "sitting_hip_height": 1.10,
    # head-impact analysis covers frames from this long before the trigger…
    "transition_lead_sec": 1.5,
    # …until the person has lain still for this many frames, or this timeout
    "settle_frames": 3,
    "transition_timeout_sec": 2.5,
    # head is "at floor level" when within this many torso lengths of the
    # centre line of the settled body
    "floor_margin_torso": 0.5,
    # peak downward head speed, torso lengths per second
    "head_speed_high": 3.0,
    "head_speed_medium": 1.5,
    "head_visible_min_fraction": 0.5,
    # feet more than this many cm above the settled floor line -> elevated fall
    "elevated_fall_cm": 100.0,
    "elevation_noise_cm": 30.0,
    # keypoint confidence: body joints, and the (harder) nose/eye/ear points
    "kpt_conf": 0.3,
    "head_conf": 0.3,
    # a head or hip jump larger than this (torso lengths per processed frame)
    # is a scene cut or a switch to another person, not motion: skip the pair
    "max_step_torso": 1.5,
    # below this shoulder-to-hip length in pixels the keypoint noise is of the
    # same order as the signal: head-impact risk is reported as unknown
    "min_torso_px": 20.0,
    # the head must have dropped this far (torso lengths) below its pre-fall
    # height before "reached floor level" timings are counted — otherwise a
    # person who walked to a different depth before falling reads as
    # "hips on the floor" while still standing (perspective)
    "descent_start_drop_torso": 0.5,
    # recovered within this many seconds, never lying, head never at floor
    # level -> the trigger was most likely a crouch / bend / sit, not a fall
    "false_alarm_max_ground_sec": 3.0,
}


def _pt(kpts, idx, conf=None):
    """Mean (x, y) of the confident keypoints among `idx`, or None."""
    if kpts is None:
        return None
    if conf is None:
        conf = ANALYSIS_PARAMS["kpt_conf"]
    k = np.asarray(kpts, dtype=float)
    sel = k[idx]
    sel = sel[sel[:, 2] > conf]
    if len(sel) == 0:
        return None
    return sel[:, :2].mean(axis=0)


def head_point(kpts, conf=ANALYSIS_PARAMS["head_conf"]):
    return _pt(kpts, HEAD_IDX, conf)


def torso_length_px(kpts):
    s, h = _pt(kpts, [L_SH, R_SH]), _pt(kpts, [L_HIP, R_HIP])
    if s is None or h is None:
        return None
    d = float(np.linalg.norm(s - h))
    return d if d > 0 else None


def torso_angle_deg(kpts):
    """Same formula as analyze_pose() in the app: 0 = upright, 90 = horizontal."""
    s, h = _pt(kpts, [L_SH, R_SH]), _pt(kpts, [L_HIP, R_HIP])
    if s is None or h is None:
        return None
    dx, dy = s[0] - h[0], s[1] - h[1]
    return float(abs(np.degrees(np.arctan2(dx, -dy))))


def knee_angle_deg(kpts, conf=None):
    """Hip-knee-ankle angle of the straighter leg (180 = straight), or None."""
    if kpts is None:
        return None
    if conf is None:
        conf = ANALYSIS_PARAMS["kpt_conf"]
    k = np.asarray(kpts, dtype=float)
    angles = []
    for hip, knee, ank in ((L_HIP, L_KNEE, L_ANK), (R_HIP, R_KNEE, R_ANK)):
        if all(k[i, 2] > conf for i in (hip, knee, ank)):
            v1, v2 = k[hip, :2] - k[knee, :2], k[ank, :2] - k[knee, :2]
            n = np.linalg.norm(v1) * np.linalg.norm(v2)
            if n > 0:
                angles.append(float(np.degrees(np.arccos(np.clip(v1 @ v2 / n, -1, 1)))))
    return max(angles) if angles else None


def frame_geometry(kpts):
    """Per-frame geometry used by both analyses. None if no torso."""
    torso = torso_length_px(kpts)
    if torso is None:
        return None
    k = np.asarray(kpts, dtype=float)
    vis = k[k[:, 2] > ANALYSIS_PARAMS["kpt_conf"]]
    hip, ank = _pt(kpts, [L_HIP, R_HIP]), _pt(kpts, [L_ANK, R_ANK])
    shoulders = _pt(kpts, [L_SH, R_SH])
    head = head_point(kpts)
    return {
        "torso_px": torso,
        "torso_angle": torso_angle_deg(kpts),
        "knee_angle": knee_angle_deg(kpts),
        # image y grows downward, so ankle_y - hip_y is the hip's height above the feet
        "hip_height_torso": (float(ank[1] - hip[1]) / torso) if (hip is not None and ank is not None) else None,
        "span_torso": float(vis[:, 1].max() - vis[:, 1].min()) / torso if len(vis) else None,
        "head": head, "shoulders": shoulders, "hip": hip, "ankle": ank,
        # head position used by the impact analysis: real head keypoints when
        # the nose/eyes/ears are confident, otherwise the shoulder midpoint
        # (about one head length away; it moves with the head in a fall)
        "head_pos": head if head is not None else shoulders,
        "head_source": "keypoints" if head is not None else ("shoulders" if shoulders is not None else None),
        "centre_y": float(vis[:, 1].mean()) if len(vis) else None,
    }


def classify_prefall_posture(geoms, lying_angle, p=ANALYSIS_PARAMS):
    """
    geoms: frame_geometry() dicts from the pre-fall window (visible frames only).
    Returns (posture, medians). posture in
      standing | sitting | lying | unclear | unknown
    """
    if len(geoms) < p["prefall_min_frames"]:
        return "unknown", {}

    def med(key):
        vals = [g[key] for g in geoms if g.get(key) is not None]
        return float(np.median(vals)) if vals else None

    m = {"torso_angle": med("torso_angle"), "knee_angle": med("knee_angle"),
         "hip_height_torso": med("hip_height_torso"), "span_torso": med("span_torso")}
    ang, knee, hip, span = m["torso_angle"], m["knee_angle"], m["hip_height_torso"], m["span_torso"]
    if ang is None:
        return "unknown", m
    if ang >= lying_angle or (span is not None and span < 1.2):
        return "lying", m
    upright = ang < p["upright_torso_deg"]
    # Knee angle first: bent knees are what distinguishes sitting, and the
    # angle survives a high camera better than the hip height, which a
    # downward-looking camera foreshortens.
    if knee is not None:
        if knee >= p["straight_knee_deg"]:
            return ("standing" if upright else "unclear"), m
        if knee < 130:
            return ("sitting" if ang < 45 else "unclear"), m
    # knees unavailable or in between: fall back to the hip height
    if hip is not None:
        if upright and hip >= p["standing_hip_height"]:
            return "standing", m
        if ang < 45 and hip < p["sitting_hip_height"]:
            return "sitting", m
    return "unclear", m


class FallAnalyser:
    """
    Collects the frames around one fall and derives:
      prefall     posture before the fall (standing / sitting / lying / …),
                  hip and head height in torso lengths, and in cm when the
                  patient's height is given
      head_impact risk (high / medium / low / unknown) from how fast the head
                  came down, whether it reached floor level, and whether it
                  got there before the hips
    Frames are (t, kpts, angle) tuples; kpts is (17, 3) or None.
    Create it at the trigger frame with the recent-frame buffer, feed every
    later processed frame with add_frame(), read result() once `done`.
    """

    def __init__(self, recent_frames, t_trigger, lying_angle,
                 patient_height_cm=None, p=ANALYSIS_PARAMS):
        self.p = dict(p)
        self.t_trigger = float(t_trigger)
        self.lying_angle = float(lying_angle)
        self.height_cm = float(patient_height_cm) if patient_height_cm else None
        w_from, w_to = self.p["prefall_window_sec"]
        self.prefall = [f for f in recent_frames
                        if self.t_trigger - w_from <= f[0] <= self.t_trigger - w_to]
        self.transition = [f for f in recent_frames
                           if f[0] > self.t_trigger - self.p["transition_lead_sec"]]
        self.done = False
        self._still_lying = 0

    def add_frame(self, t, kpts, angle):
        if self.done:
            return
        self.transition.append((float(t), kpts, angle))
        lying = kpts is not None and angle is not None and angle >= self.lying_angle
        self._still_lying = self._still_lying + 1 if lying else 0
        if self._still_lying >= self.p["settle_frames"] or \
                t - self.t_trigger >= self.p["transition_timeout_sec"]:
            self.done = True

    def finish(self):
        self.done = True

    # -- helpers ---------------------------------------------------------
    def _cm(self, torso_units):
        if torso_units is None or self.height_cm is None:
            return None
        return round(torso_units * TORSO_FRACTION_OF_HEIGHT * self.height_cm, 1)

    def result(self):
        p = self.p
        # ---- pre-fall posture -------------------------------------------
        pre_geoms = [frame_geometry(k) for (_, k, _) in self.prefall if k is not None]
        pre_geoms = [g for g in pre_geoms if g is not None]
        posture, med = classify_prefall_posture(pre_geoms, self.lying_angle, p)
        head_height_t = None
        if pre_geoms:
            hh = [(g["ankle"][1] - g["head"][1]) / g["torso_px"]
                  for g in pre_geoms if g["head"] is not None and g["ankle"] is not None]
            head_height_t = float(np.median(hh)) if hh else None
        torso_px_ref = float(np.median([g["torso_px"] for g in pre_geoms])) if pre_geoms else None

        # ---- transition / head impact ------------------------------------
        trans = [(t, frame_geometry(k), k, a) for (t, k, a) in self.transition]
        # "settled": the last run of at least settle_frames CONSECUTIVE frames
        # with the torso at or beyond the lying angle
        settled, run = [], []
        for (t, g, k, a) in trans:
            if k is not None and a is not None and a >= self.lying_angle:
                run.append((t, k))
                if len(run) >= p["settle_frames"]:
                    settled = list(run)
            else:
                run = []
        if torso_px_ref is None:
            tp = [g["torso_px"] for (_, g, _, _) in trans if g is not None]
            torso_px_ref = float(np.median(tp)) if tp else None

        # Floor line at the person's position: the centre line of the settled
        # lying body when there is one; otherwise where the feet were (the
        # feet stand on the floor before the fall).
        floor_source = None
        floor_y = None
        if settled:
            ys = []
            for _, k in settled:
                kk = np.asarray(k, dtype=float)
                vis = kk[kk[:, 2] > p["kpt_conf"]]
                ys.append(float(vis[:, 1].mean()) if len(vis) else float(kk[:, 1].mean()))
            floor_y, floor_source = float(np.mean(ys)), "settled_body"
        else:
            # feet nearest to where the fall happened: the transition frames
            # (from 1.5 s before the trigger), else the last pre-fall frames
            feet = [g["ankle"][1] for (_, g, _, _) in trans if g is not None and g["ankle"] is not None]
            if len(feet) < 2:
                feet = [g["ankle"][1] for g in pre_geoms[-4:] if g["ankle"] is not None]
            if feet:
                floor_y, floor_source = float(np.median(feet)), "feet"

        n_frames = len(trans)
        kpt_frames = sum(1 for (_, g, _, _) in trans if g is not None and g["head"] is not None)
        pos_frames = sum(1 for (_, g, _, _) in trans if g is not None and g["head_pos"] is not None)
        head_fraction = (pos_frames / n_frames) if n_frames else 0.0
        head_source = "keypoints" if kpt_frames >= 0.5 * max(n_frames, 1) else \
                      ("shoulders" if pos_frames else "none")

        # Peak DOWNWARD speed of the head between consecutive frames. Pairs
        # where the head or hip jumps more than max_step_torso in one frame
        # are scene cuts / another person and are skipped.
        peak, prev, skipped, valid_pairs = 0.0, None, 0, 0
        max_step = p["max_step_torso"] * torso_px_ref if torso_px_ref else None
        for (t, g, _, _) in trans:
            if g is None or g["head_pos"] is None:
                prev = None
                continue
            if prev is not None and max_step:
                dt = t - prev[0]
                jump_head = float(np.linalg.norm(g["head_pos"] - prev[1]))
                jump_hip = (float(np.linalg.norm(g["hip"] - prev[2]))
                            if (g["hip"] is not None and prev[2] is not None) else 0.0)
                size_ratio = g["torso_px"] / prev[3]
                if jump_head > max_step or jump_hip > max_step or not (0.5 <= size_ratio <= 2.0):
                    skipped += 1
                elif dt > 0:
                    v = float((g["head_pos"][1] - prev[1][1]) / dt / torso_px_ref)   # + = downward
                    peak = max(peak, v)
                    valid_pairs += 1
            prev = (t, g["head_pos"], g["hip"], g["torso_px"])

        # Descent start: first frame where the head is at least half a torso
        # length below its pre-fall height. Before that the person is still up,
        # and perspective can put a standing person's hips "at floor level" of
        # a fall spot that is further from the camera.
        pre_head_y = [g["head_pos"][1] for g in pre_geoms if g["head_pos"] is not None]
        pre_head_y = float(np.median(pre_head_y)) if pre_head_y else None
        descent_t = None
        if pre_head_y is not None and torso_px_ref:
            for (t, g, _, _) in trans:
                if g is not None and g["head_pos"] is not None and \
                        g["head_pos"][1] >= pre_head_y + p["descent_start_drop_torso"] * torso_px_ref:
                    descent_t = t
                    break
        if descent_t is None and trans:
            descent_t = trans[0][0]

        reached, t_head, t_hip = None, None, None
        if floor_y is not None and torso_px_ref:
            margin = p["floor_margin_torso"] * torso_px_ref
            reached = False
            for (t, g, _, _) in trans:
                if g is None or t < descent_t:
                    continue
                if g["head_pos"] is not None and t_head is None and g["head_pos"][1] >= floor_y - margin:
                    t_head, reached = t, True
                if g["hip"] is not None and t_hip is None and g["hip"][1] >= floor_y - margin:
                    t_hip = t
        head_before_hip = (round(t_hip - t_head, 2) if (t_head is not None and t_hip is not None) else None)

        reasons = []
        if torso_px_ref is not None and torso_px_ref < p["min_torso_px"]:
            risk = "unknown"
            reasons.append("person too small in the frame for a reliable estimate "
                           "(shoulder-to-hip length about {:.0f} px)".format(torso_px_ref))
        elif head_fraction < p["head_visible_min_fraction"] or head_source == "none":
            risk = "unknown"; reasons.append("head position available in fewer than half of the frames")
        elif floor_y is None:
            risk = "unknown"; reasons.append("floor level could not be established (no lying posture and no visible feet)")
        elif valid_pairs == 0:
            risk = "unknown"; reasons.append("no usable consecutive frames to measure the head's speed")
        elif not reached and floor_source == "settled_body":
            risk = "low"; reasons.append("head stayed above floor level")
        elif not reached:
            risk = "unknown"; reasons.append("person did not settle on the floor and the head did not reach the estimated floor level")
        elif peak >= p["head_speed_high"]:
            risk = "high"; reasons.append("head came down fast ({:.1f} torso lengths/s)".format(peak))
        elif peak >= p["head_speed_medium"] and head_before_hip is not None and head_before_hip >= 0:
            risk = "high"; reasons.append("head reached floor level no later than the hips ({:.1f} torso lengths/s)".format(peak))
        elif peak >= p["head_speed_medium"]:
            risk = "medium"; reasons.append("head reached floor level at moderate speed ({:.1f} torso lengths/s)".format(peak))
        elif floor_source == "settled_body":
            risk = "low"; reasons.append("head lowered slowly to floor level")
        else:
            risk = "unknown"; reasons.append("head reached the estimated floor level slowly, but the person did not settle on the floor")
        if risk != "unknown":
            if head_before_hip is not None and head_before_hip > 0:
                reasons.append("head reached floor level {:.2f} s before the hips".format(head_before_hip))
            if head_source == "shoulders":
                reasons.append("head position estimated from the shoulders (face keypoints not visible)")
            if floor_source == "feet":
                reasons.append("floor level taken from where the feet were before the fall")
        if skipped:
            reasons.append("{} frame pair(s) skipped as scene cuts / person switches".format(skipped))

        # ---- fall height category ------------------------------------------
        feet_elev_cm = None
        if floor_y is not None and torso_px_ref and self.height_cm and pre_geoms:
            ank = [g["ankle"][1] for g in pre_geoms if g["ankle"] is not None]
            if ank:
                feet_elev_cm = self._cm((floor_y - float(np.median(ank))) / torso_px_ref)
                if feet_elev_cm is not None and feet_elev_cm < p["elevation_noise_cm"]:
                    feet_elev_cm = 0.0
        if feet_elev_cm is not None and feet_elev_cm >= p["elevated_fall_cm"]:
            height_cat = "elevated_over_1m"
        else:
            height_cat = {"standing": "standing_height", "sitting": "seated_height",
                          "lying": "bed_or_sofa_height"}.get(posture, "unknown")

        return {
            "analysis_status": "done",
            "prefall": {
                "posture": posture,
                "frames_used": len(pre_geoms),
                "torso_angle_deg": round(med["torso_angle"], 1) if med.get("torso_angle") is not None else None,
                "knee_angle_deg": round(med["knee_angle"], 1) if med.get("knee_angle") is not None else None,
                "hip_height_torso_units": round(med["hip_height_torso"], 2) if med.get("hip_height_torso") is not None else None,
                "hip_height_cm": self._cm(med.get("hip_height_torso")),
                "head_height_cm": self._cm(head_height_t),
                "feet_elevation_cm": feet_elev_cm,
                "fall_height_category": height_cat,
            },
            "head_impact": {
                "risk": risk,
                "reasons": reasons,
                "peak_descent_speed_torso_per_sec": round(peak, 2),
                "peak_descent_speed_cm_per_sec": self._cm(peak),
                "head_reached_floor_level": reached,
                "head_before_hip_sec": head_before_hip,
                "head_visible_fraction": round(head_fraction, 2),
                "head_source": head_source,
                "floor_source": floor_source,
                "descent_start_video_time_sec": round(descent_t, 2) if descent_t is not None else None,
                "settled_lying": bool(settled),
                "frames_skipped_as_cuts": skipped,
                "frames_analysed": n_frames,
            },
            "image_quality": {"torso_px": round(torso_px_ref, 1) if torso_px_ref is not None else None,
                              "min_torso_px": p["min_torso_px"]},
            "scale": {"patient_height_cm": self.height_cm,
                      "torso_fraction_of_height": TORSO_FRACTION_OF_HEIGHT},
        }


# ---------------------------------------------------------------------------
# n8n contract
# ---------------------------------------------------------------------------

def build_payload(event_type, episode, include_snapshots=False, extra=None,
                  include_clip=False):
    """
    event_type: fall_detected | escalation | episode_closed | test
    Everything n8n needs is under `episode`; snapshots are optional because a
    base64 JPEG (~40-80 KB at 640 px / q70) is the bulk of the payload.
    include_clip adds payload["clip"] = {mp4_base64, duration_sec, fps,
    pre_fall_sec, post_fall_sec, codec, ...} when the episode has a clip
    (about 0.2-1 MB at 640 px / 8 s); the key is absent when there is none, so
    n8n can fall back to the snapshot or to plain text.
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "system": "FallGuard",
        "event_type": event_type,
        "sent_at": now_iso(),
        "source": episode.source,
        "episode": episode.to_dict(),
    }
    if extra:
        payload.update(extra)
    if include_snapshots:
        payload["snapshots"] = {
            "fall_frame_jpeg_base64": episode.fall_snapshot_b64,
            "latest_frame_jpeg_base64": episode.latest_snapshot_b64,
        }
    if include_clip and getattr(episode, "clip_b64", None):
        payload["clip"] = dict(episode.clip_meta or {})
        payload["clip"]["mp4_base64"] = episode.clip_b64
    return payload


_SEND_QUEUE = None
_SEND_LOCK = threading.Lock()


def _sender_worker(q):
    while True:
        url, payload, headers, log, timeout = q.get()
        entry = {"time": now_iso(), "event_type": payload.get("event_type"),
                 "episode_id": payload.get("episode", {}).get("episode_id")}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            entry["status"] = r.status_code
            entry["ok"] = 200 <= r.status_code < 300
        except Exception as e:  # network errors, timeouts, bad URL
            entry["status"] = "error: {}".format(e)
            entry["ok"] = False
        if log is not None:
            log.append(entry)
        q.task_done()


def send_to_n8n(url, payload, log=None, timeout=10, auth_header=None):
    """
    POST the payload as JSON without blocking the detection loop.
    Payloads go through ONE background worker in the order they were handed
    over, so n8n receives fall_detected before fall_analysed before
    episode_closed even when they are sent milliseconds apart.
    auth_header: (name, value) added to the request, e.g.
    ("X-FallGuard-Key", "<secret>") — must match the Header Auth credential
    on the n8n Webhook node. None sends no extra header.
    Delivery results are appended to `log` (any list-like with .append —
    a deque works) so the UI can show them. Nothing here touches Streamlit:
    Streamlit APIs must not be called from a background thread.
    """
    global _SEND_QUEUE
    if not url:
        return False
    headers = {}
    if auth_header and auth_header[0] and auth_header[1]:
        headers[auth_header[0]] = auth_header[1]
    with _SEND_LOCK:
        if _SEND_QUEUE is None:
            _SEND_QUEUE = queue.Queue()
            threading.Thread(target=_sender_worker, args=(_SEND_QUEUE,), daemon=True).start()
    _SEND_QUEUE.put((url, payload, headers, log, timeout))
    return True


def make_sample_episode(source_name="sample_video.mp4"):
    """A realistic fake episode so the n8n workflow can be built and tested
    before any video is run: fell at 12.4 s, lay for ~26 s, sat up, stood
    at 34.6 s, Level 2 and Level 3 both fired."""
    thresholds = {"level2_sec": 10, "level3_sec": 30, "recovery_angle_deg": 20,
                  "lying_angle_deg": 60, "motion_threshold_per_sec": 0.15,
                  "detection_conf": 0.25}
    signals = {"detector": True, "pose_rule": False, "temporal": True,
               "pose_score_at_fall": 0.5}
    fall_wall = (datetime.now().astimezone() - timedelta(seconds=40))
    ep = FallEpisode(12.4, signals, thresholds,
                     {"mode": "upload", "name": source_name},
                     fall_wall_time=fall_wall.isoformat(timespec="seconds"))
    # 0.125 s per processed frame (every 3rd frame at 24 fps)
    t = 12.4
    for _ in range(208):            # 26 s lying, mostly motionless
        t += 0.125
        ep.update(t, "lying", 0.05, thresholds["motion_threshold_per_sec"])
    for _ in range(16):             # 2 s lying but moving
        t += 0.125
        ep.update(t, "lying", 0.40, thresholds["motion_threshold_per_sec"])
    for _ in range(8):              # 1 s lost from view
        t += 0.125
        ep.update(t, "not_visible", None, thresholds["motion_threshold_per_sec"])
    for _ in range(40):             # 5 s sitting / getting up
        t += 0.125
        ep.update(t, "partially_upright", 0.60, thresholds["motion_threshold_per_sec"])
    ep.add_alert(2, 22.5, (fall_wall + timedelta(seconds=10.1)).isoformat(timespec="seconds"))
    ep.add_alert(3, 42.5, (fall_wall + timedelta(seconds=30.1)).isoformat(timespec="seconds"))
    ep.close("recovered", t, (fall_wall + timedelta(seconds=t - 12.4)).isoformat(timespec="seconds"))
    ep.event_features = {
        "analysis_status": "done",
        "prefall": {"posture": "standing", "frames_used": 14, "torso_angle_deg": 6.2,
                    "knee_angle_deg": 171.0, "hip_height_torso_units": 1.68,
                    "hip_height_cm": 79.8, "head_height_cm": 148.5, "feet_elevation_cm": 0.0,
                    "fall_height_category": "standing_height"},
        "head_impact": {"risk": "high",
                        "reasons": ["head came down fast (3.6 torso lengths/s)",
                                    "head reached floor level 0.12 s before the hips"],
                        "peak_descent_speed_torso_per_sec": 3.6,
                        "peak_descent_speed_cm_per_sec": 171.1,
                        "head_reached_floor_level": True, "head_before_hip_sec": 0.12,
                        "head_visible_fraction": 0.9, "head_source": "keypoints",
                        "floor_source": "settled_body", "settled_lying": True,
                        "frames_skipped_as_cuts": 0, "frames_analysed": 18},
        "image_quality": {"torso_px": 62.0, "min_torso_px": ANALYSIS_PARAMS["min_torso_px"]},
        "scale": {"patient_height_cm": 165.0, "torso_fraction_of_height": TORSO_FRACTION_OF_HEIGHT},
        "fall_confidence": "confirmed",
    }
    return ep


if __name__ == "__main__":
    print(json.dumps(build_payload("episode_closed", make_sample_episode()), indent=2))


def make_sample_clip(seconds=3.0, fps=8, width=640, height=360):
    """A synthetic clip (a stick figure that falls over) so the n8n video path
    can be exercised from the Settings page without running a video.
    Returns (mp4_base64, meta) or None when no encoder is available."""
    import math
    n = int(seconds * fps)
    frames = []
    for i in range(n):
        f = np.full((height, width, 3), 40, dtype=np.uint8)
        cv2.line(f, (0, height - 40), (width, height - 40), (90, 90, 90), 2)
        p = min(1.0, max(0.0, (i - n * 0.4) / (n * 0.3)))     # 0 = standing, 1 = lying
        cx, top, length = width // 2, height - 40, 200
        ang = p * math.pi / 2
        x2, y2 = int(cx + length * math.sin(ang)), int(top - length * math.cos(ang))
        cv2.line(f, (cx, top), (x2, y2), (0, 200, 255), 8)
        cv2.circle(f, (x2, y2), 18, (0, 200, 255), -1)
        cv2.putText(f, "FallGuard test clip  {:.1f}s".format(i / fps), (20, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        frames.append((i / fps, _jpeg_bytes(f, width, 80)))
    res = encode_clip(frames, fps=fps)
    if res is None:
        return None
    b64, meta = res
    meta["pre_fall_sec"] = round(seconds * 0.4, 2)
    meta["post_fall_sec"] = round(seconds * 0.6, 2)
    return b64, meta
