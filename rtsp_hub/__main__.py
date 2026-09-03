from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="rtsp-hub", description="Serve RTSP streams over HTTP.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--config", type=Path, help="path to the streams JSON file")
    args = parser.parse_args()

    if args.config:
        os.environ["RTSP_HUB_CONFIG"] = str(args.config)

    import uvicorn

    uvicorn.run("rtsp_hub.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
