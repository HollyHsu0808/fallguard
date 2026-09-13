"""
FallGuard: Real-Time Elderly Fall Detection and Alert System
Streamlit dashboard - dark tech theme with post-fall monitoring

Run with:  streamlit run app.py
"""

import os
from pathlib import Path

import streamlit as st
import cv2
import numpy as np
import tempfile
import time
import requests
from datetime import datetime
from ultralytics import YOLO

# ============================================================
# Page configuration
# ============================================================
st.set_page_config(page_title="FallGuard Dashboard", layout="wide",
                   initial_sidebar_state="expanded")

# ============================================================
# Custom dark theme styling
# ============================================================
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


# ============================================================
# Load models (cached)
# ============================================================
MODELS_DIR = Path(__file__).resolve().parent / "models"
DETECT_WEIGHTS = MODELS_DIR / "best.pt"          # fine-tuned fall detector (not in git)
POSE_WEIGHTS = MODELS_DIR / "yolov8n-pose.pt"    # standard Ultralytics weights (auto-download)


@st.cache_resource
def load_models():
    if not DETECT_WEIGHTS.exists():
        st.error(f"Fine-tuned detector not found at {DETECT_WEIGHTS}. "
                 "Download best.pt from the project's GitHub Release and place it in models/.")
        st.stop()
    detect_model = YOLO(str(DETECT_WEIGHTS))
    pose_model = YOLO(str(POSE_WEIGHTS))
    return detect_model, pose_model

detect_model, pose_model = load_models()


# ============================================================
# Rule-based pose analysis
# ============================================================
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


# ============================================================
# Secrets (never hard-code credentials in this file)
# ============================================================
def get_secret(name: str) -> str:
    """Read a credential from the environment, then Streamlit secrets, else empty."""
    value = os.environ.get(name, "")
    if not value:
        try:
            value = str(st.secrets.get(name, ""))
        except Exception:
            value = ""
    return value


# ============================================================
# Telegram alert
# ============================================================
def send_telegram_alert(token, chat_id, image, message):
    """Send an alert with screenshot to Telegram."""
    try:
        _, buffer = cv2.imencode('.jpg', image)
        files = {'photo': ('fall.jpg', buffer.tobytes(), 'image/jpeg')}
        caption = f"{message}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        requests.post(url, data={'chat_id': chat_id, 'caption': caption},
                      files=files, timeout=10)
        return True
    except Exception as e:
        st.warning(f"Telegram alert failed: {e}")
        return False


# ============================================================
# Session state
# ============================================================
if "events" not in st.session_state:
    st.session_state.events = []
if "camera_running" not in st.session_state:
    st.session_state.camera_running = False


# ============================================================
# Sidebar
# ============================================================
with st.sidebar:
    st.markdown("## FallGuard")
    st.caption("Real-Time Fall Detection")
    st.markdown("---")
    page = st.radio("Navigation", ["Dashboard", "Event History", "Settings"],
                    label_visibility="collapsed")
    st.markdown("---")

    st.markdown("**System Status**")
    st.markdown("🟢 Camera: Online")
    st.markdown("🟢 Detection: Active")
    st.markdown("---")

    st.markdown("**Telegram Alerts**")
    enable_telegram = st.checkbox("Enable alerts", value=False)
    telegram_token = st.text_input("Bot Token", type="password",
                                   value=get_secret("FALLGUARD_TELEGRAM_TOKEN"))
    telegram_chat_id = st.text_input("Chat ID",
                                     value=get_secret("FALLGUARD_TELEGRAM_CHAT_ID"))
    st.caption("Leave blank to keep alerts off, or set FALLGUARD_TELEGRAM_TOKEN "
               "and FALLGUARD_TELEGRAM_CHAT_ID as environment variables.")
    st.markdown("---")

    st.markdown("**Detection Settings**")
    conf_threshold = st.slider("Detection confidence", 0.0, 1.0, 0.25, 0.05)
    st.markdown("**Post-Fall Monitoring**")
    level2_sec = st.number_input("Level 2 threshold (sec)", 1, 120, 10)
    level3_sec = st.number_input("Level 3 threshold (sec)", 1, 300, 30)
    recovery_angle = st.slider("Recovery angle (degrees)", 0, 45, 20)


# ============================================================
# Helper: render status panel based on monitoring state
# ============================================================
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


# ============================================================
# Page: Dashboard
# ============================================================
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
        c1, c2 = st.columns(2)
        with c1:
            if st.button("▶ Start Camera"):
                st.session_state.camera_running = True
        with c2:
            if st.button("■ Stop Camera"):
                st.session_state.camera_running = False
        start_camera = st.session_state.get("camera_running", False)

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
        tfile.flush()
        tfile.close()
        cap = cv2.VideoCapture(tfile.name)
        run_detection = True
        is_camera = False
    elif start_camera:
        cap = cv2.VideoCapture(0)   # 0 = default laptop webcam
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
        from collections import deque
        angle_history = deque(maxlen=8)

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
                    if enable_telegram and telegram_token and telegram_chat_id:
                        send_telegram_alert(telegram_token, telegram_chat_id,
                                            display_frame, "🔴 FALL DETECTED")

            elif state in ("FALLEN", "STILL_DOWN", "EMERGENCY"):
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
                    if enable_telegram and telegram_token and telegram_chat_id:
                        send_telegram_alert(telegram_token, telegram_chat_id,
                                            display_frame, "🔵 PERSON RECOVERED")
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
                        if enable_telegram and telegram_token and telegram_chat_id:
                            send_telegram_alert(telegram_token, telegram_chat_id,
                                display_frame, f"🚨 EMERGENCY: No recovery after {level3_sec}s")
                    elif down_time >= level2_sec and 2 not in alerted_levels:
                        state = "STILL_DOWN"
                        alerted_levels.add(2)
                        st.session_state.events.append({
                            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'video_time': f"{video_time:.1f}s",
                            'level': 'Person Still Down',
                            'confidence': pose_score,
                        })
                        if enable_telegram and telegram_token and telegram_chat_id:
                            send_telegram_alert(telegram_token, telegram_chat_id,
                                display_frame, f"⚠️ Person still down after {level2_sec}s")

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
            metrics_placeholder.markdown(clean_html(f"""
            <div class="card">
                <div class="card-title">Monitoring State</div>
                <div class="metric-row"><span class="metric-key">State</span>
                    <span class="metric-val">{state}</span></div>
                <div class="metric-row"><span class="metric-key">Person detected</span>
                    <span class="metric-val">{person_str}</span></div>
                <div class="metric-row"><span class="metric-key">Time on ground</span>
                    <span class="metric-val">{down_time_display:.1f}s</span></div>
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
        if is_camera:
            st.info("Camera stopped.")
        else:
            try:
                os.remove(tfile.name)
            except OSError:
                pass
            st.success("Video processing complete.")


# ============================================================
# Page: Event History
# ============================================================
elif page == "Event History":
    st.title("Event History")
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


# ============================================================
# Page: Settings
# ============================================================
elif page == "Settings":
    st.title("Settings")
    st.markdown(clean_html("""
    <div class="card">
        <div class="card-title">Model</div>
        <div class="metric-row"><span class="metric-key">Detection</span>
            <span class="metric-val">YOLOv8s (fine-tuned on Le2i)</span></div>
        <div class="metric-row"><span class="metric-key">Pose</span>
            <span class="metric-val">YOLOv8n-Pose (COCO)</span></div>
        <div class="metric-row"><span class="metric-key">Fall trigger</span>
            <span class="metric-val">Detector OR pose score >= 0.5 OR angle rise > 40 deg in 1 s</span></div>
    </div>
    """), unsafe_allow_html=True)
    st.markdown(
"""**Post-Fall Monitoring Levels:**
- **Level 1 (Fall Detected):** triggered immediately when a fall is detected
- **Level 2 (Person Still Down):** person remains on the ground past the Level 2 threshold
- **Level 3 (Emergency):** person remains on the ground past the Level 3 threshold
- **Recovered:** a visible, upright person (angle below the recovery angle, pose score below 0.3, detector clear) for 4 consecutive processed frames

Every state change, including recovery, sends a separate Telegram alert. Thresholds are adjustable in the sidebar.""")
