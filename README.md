# rtsp-hub

Select a few RTSP streams and serve them over plain HTTP from a single app.
Each stream gets its own URL; a dashboard page shows them all at once.

- `GET /` — dashboard: add/enable/disable/delete streams, live grid preview
- `GET /streams/{id}/mjpeg` — live MJPEG (`multipart/x-mixed-replace`), works in an `<img>` tag
- `GET /streams/{id}/snapshot.jpg` — single JPEG frame
- `GET /api/streams`, `POST /api/streams`, `PATCH|DELETE /api/streams/{id}` — JSON API
- `GET /healthz`

FFmpeg is spawned per stream only while a client is connected and is stopped
after 15 idle seconds; one ffmpeg process feeds all viewers of that stream and
restarts automatically if the camera drops.

## Requirements

- Python 3.10+
- `ffmpeg` on `PATH`

## Run

```bash
pip install -r requirements.txt
python -m rtsp_hub            # http://0.0.0.0:8080
```

Options: `--host`, `--port`, `--config` (defaults to `~/.config/rtsp-hub/streams.json`,
overridable with `RTSP_HUB_CONFIG`).

## Config file

```json
{
  "streams": [
    {
      "id": "front-door",
      "name": "Front door",
      "url": "rtsp://user:pass@192.168.1.20:554/Streaming/Channels/101",
      "enabled": true,
      "width": 640,
      "fps": 10,
      "quality": 5,
      "transport": "tcp"
    }
  ]
}
```

`quality` is ffmpeg's `-q:v` (2 = best, 31 = worst). `transport` is `tcp` or `udp`.
Credentials in RTSP URLs are redacted in the API, UI and logs.

URLs may reference environment variables (`$VAR` / `${VAR}`) so passwords stay out
of the config file:

```bash
cp examples/streams.8cam.json ~/.config/rtsp-hub/streams.json
RTSP_PASSWORD='your-password' python -m rtsp_hub
```

`examples/streams.8cam.json` is an 8-channel NVR (`ch0/1` … `ch7/1`) — edit the
host and channel paths to match yours.

## Notes

MJPEG is used because it plays in any browser with no plugin and no client-side
player, at the cost of bandwidth. The app has no authentication — put it behind
a reverse proxy or a trusted network.

An MJPEG response never ends, and browsers allow only ~6 connections per host, so
the dashboard previews tiles by polling `snapshot.jpg` once a second and opens a
real MJPEG connection only for tiles you put in **Live** mode (max 3 at a time).
Any tile's `mjpeg` URL can still be embedded directly elsewhere.
