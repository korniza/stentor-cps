"""Serve the Stentor web UI over HTTP at :8000.

Put a reverse proxy with TLS in front (the browser serial bridge needs https/wss) and restrict access to your LAN
or an SSO; the app itself only checks an `X-Shack: 1` header on the UI API."""
import os

import uvicorn

from stentor.ui import app

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                log_level="info")
