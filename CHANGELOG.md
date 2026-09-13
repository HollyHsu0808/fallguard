# Changelog

## v2 (September 2026): n8n care workflow, video alerts, decisions in the app

Built at the n8n University Hackathon, Sydney (11–13 September 2026); winner of Best Business Use Case. The app stops alerting by itself and becomes the sensor; n8n decides and delivers.

- **Fall clip in the alert.** The app keeps the last 5 s of frames in a ring buffer; when a fall is called it keeps collecting for ~2.5 s, encodes an H.264 mp4 (ffmpeg via imageio-ffmpeg, OpenCV fallback) and puts it in the `fall_analysed` and `escalation` payloads. n8n sends it with Telegram *Send Video*; falls back to the fall frame as a photo, then to text.
- **Episode contract.** `fall_detected` → `fall_analysed` (pre-fall posture, fall-height category, head-impact risk, `fall_confidence`) → `escalation` (Level 2/3) → `episode_closed` (recovered / unresolved). `likely_false_alarm` episodes are logged and never alert anyone.
- **Report by Claude, facts only.** Grading (tier, flags, plain-language numbers) feeds a prompt that may only restate measured values: time of fall, time on the floor, head-impact *risk*, fall height. No diagnosis.
- **Decisions in the app, not in email.** Post-fall check, family-notice approval, outcome and information-sheet approval are queued in a `pending_actions` sheet; the app's Actions page shows them and posts the answer (with who and when) to the waiting execution. Emails are HTML notifications that say what happened and what to do.
- **Documents.** Transfer pack + ISBAR handover, family notice (approved), incident-report draft (SIRS classification left to a person), after-fall information sheet assembled only from approved blocks and approved by a nurse or GP before it is sent.
- **Resident lookup with a fallback** (`default` row, then first row) so a demo file name never silently drops the alert.
- Bugs fixed on the way: each-item Code nodes must return one object; *Convert to File* empties the item's json; n8n expressions reject arrow functions; Basic LLM Chain is not covered by n8n Gateway credits (use the Anthropic node).


Derived from the nine dated script versions kept during development (app_v1 to app_v7) and the author's version notes. Each entry names the failure that prompted the change.

## app.py (repository version)

Behaviour identical to v7. Model weights are loaded from `models/` with a clear error if the fine-tuned detector is missing; Telegram credentials are read from environment variables or Streamlit secrets, with the sidebar fields as a fallback. No credential is stored in the code.

## v7: live camera mode

- Added a video-source switch: upload a file or start the laptop webcam, with Start and Stop buttons.
- Camera mode measures event time as seconds elapsed since Start; file mode keeps frame-based video time. Every event also carries a wall-clock timestamp in both modes.

## v6: never stand down on a lost person

- The scene-change reset now fires only from the NORMAL or RECOVERED states. Once a fall is active, an empty frame is treated as a danger signal, not an all-clear.
- The Monitoring State card is always rendered, showing state, whether a person is detected, time on ground, body angle, box ratio and angle change, so the operator can see the state even when the pose model loses the person.

## v5: scene-change reset

- Compilation videos cut between scenes, so a fall from one clip bled into the next. After 20 consecutive processed frames (about 2.5 s) with no detection box and no pose, the state machine resets to NORMAL.

## v4: recovery needs positive evidence

- Fixed the case where a fallen person the pose model could not see was reported as recovered. Recovery now requires the pose model to see the person, an upright body angle below the recovery threshold, a low fall score, the detector agreeing, and all of this holding for 4 consecutive processed frames (about 0.5 s).

## v3.1: restore event timestamps

- Restored the video timestamp on each event and in the Event History page after it was dropped in v3.

## v3: temporal signal

- Added a third detection signal: an 8-sample buffer of body angle (about 1 s at every third frame). A rise of more than 40 degrees within the buffer counts as a fall, separating a genuine fall from slowly sitting or lying down.
- Fall trigger became detector OR pose score of at least 0.5 OR temporal signal. Angle change is shown in the dashboard.

## v2.1: baseline snapshot

- Labelled the two-signal build as the baseline without temporal detection, and recorded the video timestamp on each event for comparison runs.

## v2: detector OR pose

- The detector, fine-tuned only on the Le2i dataset, missed falls in unseen scenes while the COCO pose model still tracked body geometry. The trigger changed from detector AND pose score of at least 0.3 to detector OR pose score of at least 0.5.
- Recovery additionally requires the detector not to flag a fall.

## v1: first working version

- Streamlit dashboard with a YOLOv8s fall detector and YOLOv8n-pose keypoints, a four-metric rule-based fall score, a post-fall state machine with three escalation levels, Telegram photo alerts, Event History and Settings pages.
