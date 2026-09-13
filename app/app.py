"""
FallGuard: Real-Time Elderly Fall Detection and Alert System
Streamlit dashboard - dark tech theme with post-fall monitoring

v8 additions
  * Fall EPISODE tracking (fall_episode.py): for every fall, the time on the
    ground, how that time splits into lying / partially upright / not visible,
    and how much of it the person was motionless.
  * n8n webhook events (fall_detected, escalation, episode_closed) carrying the
    full episode record, so an n8n workflow can grade the incident and have an
    LLM write the caregiver report. See n8n/README.md for the contract.
  * Episodes table + JSON download in Event History; test-payload button and
    delivery log in Settings.
v9 additions
  * Fall analysis (FallAnalyser): pre-fall posture -> fall-height category
    (standing / seated / bed height; cm if the patient's height is given) and
    head-impact risk from the head keypoints. Sent as `fall_analysed` ~2 s
    after the fall and in every later event under episode.event_features.
  * Header Auth on the webhook (X-FallGuard-Key), for a public n8n Cloud URL.
  * In-app Telegram removed: every notification now goes through n8n.

Run with:  streamlit run app_v9.py      (fall_episode.py must sit next to it)
Optional .env keys: N8N_WEBHOOK_URL, N8N_WEBHOOK_KEY
"""

import os
import json
from collections import deque

import streamlit as st
import pandas as pd
import requests
import cv2
import numpy as np
import tempfile
import time
from datetime import datetime
from ultralytics import YOLO

from fall_episode import (FallEpisode, FallAnalyser, classify_posture,
                          keypoint_motion, encode_snapshot, build_payload,
                          send_to_n8n, make_sample_episode,
                          ClipBuffer, make_sample_clip, now_iso)

# v10: fall clip sent to n8n (Telegram video). Seconds kept before the fall
# and collected after it; the clip is encoded once, when fall_analysed is
# sent (~2.5 s after the fall) or when CLIP_POST_SEC has passed.
CLIP_PRE_SEC = 5.0
CLIP_POST_SEC = 3.0

# .env is optional: without python-dotenv the sidebar fields simply start empty
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # minimal fallback so .env still works without python-dotenv
    _env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env):
        for _line in open(_env, encoding="utf-8"):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# 
# Page configuration

st.set_page_config(page_title="FallGuard Dashboard", layout="wide",
                   initial_sidebar_state="expanded")

# Custom dark theme styling
st.markdown("""
<style>
    .stApp { background-color: #0A0A0F; color: #E8E8EC; }
    section[data-testid="stSidebar"] { background-color: #0F0F16; border-right: 1px solid #1F1F28; }
    h1, h2, h3 { color: #FFFFFF !important; font-weight: 700; }
    .card {
        background: #13131C; border: 1px solid #1F1F2E;
        border-radius: 12px; padding: 18px 20px; margin-bottom: 14px;
    }
    .card-title {
        font-size: 13px; font-weight: 600; color: #7A7A8C;
        text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px;
    }
    .status-fall {
        background: linear-gradient(135deg, #2A1015, #3A1418);
        border: 1px solid #FF5A3C; border-radius: 10px;
        padding: 16px; text-align: center;
    }
    .status-fall .label { color: #FF6B4A; font-size: 20px; font-weight: 700; }
    .status-emergency {
        background: linear-gradient(135deg, #3A0A0A, #500D0D);
        border: 2px solid #FF2020; border-radius: 10px;
        padding: 16px; text-align: center;
        animation: pulse 1s infinite;
    }
    .status-emergency .label { color: #FF3030; font-size: 20px; font-weight: 700; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.6; } }
    .status-warning {
        background: linear-gradient(135deg, #2A1F08, #3A2A0C);
        border: 1px solid #FFA53C; border-radius: 10px;
        padding: 16px; text-align: center;
    }
    .status-warning .label { color: #FFB84A; font-size: 20px; font-weight: 700; }
    .status-normal {
        background: linear-gradient(135deg, #0F1A14, #12211A);
        border: 1px solid #2E7D5A; border-radius: 10px;
        padding: 16px; text-align: center;
    }
    .status-normal .label { color: #3CCB7F; font-size: 20px; font-weight: 700; }
    .status-recovered {
        background: linear-gradient(135deg, #0A1A20, #0D2630);
        border: 1px solid #3C9FCB; border-radius: 10px;
        padding: 16px; text-align: center;
    }
    .status-recovered .label { color: #4AB8E0; font-size: 20px; font-weight: 700; }
    .metric-row { display: flex; justify-content: space-between;
        padding: 6px 0; border-bottom: 1px solid #1A1A24; }
    .metric-key { color: #7A7A8C; font-size: 13px; }
    .metric-val { color: #E8E8EC; font-size: 13px; font-weight: 600; }
    .big-num { font-size: 40px; font-weight: 700; color: #FF6B4A; }
    .event-item { padding: 8px 0; border-bottom: 1px solid #1A1A24;
        font-size: 13px; color: #C8C8D0; }
    /* fall score progress bar */
    .bar-bg { background: #1A1A24; border-radius: 6px; height: 10px; margin-top: 4px; }
    .bar-fill { border-radius: 6px; height: 10px; }
</style>
""", unsafe_allow_html=True)


# Load models (cached)
@st.cache_resource
def _weights(name):
    """Look for a weights file next to this script, then in ../models and ./models."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.join(here, "..", "models"), os.path.join(here, "models"), os.getcwd()):
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    if name.startswith("yolov8n-pose"):
        return name                      # ultralytics downloads the official pose weights itself
    st.error(f"{name} not found. Download it from the GitHub Releases page and put it in models/.")
    st.stop()


def load_models():
    detect_model = YOLO(_weights("best.pt"))
    pose_model = YOLO(_weights("yolov8n-pose.pt"))
    return detect_model, pose_model

detect_model, pose_model = load_models()


# Rule-based pose analysis
def analyze_pose(keypoints, bbox, img_h, img_w):
    """Compute four body geometry metrics from 17 keypoints."""
    kpts = keypoints[:, :2].cpu().numpy()
    x1, y1, x2, y2 = bbox.cpu().numpy()

    ratio = (y2 - y1) / (x2 - x1) if (x2 - x1) > 0 else 0

    shoulder_mid = (kpts[5] + kpts[6]) / 2
    hip_mid = (kpts[11] + kpts[12]) / 2
    dx = shoulder_mid[0] - hip_mid[0]
    dy = shoulder_mid[1] - hip_mid[1]
    angle = abs(np.degrees(np.arctan2(dx, -dy)))

    center_height = (shoulder_mid[1] + hip_mid[1]) / 2 / img_h

    leg_dy = abs(kpts[13][1] - kpts[15][1]) + abs(kpts[14][1] - kpts[16][1])
    leg_vertical = leg_dy / (2 * img_h)

    return {'ratio': ratio, 'angle': angle,
            'center_height': center_height, 'leg_vertical': leg_vertical}


def compute_fall_score(metrics):
    """Weighted fall confidence score from 0.0 to 1.0."""
    score = 0.0
    if metrics['ratio'] < 0.8:           score += 0.3
    if metrics['angle'] > 45:            score += 0.3
    if metrics['center_height'] > 0.50:  score += 0.2
    if metrics['leg_vertical'] < 0.06:   score += 0.2
    return score


# Session state
if "events" not in st.session_state:
    st.session_state.events = []
if "camera_running" not in st.session_state:
    st.session_state.camera_running = False
if "episodes" not in st.session_state:
    st.session_state.episodes = []          # closed FallEpisode dicts, oldest first
if "webhook_log" not in st.session_state:
    st.session_state.webhook_log = deque(maxlen=50)   # appended to by sender threads


# Sidebar
with st.sidebar:
    st.markdown("## FallGuard")
    st.caption("Real-Time Fall Detection")
    st.markdown("---")
    page = st.radio("Navigation", ["Dashboard", "Actions", "Event History", "Settings"],
                    label_visibility="collapsed")
    st.markdown("---")

    st.markdown("**System Status**")
    st.markdown("🟢 Camera: Online")
    st.markdown("🟢 Detection: Active")
    st.markdown("---")

    st.markdown("**n8n Pipeline**")
    st.caption("All alerts and reports are sent by the n8n workflow.")
    enable_n8n = st.checkbox("Send events to n8n", value=bool(os.getenv("N8N_WEBHOOK_URL")))
    n8n_url = st.text_input("Webhook URL", value=os.getenv("N8N_WEBHOOK_URL", ""),
                            help="Production URL of the n8n Webhook node "
                                 "(…/webhook/<path>), or the Test URL while building.")
    n8n_key = st.text_input("Auth key (X-FallGuard-Key)", type="password",
                            value=os.getenv("N8N_WEBHOOK_KEY", ""),
                            help="Sent as the HTTP header X-FallGuard-Key. Must equal the "
                                 "Header Auth credential on the n8n Webhook node. "
                                 "Leave empty if the node has no authentication.")
    n8n_snapshots = st.checkbox("Include snapshots in payload", value=True,
                                help="Adds the fall frame and latest frame as base64 JPEG "
                                     "(~40-80 KB each).")
    n8n_clip = st.checkbox("Include fall clip (mp4) in payload", value=True,
                           help="Adds a short H.264 clip (about 5 s before to 3 s after the "
                                "fall, 640 px, roughly 0.3-1 MB) that n8n sends to Telegram "
                                "as a video. Needs `pip install imageio-ffmpeg`; without an "
                                "encoder the clip is skipped and n8n falls back to the snapshot.")
    st.markdown("---")

    st.markdown("**Detection Settings**")
    conf_threshold = st.slider("Detection confidence", 0.0, 1.0, 0.25, 0.05)
    st.markdown("**Post-Fall Monitoring**")
    level2_sec = st.number_input("Level 2 threshold (sec)", 1, 120, 10)
    level3_sec = st.number_input("Level 3 threshold (sec)", 1, 300, 30)
    recovery_angle = st.slider("Recovery angle (degrees)", 0, 45, 20)
    lying_angle = st.slider("Lying angle (degrees)", 45, 90, 60,
                            help="Torso angle from vertical at or above which a frame "
                                 "counts as lying (below it: sitting / kneeling / rising).")
    motion_threshold = st.slider("Motionless threshold (box-diagonals / sec)",
                                 0.02, 0.50, 0.15, 0.01,
                                 help="Mean keypoint movement per second, as a fraction of "
                                      "the body bounding-box diagonal. Below this the frame "
                                      "counts as motionless. Calibrate on your own videos.")
    st.markdown("**Fall Analysis**")
    patient_height_cm = st.number_input("Patient height (cm, 0 = unknown)", 0, 220, 0,
                                        help="Only used to express fall height and head "
                                             "speed in cm. Estimates from a single camera "
                                             "are rough (tens of cm).")


# Helper: render status panel based on monitoring state

def clean_html(html):
    """Strip leading whitespace from each line so Streamlit renders HTML
    instead of treating indented lines as a code block."""
    return "".join(line.strip() for line in html.splitlines())


def render_status(state, confidence, fps, down_time=0):
    now = datetime.now().strftime('%H:%M:%S')
    today = datetime.now().strftime('%Y-%m-%d')

    if state == "EMERGENCY":
        banner = '<div class="status-emergency"><div class="label">🚨 EMERGENCY: NO RECOVERY</div></div>'
    elif state == "STILL_DOWN":
        banner = '<div class="status-warning"><div class="label">⚠️ PERSON STILL DOWN</div></div>'
    elif state == "FALLEN":
        banner = '<div class="status-fall"><div class="label">🔴 FALL DETECTED</div></div>'
    elif state == "RECOVERED":
        banner = '<div class="status-recovered"><div class="label">🔵 PERSON RECOVERED</div></div>'
    else:
        banner = '<div class="status-normal"><div class="label">🟢 Normal Activity</div></div>'

    down_row = ""
    if state in ("FALLEN", "STILL_DOWN", "EMERGENCY"):
        down_row = f'<div class="metric-row"><span class="metric-key">Time on ground</span><span class="metric-val">{down_time:.1f}s</span></div>'

    return clean_html(f"""
    <div class="card">
        <div class="card-title">Detection Status</div>
        {banner}
        <div style="margin-top:14px;">
            <div class="metric-row"><span class="metric-key">Confidence</span>
                <span class="metric-val">{confidence:.0%}</span></div>
            {down_row}
            <div class="metric-row"><span class="metric-key">Time</span>
                <span class="metric-val">{now}</span></div>
            <div class="metric-row"><span class="metric-key">Date</span>
                <span class="metric-val">{today}</span></div>
            <div class="metric-row"><span class="metric-key">FPS</span>
                <span class="metric-val">{fps:.1f}</span></div>
        </div>
    </div>""")


def render_score_bar(score):
    """Fall score progress bar with colour based on level."""
    pct = int(score * 100)
    if score >= 0.6:
        color = "#FF6B4A"
    elif score >= 0.3:
        color = "#FFB84A"
    else:
        color = "#3CCB7F"
    return clean_html(f"""
    <div class="card">
        <div class="card-title">Fall Score</div>
        <div style="display:flex; justify-content:space-between;">
            <span class="metric-key">Score</span>
            <span class="metric-val">{score:.2f}</span>
        </div>
        <div class="bar-bg"><div class="bar-fill" style="width:{pct}%; background:{color};"></div></div>
    </div>""")



# Page: Dashboard
if page == "Dashboard":
    st.title("FallGuard Dashboard")
    st.caption("With temporal detection (3-signal: detection + pose + temporal)")

    # choose the video source: uploaded file or live webcam
    source_mode = st.radio("Video source", ["Upload Video", "Live Camera (Webcam)"],
                           horizontal=True)

    uploaded_file = None
    start_camera = False
    if source_mode == "Upload Video":
        uploaded_file = st.file_uploader("Upload a video to detect fall events",
                                         type=["mp4", "avi", "mov"])
    else:
        st.caption("Click Start to begin live detection from your laptop camera. "
                   "Click Stop (or press it again) to end the stream.")
        cc1, cc2 = st.columns([1, 2])
        with cc1:
            camera_index = st.number_input("Camera index", min_value=0, max_value=9,
                                           value=int(st.session_state.get("camera_index", 0)),
                                           help="0 = built-in webcam. An external USB camera is usually 1 "
                                                "(or 0 if the laptop lid camera is disabled). Use Detect cameras.")
            st.session_state.camera_index = int(camera_index)
        with cc2:
            if st.button("Detect cameras"):
                found = []
                for i in range(6):
                    cap_probe = cv2.VideoCapture(i, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(i)
                    ok = cap_probe.isOpened() and cap_probe.read()[0]
                    cap_probe.release()
                    if ok:
                        found.append(i)
                st.session_state.cameras_found = found
            if "cameras_found" in st.session_state:
                st.caption("Cameras that opened: " + (", ".join(map(str, st.session_state.cameras_found)) or "none"))
        c1, c2 = st.columns(2)
        with c1:
            if st.button("▶ Start Camera"):
                st.session_state.camera_running = True
        with c2:
            if st.button("■ Stop Camera"):
                st.session_state.camera_running = False
        start_camera = st.session_state.get("camera_running", False)

    # v10: who will be notified for this source? Asks n8n's fallguard-profile
    # endpoint (same lookup + fallback rule as the workflow) so the demo shows
    # the recipient BEFORE the video runs.
    src_name = "webcam" if source_mode != "Upload Video" else (uploaded_file.name if uploaded_file is not None else None)
    if src_name and enable_n8n and n8n_url:
        prof, perr = None, None
        try:
            base_url = n8n_url.strip().rstrip("/").replace("/webhook-test/", "/webhook/").rsplit("/", 1)[0]
            pr = requests.get(base_url + "/fallguard-profile", params={"source": src_name},
                              headers={"X-FallGuard-Key": n8n_key} if n8n_key else {}, timeout=15)
            prof = pr.json() if pr.ok else None
            if not pr.ok:
                perr = f"HTTP {pr.status_code}"
        except Exception as e:
            perr = str(e)
        if not prof or prof.get("error"):
            st.warning(f"Could not check the residents sheet for **{src_name}** ({perr or (prof or {}).get('error')}). "
                       "Alerts may not be delivered.")
        else:
            chat = str(prof.get("carer_telegram_chat_id", "") or "").strip()
            chat_ok = chat.isdigit() and chat not in ("123456789", "987654321")
            bad_mail = [k for k in ("carer_email", "supporter_email", "gp_email")
                        if "example.com" in str(prof.get(k, "")).lower() or "@" not in str(prof.get(k, ""))]
            match = ("matched row" if prof.get("profile_match") == "exact"
                     else f"no row named '{src_name}', using the fallback row '{prof.get('source_name')}'")
            line = (f"**{src_name}** → resident **{prof.get('resident_name', '?')}** ({match}) · "
                    f"Telegram chat `{chat or '—'}` · carer email `{prof.get('carer_email', '—')}` · "
                    f"supporter `{prof.get('supporter_email', '—')}`")
            if chat_ok and not bad_mail:
                st.success("Notifications ready: " + line)
            elif not chat_ok:
                st.warning("Telegram will FAIL (chat ID missing or placeholder): " + line)
            else:
                st.warning("Email will BOUNCE (placeholder address in " + ", ".join(bad_mail)
                           + " — fix the residents sheet): " + line)

    col_feed, col_status = st.columns([2, 1])
    with col_feed:
        st.markdown('<div class="card-title">Live Feed</div>', unsafe_allow_html=True)
        frame_placeholder = st.empty()
    with col_status:
        status_placeholder = st.empty()
        score_placeholder = st.empty()
        metrics_placeholder = st.empty()

    st.markdown("---")
    col_total, col_events = st.columns([1, 2])
    with col_total:
        total_placeholder = st.empty()
    with col_events:
        events_placeholder = st.empty()

    status_placeholder.markdown(render_status("NORMAL", 0, 0), unsafe_allow_html=True)

    # decide whether to run, and set up the capture source
    run_detection = False
    is_camera = False
    cap = None

    if uploaded_file is not None:
        tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tfile.write(uploaded_file.read())
        cap = cv2.VideoCapture(tfile.name)
        run_detection = True
        is_camera = False
    elif start_camera:
        cam_idx = int(st.session_state.get("camera_index", 0))
        # DirectShow on Windows: external USB cameras often fail to open (or
        # take seconds) with the default MSMF backend
        cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(cam_idx)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(cam_idx)          # fall back to the default backend
        if not cap.isOpened():
            st.error(f"Camera {cam_idx} could not be opened. Close other apps using the camera "
                     f"(Teams, Zoom, browser), check Windows Settings → Privacy → Camera, "
                     f"then press Detect cameras and pick an index that opened.")
            st.session_state.camera_running = False
            cap = None
        else:
            run_detection = True
            is_camera = True

    if run_detection and cap is not None:
        # video timing (only used for uploaded files)
        video_fps = cap.get(cv2.CAP_PROP_FPS) or 24

        # post-fall monitoring state machine
        state = "NORMAL"          # NORMAL, FALLEN, STILL_DOWN, EMERGENCY, RECOVERED
        fall_start_time = None    # timestamp when fall began
        alerted_levels = set()    # which alert levels already sent
        recovery_frames = 0       # consecutive frames meeting recovery criteria
        empty_frames = 0          # consecutive frames with no person at all

        # temporal detection buffer (~1 second of body angles)
        angle_history = deque(maxlen=8)

        # ---- v8: fall episode tracking + n8n ---------------------------------
        episode = None            # FallEpisode while a fall is active, else None
        prev_kpts = None          # keypoints of the previous processed frame (motion)
        prev_video_time = None    # timestamp of the previous processed frame
        # ---- v9: fall analysis ------------------------------------------------
        analyser = None           # FallAnalyser from the trigger until it is done
        recent_frames = deque(maxlen=64)   # (video_time, kpts or None, angle or None), last ~8 s
        # v10: last CLIP_PRE_SEC seconds of display frames for the fall clip
        clip_buffer = ClipBuffer(pre_sec=CLIP_PRE_SEC) if n8n_clip else None
        thresholds = {
            "level2_sec": int(level2_sec),
            "level3_sec": int(level3_sec),
            "recovery_angle_deg": int(recovery_angle),
            "lying_angle_deg": int(lying_angle),
            "motion_threshold_per_sec": float(motion_threshold),
            "detection_conf": float(conf_threshold),
            "patient_height_cm": int(patient_height_cm) or None,
        }
        source_info = {
            "mode": "camera" if is_camera else "upload",
            "name": "webcam" if is_camera else uploaded_file.name,
        }

        def emit_n8n(event_type, ep, extra=None):
            """Send one event for the current episode to n8n (non-blocking)."""
            if not (enable_n8n and n8n_url and ep is not None):
                return
            send_to_n8n(n8n_url,
                        build_payload(event_type, ep, include_snapshots=n8n_snapshots,
                                      extra=extra, include_clip=n8n_clip),
                        log=st.session_state.webhook_log,
                        auth_header=("X-FallGuard-Key", n8n_key) if n8n_key else None)

        def finish_analysis(ep, an):
            """Store the analyser's result on the episode and tell n8n."""
            if ep is None or an is None:
                return None
            an.finish()
            ep.set_analysis(an.result())      # also sets event_features.fall_confidence
            ep.finalize_clip()                # v10: clip = pre-fall buffer + frames so far
            emit_n8n("fall_analysed", ep)
            return None
        # ----------------------------------------------------------------------

        frame_count = 0
        fps = 0.0
        prev_time = time.time()
        camera_start_time = time.time()   # wall-clock start for camera mode

        while cap.isOpened():
            # for camera mode, stop when the user clicks Stop
            if is_camera and not st.session_state.get("camera_running", False):
                break

            ret, frame = cap.read()
            if not ret:
                break

            frame_count += 1
            if frame_count % 3 != 0:
                continue

            # timestamp: video uses frame/fps, camera uses real elapsed time
            if is_camera:
                video_time = time.time() - camera_start_time
            else:
                video_time = frame_count / video_fps

            # display FPS
            now_time = time.time()
            fps = 1.0 / (now_time - prev_time) if (now_time - prev_time) > 0 else 0
            prev_time = now_time

            h, w = frame.shape[:2]

            # step 1: detection
            det_results = detect_model(frame, conf=conf_threshold, verbose=False)
            det_fall = (len(det_results[0].boxes) > 0 and
                        (det_results[0].boxes.cls == 0).any().item())

            display_frame = frame.copy()
            pose_score = 0.0
            metrics = None
            body_angle = 0.0
            person_detected = False   # did the pose model actually find a person?
            kpts_np = None            # (17, 3) x, y, conf of the tracked person
            bbox_np = None            # (x1, y1, x2, y2)

            # step 2: pose analysis (always run pose for monitoring)
            pose_results = pose_model(frame, verbose=False)
            if (pose_results[0].keypoints is not None and
                    len(pose_results[0].keypoints) > 0):
                kpts = pose_results[0].keypoints.data[0]
                bbox = pose_results[0].boxes.xyxy[0]
                metrics = analyze_pose(kpts, bbox, h, w)
                pose_score = compute_fall_score(metrics)
                body_angle = metrics['angle']
                display_frame = pose_results[0].plot()
                person_detected = True
                kpts_np = kpts.cpu().numpy()
                bbox_np = bbox.cpu().numpy()

            # v8: posture bucket + movement since the previous processed frame.
            # motion is None when it cannot be measured (person lost, too few
            # confident keypoints, first frame) and is then NOT counted as still.
            frame_dt = (video_time - prev_video_time) if prev_video_time is not None else None
            posture = classify_posture(person_detected, body_angle,
                                       recovery_angle, lying_angle)
            motion = keypoint_motion(prev_kpts, kpts_np, bbox_np, frame_dt) \
                if (person_detected and prev_kpts is not None) else None
            prev_kpts = kpts_np          # None when nobody was found -> resets motion
            prev_video_time = video_time
            recent_frames.append((video_time, kpts_np,
                                  body_angle if person_detected else None))

            # step 3: temporal detection
            # store the current angle, then compare against the oldest angle
            # in the ~1 second buffer. A rapid increase (> 40 degrees within
            # about 1 second) is the signature of a genuine fall, as opposed
            # to the slow, gradual change seen when sitting or lying down.
            angle_change = 0.0
            temporal_fall = False
            if body_angle > 0:
                angle_history.append(body_angle)
                if len(angle_history) >= 4:
                    angle_change = body_angle - angle_history[0]
                    if angle_change > 40:
                        temporal_fall = True

            # ============================================
            # State machine for post-fall monitoring
            # ============================================
            # Fall is triggered when ANY of three signals fire:
            #   1. the detection model flags a fall, OR
            #   2. single-frame pose rule analysis is confident (>= 0.5), OR
            #   3. temporal analysis sees a rapid body-angle increase (> 40 deg/sec)
            # Combining single-frame and temporal cues improves robustness on
            # unseen scenes and reduces false positives from slow movements.
            detected_fall = det_fall or pose_score >= 0.5 or temporal_fall

            # ----------------------------------------------------------
            # Scene-change reset for compilation videos.
            # IMPORTANT: we only reset from NORMAL/RECOVERED states. Once a
            # fall is active (FALLEN/STILL_DOWN/EMERGENCY) we NEVER reset on
            # "no person", because a fallen person who the pose model cannot
            # see is exactly the situation we must keep escalating — losing
            # the person is a danger signal, not an all-clear.
            # ----------------------------------------------------------
            nobody_here = (not person_detected and
                           len(det_results[0].boxes) == 0)
            if nobody_here:
                empty_frames += 1
            else:
                empty_frames = 0

            # only reset when we are NOT in an active fall, and the scene has
            # been genuinely empty for a sustained period (~2.5s)
            if empty_frames >= 20 and state in ("NORMAL", "RECOVERED"):
                state = "NORMAL"
                fall_start_time = None
                alerted_levels = set()
                recovery_frames = 0

            if state in ("NORMAL", "RECOVERED"):
                if detected_fall:
                    # new fall begins
                    state = "FALLEN"
                    fall_start_time = video_time
                    alerted_levels = {1}
                    st.session_state.events.append({
                        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'video_time': f"{video_time:.1f}s",
                        'level': 'Fall Detected',
                        'confidence': pose_score,
                    })
                    # v8: open the episode record. Which of the three signals
                    # fired is kept so the report can say why the fall was called.
                    episode = FallEpisode(
                        fall_video_time=video_time,
                        trigger_signals={
                            "detector": bool(det_fall),
                            "pose_rule": bool(pose_score >= 0.5),
                            "temporal": bool(temporal_fall),
                            "pose_score_at_fall": float(pose_score),
                            "angle_change_deg": float(angle_change),
                        },
                        thresholds=thresholds, source=source_info)
                    episode.fall_snapshot_b64 = encode_snapshot(display_frame)
                    episode.latest_snapshot_b64 = episode.fall_snapshot_b64
                    episode.update(video_time, posture, motion, motion_threshold)
                    episode.event_features = {"analysis_status": "pending"}
                    if clip_buffer is not None:
                        episode.start_clip(clip_buffer.snapshot(), post_sec=CLIP_POST_SEC)
                    emit_n8n("fall_detected", episode)

                    # v9: analyse the frames around the fall. The buffer already
                    # holds the last few seconds, so a late trigger (person
                    # already on the floor) still sees the fall motion itself.
                    analyser = FallAnalyser(list(recent_frames), video_time, lying_angle,
                                            patient_height_cm=patient_height_cm or None)

            elif state in ("FALLEN", "STILL_DOWN", "EMERGENCY"):
                # v8: attribute this frame's interval to its posture bucket
                if episode is not None:
                    episode.update(video_time, posture, motion, motion_threshold)

                # v9: feed the analyser until the body settles (or 2.5 s pass)
                if analyser is not None:
                    analyser.add_frame(video_time, kpts_np,
                                       body_angle if person_detected else None)
                    if analyser.done:
                        analyser = finish_analysis(episode, analyser)

                # ----------------------------------------------------------
                # Recovery requires POSITIVE evidence that the person stood up.
                # Critically, if the pose model cannot find a person, we do
                # NOT treat that as recovery — a person who cannot be detected
                # may well be lying on the ground or occluded. "No person"
                # is a reason to stay alert, not to stand down.
                # We also require the upright pose to hold for several
                # consecutive frames to avoid single-frame noise.
                # ----------------------------------------------------------
                upright_now = (person_detected and
                               body_angle < recovery_angle and
                               pose_score < 0.3 and
                               not det_fall)

                if upright_now:
                    recovery_frames += 1
                else:
                    recovery_frames = 0   # reset if any frame breaks the streak

                # require ~4 consecutive upright frames (about 0.5s) to confirm
                if recovery_frames >= 4:
                    state = "RECOVERED"
                    recovery_frames = 0
                    st.session_state.events.append({
                        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'video_time': f"{video_time:.1f}s",
                        'level': 'Recovered',
                        'confidence': pose_score,
                    })
                    # v8: close the episode -> full record goes to n8n for the report
                    if episode is not None:
                        episode.latest_snapshot_b64 = encode_snapshot(display_frame)
                        episode.close("recovered", video_time)
                        analyser = finish_analysis(episode, analyser)   # if still pending
                        st.session_state.episodes.append(episode.to_dict())
                        emit_n8n("episode_closed", episode)
                        episode = None
                else:
                    # still on the ground - check escalation
                    down_time = video_time - fall_start_time
                    if down_time >= level3_sec and 3 not in alerted_levels:
                        state = "EMERGENCY"
                        alerted_levels.add(3)
                        st.session_state.events.append({
                            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'video_time': f"{video_time:.1f}s",
                            'level': 'EMERGENCY: No Recovery',
                            'confidence': pose_score,
                        })
                        if episode is not None:
                            episode.add_alert(3, video_time)
                            episode.latest_snapshot_b64 = encode_snapshot(display_frame)
                            emit_n8n("escalation", episode, extra={"alert_level": 3})
                    elif down_time >= level2_sec and 2 not in alerted_levels:
                        state = "STILL_DOWN"
                        alerted_levels.add(2)
                        st.session_state.events.append({
                            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'video_time': f"{video_time:.1f}s",
                            'level': 'Person Still Down',
                            'confidence': pose_score,
                        })
                        if episode is not None:
                            episode.add_alert(2, video_time)
                            episode.latest_snapshot_b64 = encode_snapshot(display_frame)
                            emit_n8n("escalation", episode, extra={"alert_level": 2})

            # compute time on ground for display
            down_time_display = 0
            if state in ("FALLEN", "STILL_DOWN", "EMERGENCY") and fall_start_time is not None:
                down_time_display = video_time - fall_start_time

            # draw banner on frame
            if state == "EMERGENCY":
                cv2.rectangle(display_frame, (0, 0), (w, 40), (0, 0, 200), -1)
                cv2.putText(display_frame, "EMERGENCY: NO RECOVERY", (10, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
            elif state == "STILL_DOWN":
                cv2.rectangle(display_frame, (0, 0), (w, 40), (0, 120, 255), -1)
                cv2.putText(display_frame, "PERSON STILL DOWN", (10, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
            elif state == "FALLEN":
                cv2.rectangle(display_frame, (0, 0), (w, 40), (0, 0, 255), -1)
                cv2.putText(display_frame, "FALL DETECTED", (10, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)

            # v10: rolling buffer for the fall clip; while an episode is
            # collecting, the same JPEG also goes into its recorder
            if clip_buffer is not None:
                jpeg = clip_buffer.push(video_time, display_frame)
                if episode is not None:
                    episode.record_clip_frame(video_time, jpeg)

            # show frame
            frame_rgb = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
            frame_placeholder.image(frame_rgb, width="stretch")

            # update panels
            status_placeholder.markdown(
                render_status(state, pose_score, fps, down_time_display),
                unsafe_allow_html=True)
            score_placeholder.markdown(render_score_bar(pose_score),
                                       unsafe_allow_html=True)

            # Body geometry + state machine status (always rendered, even when
            # the pose model loses the person, so the monitoring state stays visible)
            angle_str = f"{metrics['angle']:.1f}°" if metrics else "—"
            ratio_str = f"{metrics['ratio']:.2f}" if metrics else "—"
            person_str = "Yes" if person_detected else "NO (lost)"
            # v8: episode-level figures (only meaningful while a fall is active)
            if episode is not None:
                posture_str = posture.replace("_", " ")
                lying_str = f"{episode.time_lying_sec:.1f}s"
                still_str = f"{episode.motionless_sec:.1f}s"
                motion_str = f"{motion:.2f}/s" if motion is not None else "—"
                ef = episode.event_features or {}
                if ef.get("analysis_status") == "done":
                    head_str = ef["head_impact"]["risk"]
                    prefall_str = "{} · {}".format(ef["prefall"]["posture"].replace("_", " "),
                                                   ef.get("fall_confidence", "").replace("_", " "))
                else:
                    head_str = prefall_str = "analysing…"
            else:
                posture_str, lying_str, still_str, motion_str = "—", "—", "—", "—"
                head_str = prefall_str = "—"
            metrics_placeholder.markdown(clean_html(f"""
            <div class="card">
                <div class="card-title">Monitoring State</div>
                <div class="metric-row"><span class="metric-key">State</span>
                    <span class="metric-val">{state}</span></div>
                <div class="metric-row"><span class="metric-key">Person detected</span>
                    <span class="metric-val">{person_str}</span></div>
                <div class="metric-row"><span class="metric-key">Time on ground</span>
                    <span class="metric-val">{down_time_display:.1f}s</span></div>
                <div class="metric-row"><span class="metric-key">Posture</span>
                    <span class="metric-val">{posture_str}</span></div>
                <div class="metric-row"><span class="metric-key">Time lying</span>
                    <span class="metric-val">{lying_str}</span></div>
                <div class="metric-row"><span class="metric-key">Time motionless</span>
                    <span class="metric-val">{still_str}</span></div>
                <div class="metric-row"><span class="metric-key">Movement</span>
                    <span class="metric-val">{motion_str}</span></div>
                <div class="metric-row"><span class="metric-key">Pre-fall posture</span>
                    <span class="metric-val">{prefall_str}</span></div>
                <div class="metric-row"><span class="metric-key">Head-impact risk</span>
                    <span class="metric-val">{head_str}</span></div>
                <div class="metric-row"><span class="metric-key">Body angle</span>
                    <span class="metric-val">{angle_str}</span></div>
                <div class="metric-row"><span class="metric-key">Box ratio</span>
                    <span class="metric-val">{ratio_str}</span></div>
                <div class="metric-row"><span class="metric-key">Angle change (1s)</span>
                    <span class="metric-val">{angle_change:.1f}°</span></div>
            </div>"""), unsafe_allow_html=True)

            # analytics
            fall_count = sum(1 for e in st.session_state.events
                             if e['level'] == 'Fall Detected')
            total_placeholder.markdown(clean_html(f"""
            <div class="card">
                <div class="card-title">Total Falls</div>
                <div class="big-num">{fall_count}</div>
            </div>"""), unsafe_allow_html=True)

            recent = list(reversed(st.session_state.events))[:6]
            events_html = '<div class="card"><div class="card-title">Recent Events</div>'
            if recent:
                for e in recent:
                    icon = "🚨" if "EMERGENCY" in e['level'] else \
                           "⚠️" if "Still Down" in e['level'] else \
                           "🔵" if e['level'] == "Recovered" else "🔴"
                    events_html += f'<div class="event-item">{icon} {e["level"]} — {e["time"]}</div>'
            else:
                events_html += '<div class="event-item" style="color:#5A5A6C;">No events yet</div>'
            events_html += '</div>'
            events_placeholder.markdown(events_html, unsafe_allow_html=True)

            time.sleep(0.03)

        cap.release()

        # v8: the stream ended while a fall was still active. That is NOT a
        # recovery — close the record as "unresolved" so the report says the
        # person was last seen on the ground, and how long they had been there.
        if episode is not None:
            episode.latest_snapshot_b64 = encode_snapshot(display_frame)
            episode.close("unresolved", prev_video_time if prev_video_time is not None
                          else episode.fall_video_time)
            analyser = finish_analysis(episode, analyser)   # if still pending
            st.session_state.episodes.append(episode.to_dict())
            emit_n8n("episode_closed", episode)
            st.warning(f"Stream ended with the person still down "
                       f"({episode.time_on_ground_sec:.1f}s on ground). "
                       f"Episode closed as unresolved.")
            episode = None

        if is_camera:
            st.info("Camera stopped.")
        else:
            st.success("Video processing complete.")


# Page: Event History
elif page == "Actions":
    # ------------------------------------------------------------------
    # v10: decisions the n8n workflow is waiting for. n8n queues each one
    # in the pending_actions sheet (with the execution's resume URL); the
    # carer / nurse answers here and the answer is POSTed straight back
    # to n8n, together with who decided and when (audit trail).
    # ------------------------------------------------------------------
    st.title("Actions")
    st.caption("Decisions the FallGuard workflow is waiting for. Every answer goes straight to n8n "
               "and is recorded with your name and the time.")
    actor = st.text_input("Your name and role (recorded with each decision)",
                          value=st.session_state.get("actor", ""), placeholder="e.g. Sam Lee, carer / RN")
    st.session_state.actor = actor
    if not n8n_url:
        st.error("Set the n8n Webhook URL in the sidebar first.")
        st.stop()
    base_url = n8n_url.strip().rstrip("/").replace("/webhook-test/", "/webhook/")
    pending_url = base_url.rsplit("/", 1)[0] + "/fallguard-pending"
    if not n8n_key:
        st.warning("Auth key (X-FallGuard-Key) is empty in the sidebar: n8n will answer 403.")
    hdrs = {"X-FallGuard-Key": n8n_key} if n8n_key else {}
    if "done_actions" not in st.session_state:
        st.session_state.done_actions = {}
    if "decision_log" not in st.session_state:
        st.session_state.decision_log = []

    rows = []
    try:
        resp = requests.get(pending_url, headers=hdrs, timeout=20)
        resp.raise_for_status()
        rows = resp.json() if resp.text.strip() else []
        if isinstance(rows, dict):
            rows = [rows]
    except Exception as e:
        st.error(f"Could not load pending actions from {pending_url}: {e}")
    total_rows = len(rows)
    rows = [r for r in rows if str(r.get("status", "")).lower() == "open"
            and r.get("resume_url") not in st.session_state.done_actions]

    c1, c2 = st.columns([1, 5])
    with c1:
        if st.button("Refresh"):
            st.rerun()
    with c2:
        st.caption(f"{len(rows)} pending ({total_rows} rows returned) · source: {pending_url}")

    STEPS = {
        "post_fall_check": ("Post-fall check", "Check on the person first, then answer. Your decision sets the next steps."),
        "supporter_notice_approval": ("Approve family notice", "Approve to send the fall notice to the registered supporter; decline to hold it."),
        "outcome": ("Record outcome", "How did it end? This closes the episode."),
        "education_sheet_approval": ("Approve information sheet (nurse / GP)", "Approve to send the after-fall information sheet to the family; decline to hold it."),
    }

    def send_answer(row, payload):
        if not actor.strip():
            st.error("Enter your name and role first.")
            return False
        payload = dict(payload)
        payload["decided_by"] = actor.strip()
        payload["decided_at"] = now_iso()
        payload["step"] = row.get("step")
        payload["episode_id"] = row.get("episode_id")
        try:
            r = requests.post(row["resume_url"], json=payload, headers=hdrs, timeout=20)
            ok = 200 <= r.status_code < 300
        except Exception as e:
            st.error(f"Could not reach n8n: {e}")
            return False
        if not ok:
            st.error(f"n8n answered HTTP {r.status_code}: {r.text[:200]}")
            return False
        st.session_state.done_actions[row["resume_url"]] = payload
        st.session_state.decision_log.append({"time": payload["decided_at"], "who": actor.strip(),
                                              "resident": row.get("resident"), "step": row.get("step"),
                                              "answer": {k: v for k, v in payload.items()
                                                         if k not in ("decided_by", "decided_at", "step", "episode_id")}})
        return True

    if not rows:
        st.success("No pending actions. The workflow will add one here when it needs a decision.")
    for i, row in enumerate(rows):
        step = row.get("step", "")
        title, hint = STEPS.get(step, (step, ""))
        with st.container(border=True):
            st.markdown(f"**{title}** — {row.get('resident', '')}  ·  queued {row.get('created_at', '')}")
            st.caption(hint)
            with st.expander("Details from the workflow", expanded=(step != "education_sheet_approval")):
                st.text(row.get("detail", ""))
            with st.form(key=f"action_{i}"):
                if step == "post_fall_check":
                    a1 = st.selectbox("ACD reviewed", ["Yes", "No", "No ACD on file"])
                    a2 = st.selectbox("Injury seen", ["None", "Minor", "Possible fracture or bleeding", "Head wound"])
                    a3 = st.selectbox("Alert and responsive", ["Yes", "Drowsy or confused", "Unresponsive"])
                    a4 = st.selectbox("Pain", ["None", "Mild", "Severe"])
                    a5 = st.selectbox("Can stand and walk", ["Yes", "With help", "No"])
                    a6 = st.selectbox("Decision", ["Call 000", "Contact GP or virtual care", "Observe at home"])
                    a7 = st.text_input("Notes")
                    if st.form_submit_button("Submit decision"):
                        if send_answer(row, {"ACD reviewed": a1, "Injury seen": a2, "Alert and responsive": a3,
                                             "Pain": a4, "Can stand and walk": a5, "Decision": a6, "Notes": a7}):
                            st.success(f"Sent: {a6}. n8n continues with that branch.")
                            st.rerun()
                elif step == "outcome":
                    o1 = st.selectbox("Outcome", ["Stayed home", "ED then home", "Admitted"])
                    o2 = st.text_input("Hospital (if any)")
                    o3 = st.text_input("Notes")
                    if st.form_submit_button("Submit outcome"):
                        if send_answer(row, {"Outcome": o1, "Hospital": o2, "Notes": o3}):
                            st.success(f"Sent: {o1}.")
                            st.rerun()
                else:   # approvals
                    note = st.text_input("Comment (optional)")
                    b1, b2 = st.columns(2)
                    approve = b1.form_submit_button("Approve")
                    decline = b2.form_submit_button("Decline")
                    if approve or decline:
                        if send_answer(row, {"approved": bool(approve), "comment": note}):
                            st.success("Approved." if approve else "Declined.")
                            st.rerun()

    if st.session_state.decision_log:
        st.markdown("---")
        st.markdown("**Decisions made in this session**")
        st.dataframe(pd.DataFrame(st.session_state.decision_log), width="stretch", hide_index=True)

elif page == "Event History":
    st.title("Event History")

    # v8: one row per fall episode with the durations that feed the report
    st.markdown("### Fall Episodes")
    episodes = st.session_state.episodes
    if not episodes:
        st.info("No completed fall episodes yet (an episode closes on recovery "
                "or when the video/camera stops).")
    else:
        rows = []
        for ep in episodes:
            pb = ep["posture_breakdown_sec"]
            rows.append({
                "Episode": ep["episode_id"],
                "Fall time": ep["fall_time"],
                "Outcome": ep["status"],
                "On ground (s)": ep["time_on_ground_sec"],
                "Lying (s)": pb["lying"],
                "Partially upright (s)": pb["partially_upright"],
                "Not visible (s)": pb["not_visible"],
                "Motionless (s)": ep["time_motionless_sec"],
                "Max level": ep["max_alert_level"],
                "Pre-fall posture": (ep.get("event_features") or {}).get("prefall", {}).get("posture", "—"),
                "Head risk": (ep.get("event_features") or {}).get("head_impact", {}).get("risk", "—"),
                "Fall confidence": (ep.get("event_features") or {}).get("fall_confidence", "—"),
                "Source": ep["source"]["name"],
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.download_button(
            "Download episodes (JSON)",
            data=json.dumps(episodes, indent=2),
            file_name=f"fallguard_episodes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json")

    st.markdown("### Alert Log")
    if len(st.session_state.events) == 0:
        st.info("No fall events recorded yet.")
    else:
        st.markdown(f"**Total events:** {len(st.session_state.events)}")
        for i, event in enumerate(reversed(st.session_state.events), 1):
            icon = "🚨" if "EMERGENCY" in event['level'] else \
                   "⚠️" if "Still Down" in event['level'] else \
                   "🔵" if event['level'] == "Recovered" else "🔴"
            vtime = event.get('video_time', '—')
            st.markdown(f'<div class="event-item">{i}. {icon} <b>{event["level"]}</b> '
                        f'— video {vtime} &nbsp;|&nbsp; {event["time"]}</div>',
                        unsafe_allow_html=True)
        if st.button("Clear history"):
            st.session_state.events = []
            st.rerun()


# Page: Settings
elif page == "Settings":
    st.title("Settings")
    st.markdown(clean_html("""
    <div class="card">
        <div class="card-title">Model</div>
        <div class="metric-row"><span class="metric-key">Detection</span>
            <span class="metric-val">YOLOv8s (fine-tuned on Le2i)</span></div>
        <div class="metric-row"><span class="metric-key">Pose</span>
            <span class="metric-val">YOLOv8n-Pose (COCO)</span></div>
        <div class="metric-row"><span class="metric-key">Fall threshold</span>
            <span class="metric-val">Pose score >= 0.3</span></div>
    </div>
    """), unsafe_allow_html=True)
    st.markdown(
"""**Post-Fall Monitoring Levels:**
- **Level 1 (Fall Detected):** triggered immediately when a fall is detected
- **Level 2 (Person Still Down):** person remains on the ground past the Level 2 threshold
- **Level 3 (Emergency):** person remains on the ground past the Level 3 threshold
- **Recovered:** body angle returns below the recovery angle

Each level is sent to n8n as an event; the n8n workflow decides who is notified and how. Thresholds are adjustable in the sidebar.

**Episode measurements (v8):** while a fall is active, every processed frame is
bucketed as *lying* (torso angle ≥ lying angle), *partially upright* (sitting,
kneeling, standing up, or upright but not yet confirmed) or *not visible*; the
three add up to the time on ground. *Motionless* counts frames where mean keypoint
movement is below the motionless threshold — it overlaps the posture buckets.

**Fall analysis (v9):** about two seconds after a fall is called, the frames
around it are analysed. *Pre-fall posture* (standing / sitting / lying) gives the
fall-height category; with the patient's height set, hip and head height are also
given in cm (rough, single-camera estimate). *Head-impact risk* combines how fast
the head came down, whether it reached floor level, and whether it got there before
the hips — it is a risk estimate from 2-D keypoints, not a detection of contact,
and reads *unknown* when the head keypoints were not visible.""")

    # v8: n8n integration
    st.markdown("### n8n Pipeline")
    st.markdown(
"""Events are POSTed as JSON to the webhook URL in the sidebar:

| `event_type` | when | typical n8n action |
|---|---|---|
| `fall_detected` | the frame a fall is called (Level 1) | immediate short alert |
| `escalation` | Level 2 / Level 3 fires (`alert_level` at top level) | escalating alert |
| `episode_closed` | recovery confirmed, or stream ended while down (`episode.status` = `recovered` / `unresolved`) | grade + LLM caregiver report |
| `fall_analysed` | ~2 s after the fall, when `episode.event_features` (pre-fall posture, head-impact risk) is ready | optional follow-up alert |
| `test` | the button below | build the workflow without running video |

`episode` carries `time_on_ground_sec`, `posture_breakdown_sec`
(`lying` / `partially_upright` / `not_visible`), `time_motionless_sec`,
`max_alert_level`, `alerts[]`, `trigger_signals`, `thresholds`, and
`event_features` (`prefall.posture`, `prefall.fall_height_category`,
`head_impact.risk`, …). In n8n these are under `$json.body` (for example
`{{ $json.body.episode.time_on_ground_sec }}`). If an auth key is set in the
sidebar it is sent as the `X-FallGuard-Key` header — configure the same name and
value as a Header Auth credential on the Webhook node.
Full schema and workflow spec: `n8n/README.md`.""")

    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button("Send test payload to n8n"):
            if not n8n_url:
                st.error("Enter the webhook URL in the sidebar first.")
            else:
                ep = make_sample_episode()
                if n8n_snapshots:
                    blank = np.zeros((360, 640, 3), dtype=np.uint8)
                    cv2.putText(blank, "FallGuard test snapshot", (40, 190),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
                    ep.fall_snapshot_b64 = encode_snapshot(blank)
                    ep.latest_snapshot_b64 = ep.fall_snapshot_b64
                if n8n_clip:
                    sample_clip = make_sample_clip()
                    if sample_clip is None:
                        st.warning("No mp4 encoder found (pip install imageio-ffmpeg); "
                                   "test payload sent without a clip.")
                    else:
                        ep.clip_b64, ep.clip_meta = sample_clip
                send_to_n8n(n8n_url,
                            build_payload("test", ep, include_snapshots=n8n_snapshots,
                                          include_clip=n8n_clip),
                            log=st.session_state.webhook_log,
                            auth_header=("X-FallGuard-Key", n8n_key) if n8n_key else None)
                time.sleep(1.5)     # give the background thread time to log the result
                st.rerun()
    with c2:
        with st.expander("Sample payload (episode_closed, no snapshots)"):
            st.code(json.dumps(build_payload("episode_closed", make_sample_episode()),
                               indent=2), language="json")

    st.markdown("**Recent deliveries**")
    log = list(st.session_state.webhook_log)
    if not log:
        st.caption("Nothing sent yet.")
    else:
        for entry in reversed(log[-10:]):
            icon = "🟢" if entry.get("ok") else "🔴"
            st.markdown(f'<div class="event-item">{icon} {entry["event_type"]} '
                        f'— {entry["episode_id"]} — HTTP {entry["status"]} '
                        f'— {entry["time"]}</div>', unsafe_allow_html=True)
