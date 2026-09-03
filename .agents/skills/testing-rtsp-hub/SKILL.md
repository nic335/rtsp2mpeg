---
name: testing-rtsp-hub
description: How to run and end-to-end test the rtsp-hub dashboard (FastAPI + MJPEG) locally, including a fake RTSP camera with password auth so ${VAR} expansion in RTSP URLs can actually be proven.
---

# Testing rtsp-hub locally

## Bring up the stack

```bash
# 1. Fake RTSP server (mediamtx). Binary usually at /tmp/mediamtx, config /tmp/mediamtx.yml
cd /tmp && setsid nohup ./mediamtx > /tmp/mediamtx.log 2>&1 < /dev/null &

# 2. One publisher per fake camera (use different lavfi sources so tiles are distinguishable)
setsid nohup ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 \
  -f lavfi -i "testsrc=size=640x360:rate=15" -c:v libx264 -preset ultrafast \
  -tune zerolatency -f rtsp rtsp://127.0.0.1:8554/test > /tmp/pub1.log 2>&1 < /dev/null &
# repeat with testsrc2 -> rtsp://127.0.0.1:8554/test2

# 3. The app, with an isolated registry file and any env vars referenced by URLs
cd /home/ubuntu/rtsp-hub && \
  RTSP_HUB_CONFIG=/home/ubuntu/rtsp-hub-test.json RTSP_PASSWORD=secretpw \
  nohup python3 -m rtsp_hub --port 8080 > /tmp/hub.log 2>&1 &
```

`testsrc`/`testsrc2` contain a frame counter, so comparing two screenshots a few
seconds apart is a reliable "is it actually live?" check.

## Making `${VAR}` expansion in RTSP URLs falsifiable

By default mediamtx allows anonymous reads, so a URL with a wrong/unexpanded
password still connects and the test proves nothing. Gate reads behind a
password in `/tmp/mediamtx.yml`:

```yaml
authInternalUsers:
- user: any
  pass:
  ips: []
  permissions:
  - action: publish
    path:
- user: admin
  pass: secretpw
  ips: []
  permissions:
  - action: read
    path:
  - action: playback
    path:
# keep the localhost api/metrics entry that follows
```

Restart mediamtx and the publishers. Verify the gate first:
`ffmpeg -rtsp_transport tcp -i rtsp://admin:wrongpw@127.0.0.1:8554/test -frames:v 1 -f image2 -y /tmp/x.jpg`
must fail with `401 Unauthorized`, and `admin:secretpw` must succeed.
Then a dashboard stream with URL `rtsp://admin:${RTSP_PASSWORD}@127.0.0.1:8554/test`
rendering live video proves the variable was expanded.

## Known trap: the dashboard wedges itself (MJPEG connection leak)

`templates/index.html` polls `/api/streams` every 4s and rebuilds the whole grid
whenever the signature changes — and the signature includes `status.clients` /
`status.live`, which change constantly, so every poll recreates each tile's
`<img>` and opens a new MJPEG connection. The old connections are not reaped
promptly server-side (`worker.py` only notices a disconnect between frames, and
`mjpeg()` waits up to 30s for a frame), so the per-stream `clients` count climbs
1 -> 6 within ~30 seconds.

Symptoms once Chrome's 6-connections-per-host limit is reached:
- "Add stream" appears to do nothing (the POST is queued for tens of seconds, or
  arrives much later — you may see a stream you thought failed appear minutes later)
- Disable/Enable/Delete clicks are ignored
- F5 hangs with a spinner while the old page stays on screen
- Newly enabled tiles stay black even though `/api/streams` shows `live: true`

Workarounds while testing (this may be fixed later; check the tile's
"N client(s)" text — if it is > 1 per visible tile, the leak is present):
- Do each UI action within the first few seconds after a fresh page load
- Restart Chrome (`pkill chrome`, relaunch) to reset the connection pool instead
  of reloading the tab
- Keep only one enabled stream while exercising toggle/delete flows
- Cross-check real state with `curl -s localhost:8080/api/streams` and `tail /tmp/hub.log`
  (the log shows whether the POST/PATCH ever reached the server)
- `ss -tn | grep 8080` shows the held-open sockets

## Other notes

- Tiles can take 20-30s to paint the first frame even when the server reports
  `live: true` — do not call a black tile a failure without checking
  `/api/streams` and waiting.
- Top-level navigation to `/streams/<id>/mjpeg` by clicking the tile's "MJPEG"
  link may never commit in the same tab; open the URL in a **new tab** instead —
  there it renders fine.
- A bad URL (e.g. `rtsp://127.0.0.1:9999/nope`) shows a red dot plus the ffmpeg
  stderr ("Connection refused") on the tile, and ffmpeg is retried every 2s
  forever — expect a busy restart loop in `/tmp/hub.log`.
- Passwords are stored unexpanded in the registry JSON; `redacted_url` masks
  `//user:pass@` as `//***@` in API/UI.

## Devin Secrets Needed

None — all testing uses a local mediamtx with a locally chosen password.
