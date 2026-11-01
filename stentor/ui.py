# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Local web UI ("Shack") for Stentor. Put a TLS reverse proxy in front and restrict it to your LAN or an SSO;
the app itself only checks an `X-Shack: 1` header on the UI API.

/ui/                      the page
/ui/api/tool/<name>       POST, run a whitelisted server tool synchronously (reads + file edits)
/ui/api/job/<name>        POST, start a long tool (radio_read / radio_write / contacts) -> {"job": id}
/ui/api/job/<id>          GET, poll {"state": running|done|error, "result": ...}
/ui/ws/serial             WebSocket, browser Web Serial bridge (see bridge.py)
"""
import asyncio
import inspect
import time
import uuid
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute

from . import bridge
from . import server as srv

SYNC_TOOLS = {
    # read
    "wpsd_status", "bm_device_info", "wpsd_last_heard", "wpsd_config", "codeplug_list", "codeplug_get",
    "codeplug_overview", "codeplug_settings", "codeplug_diff", "codeplug_validate", "wpsd_sync_status",
    "dashboard_snapshot", "server_info", "radio_detect", "repeaters_search", "talkgroups_search", "dmr_id_lookup",
    # edit the workspace file
    "codeplug_upsert", "codeplug_delete", "codeplug_zone_set", "codeplug_add_channel", "codeplug_add_talkgroup",
    "codeplug_import_repeaters", "codeplug_copy", "codeplug_new", "codeplug_sync_wpsd", "codeplug_export_cps",
    "fm_cities", "fm_stations", "fm_list", "fm_list_set", "codeplug_import_region", "repeaters_liveness",
    "presets_list", "codeplug_add_preset",
}
JOB_TOOLS = {"radio_read", "radio_write", "radio_verify", "radio_write_callsign_db", "codeplug_import_rdt",
             "fm_radio_read", "fm_radio_write", "radio_read_blocks"}
HTML = Path(__file__).parent / "static" / "shack.html"
_jobs: dict[str, dict] = {}


def _guard(request: Request):
    if request.headers.get("x-shack") != "1" and not (request.method == "GET" and request.query_params.get("shack") == "1"):
        return JSONResponse({"error": "missing X-Shack header"}, status_code=403)
    return None


async def _run(name: str, args: dict):
    fn = getattr(srv, name)
    params = inspect.signature(fn).parameters
    args = {k: v for k, v in (args or {}).items() if k in params}
    res = fn(**args)
    if inspect.isawaitable(res):
        res = await res
    return res


async def page(request: Request):
    return HTMLResponse(HTML.read_text(), headers={"Cache-Control": "no-cache"})


async def tool(request: Request):
    if (g := _guard(request)):
        return g
    name = request.path_params["name"]
    if name not in SYNC_TOOLS:
        return JSONResponse({"error": f"tool '{name}' not available from the web UI"}, status_code=404)
    try:
        args = await request.json()
    except Exception:  # noqa: BLE001
        args = {}
    try:
        return JSONResponse(await _run(name, args))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


async def job_start(request: Request):
    if (g := _guard(request)):
        return g
    name = request.path_params["name"]
    if name not in JOB_TOOLS:
        return JSONResponse({"error": f"'{name}' is not a background job"}, status_code=404)
    try:
        args = await request.json()
    except Exception:  # noqa: BLE001
        args = {}
    # one radio job at a time
    is_radio = lambda n: n.startswith("radio_") or n.startswith("fm_radio_")  # noqa: E731
    if is_radio(name) and any(j["state"] == "running" and is_radio(j["name"]) for j in _jobs.values()):
        return JSONResponse({"error": "a radio operation is already running"}, status_code=409)
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "name": name, "state": "running", "started": time.time(), "result": None, "error": None}
    _jobs[jid] = job

    async def runner():
        try:
            job["result"] = await _run(name, args)
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001
            job["error"] = f"{type(e).__name__}: {e}"
            job["state"] = "error"
        job["finished"] = time.time()

    asyncio.get_running_loop().create_task(runner())
    # expire old jobs
    for k in [k for k, j in _jobs.items() if j.get("finished") and time.time() - j["finished"] > 3600]:
        _jobs.pop(k, None)
    return JSONResponse({"job": jid})


async def job_get(request: Request):
    if (g := _guard(request)):
        return g
    j = _jobs.get(request.path_params["jid"])
    if not j:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse({**j, "elapsed": round(time.time() - j["started"], 1)})


async def bridge_status(request: Request):
    if (g := _guard(request)):
        return g
    return JSONResponse(bridge.status())


async def bridge_trace(request: Request):
    if (g := _guard(request)):
        return g
    if request.query_params.get("events"):
        return JSONResponse(bridge.events())
    return JSONResponse(bridge.trace(int(request.query_params.get("last", "200"))))


FLAGS = Path(__import__("os").environ.get("DATA_DIR", "/data")) / "flags"


async def flag(request: Request):
    """Country flag as SVG, fetched once from flagcdn.com and cached on disk (Windows has no flag emoji)."""
    iso = request.path_params["iso"].lower()
    if not iso.isalpha() or len(iso) != 2:
        return JSONResponse({"error": "bad code"}, status_code=404)
    FLAGS.mkdir(parents=True, exist_ok=True)
    f = FLAGS / f"{iso}.svg"
    if not f.exists():
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(f"https://flagcdn.com/{iso}.svg")
            if r.status_code != 200 or b"<svg" not in r.content[:200]:
                return JSONResponse({"error": "no flag"}, status_code=404)
            f.write_bytes(r.content)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=502)
    return Response(f.read_bytes(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=2592000"})


async def export_cps(request: Request):
    """Download the AnyTone CPS CSV set of a workspace codeplug as a zip (navigation, so the guard is ?shack=1)."""
    if (g := _guard(request)):
        return g
    from . import codeplug as cp, cps
    name = request.query_params.get("codeplug", "current")
    try:
        doc = cp.load(name)
    except FileNotFoundError:
        return JSONResponse({"error": f"codeplug '{name}' not found"}, status_code=404)
    data = cps.export_zip(doc)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{cp.safe_name(name)}-anytone-cps.zip"'})


async def import_rdt(request: Request):
    """Upload an AnyTone CPS .rdt (raw body, ?name=<file>) and decode it into a workspace codeplug."""
    if (g := _guard(request)):
        return g
    from . import codeplug as cp, config
    name = Path(request.query_params.get("name", "upload.rdt")).name
    if not name.lower().endswith(".rdt"):
        return JSONResponse({"error": "expected a .rdt file"}, status_code=400)
    body = await request.body()
    if not body or len(body) > 64 * 1024 * 1024:
        return JSONResponse({"error": "empty or too large"}, status_code=400)
    dest_dir = config.CPS_CODEPLUG_DIR / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{cp.safe_name(name[:-4])}.rdt"
    dest.write_bytes(body)
    target = cp.safe_name(request.query_params.get("target") or f"cps-{name[:-4]}")
    try:
        res = await srv.codeplug_import_rdt(str(dest), target)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    res["stored"] = str(dest)
    return JSONResponse(res)


async def fm_logo(request: Request):
    """Station logo proxied from radiomap.eu and cached (ui/fm/logo/gr/images/x.gif)."""
    from . import fmradio
    got = await fmradio.logo_bytes(request.path_params["path"])
    if not got:
        return JSONResponse({"error": "no logo"}, status_code=404)
    data, mime = got
    return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=2592000"})


async def jobs_list(request: Request):
    if (g := _guard(request)):
        return g
    return JSONResponse([{k: v for k, v in j.items() if k != "result"} for j in _jobs.values()])


app = Starlette(routes=[
    Route("/", lambda r: RedirectResponse("/ui/")),
    Route("/ui", lambda r: RedirectResponse("/ui/")),
    Route("/ui/", page),
    Route("/ui/api/tool/{name}", tool, methods=["POST"]),
    Route("/ui/api/job/{name}", job_start, methods=["POST"]),
    Route("/ui/api/job/{jid}", job_get, methods=["GET"]),
    Route("/ui/api/jobs", jobs_list, methods=["GET"]),
    Route("/ui/api/bridge", bridge_status, methods=["GET"]),
    Route("/ui/api/bridge/trace", bridge_trace, methods=["GET"]),
    Route("/ui/flag/{iso}", flag, methods=["GET"]),
    Route("/ui/api/export/cps", export_cps, methods=["GET"]),
    Route("/ui/fm/logo/{path:path}", fm_logo, methods=["GET"]),
    Route("/ui/api/import/rdt", import_rdt, methods=["POST"]),
    WebSocketRoute("/ui/ws/serial", bridge.serial_ws),
])
