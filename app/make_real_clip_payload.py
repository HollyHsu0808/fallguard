"""
Headless helper: run the FallGuard fall detector (best.pt) over a video, cut a
real clip around the first detected fall (CLIP_PRE_SEC before, CLIP_POST_SEC
after) with the same ClipBuffer / encode_clip code the app uses, and write a
`fall_analysed` payload that carries it.

    python make_real_clip_payload.py "video (13).avi" ../test_payloads/fall_analysed_real_clip.json

The payload's numbers (time on floor, head risk ...) are copied from
test_payloads/fall_analysed.json; only source.name, the clip, the fall frame
snapshot and the timestamps are real. Use the Streamlit app for full analysis.
"""
import json
import os
import sys

import cv2
from ultralytics import YOLO

from fall_episode import ClipBuffer, encode_clip, encode_snapshot, now_iso

CLIP_PRE_SEC, CLIP_POST_SEC = 5.0, 3.0
CONF = 0.25
STEP = 3                       # the app processes every 3rd frame


def main(video, out_json, template):
    here = os.path.dirname(os.path.abspath(__file__))
    w = next((c for c in (os.path.join(here, "best.pt"), os.path.join(here, "..", "models", "best.pt")) if os.path.exists(c)), None)
    if w is None:
        sys.exit("best.pt not found (put it next to this script or in ../models)")
    detect = YOLO(w)
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    buf = ClipBuffer(pre_sec=CLIP_PRE_SEC)
    frame_no, fall_t, fall_snapshot = 0, None, None
    frames_after = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_no += 1
        if frame_no % STEP:
            continue
        t = frame_no / fps
        res = detect(frame, conf=CONF, verbose=False)[0]
        is_fall = len(res.boxes) > 0 and (res.boxes.cls == 0).any().item()
        shown = res.plot() if len(res.boxes) else frame.copy()
        if fall_t is None and is_fall:
            fall_t = t
            fall_snapshot = encode_snapshot(shown)
            pre = buf.snapshot()
            print("fall detected at %.2f s (frame %d); pre-fall frames: %d" % (t, frame_no, len(pre)))
            cv2.rectangle(shown, (0, 0), (shown.shape[1], 30), (0, 0, 255), -1)
            cv2.putText(shown, "FALL DETECTED", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            frames_after = list(pre)
        elif fall_t is not None:
            cv2.rectangle(shown, (0, 0), (shown.shape[1], 30), (0, 0, 255), -1)
            cv2.putText(shown, "FALL DETECTED", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        jpeg = buf.push(t, shown)
        if fall_t is not None:
            frames_after.append((t, jpeg))
            if t - fall_t >= CLIP_POST_SEC:
                break
    cap.release()
    if fall_t is None:
        sys.exit("no fall detected in %s" % video)
    res = encode_clip(frames_after)
    if res is None:
        sys.exit("no mp4 encoder (pip install imageio-ffmpeg)")
    b64, meta = res
    meta["pre_fall_sec"] = round(fall_t - frames_after[0][0], 2)
    meta["post_fall_sec"] = round(frames_after[-1][0] - fall_t, 2)
    payload = json.load(open(template, encoding="utf-8"))
    name = os.path.basename(video)
    payload["source"]["name"] = name
    payload["episode"]["source"]["name"] = name
    payload["sent_at"] = now_iso()
    payload["episode"]["fall_time"] = now_iso()
    payload["episode"]["fall_video_time_sec"] = round(fall_t, 2)
    payload["clip"] = dict(meta)
    payload["clip"]["mp4_base64"] = b64
    payload["snapshots"] = {"fall_frame_jpeg_base64": fall_snapshot, "latest_frame_jpeg_base64": fall_snapshot}
    json.dump(payload, open(out_json, "w", encoding="utf-8"), indent=2)
    with open(os.path.splitext(out_json)[0] + ".mp4", "wb") as fh:
        import base64
        fh.write(base64.b64decode(b64))
    print("clip:", meta, "-> wrote", out_json)


if __name__ == "__main__":
    video = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "../test_payloads/fall_analysed_real_clip.json"
    template = sys.argv[3] if len(sys.argv) > 3 else "../test_payloads/fall_analysed.json"
    main(video, out, template)
