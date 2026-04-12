from __future__ import annotations
import asyncio
import json
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

templates = Jinja2Templates(directory="web/templates")


async def dashboard(request: Request):
    hub = request.app.state.hub
    db = request.app.state.db
    stats = await db.get_fill_stats()
    snapshots = await db.get_pool_snapshots(limit=5000)
    logs = await db.get_order_logs(limit=50)

    return templates.TemplateResponse(request, "dashboard.html", {
        "hub": hub,
        "stats": stats,
        "snapshots_json": json.dumps(snapshots),
        "logs": logs,
    })


async def sse_state(request: Request):
    hub = request.app.state.hub

    async def event_generator():
        last_update = 0.0
        while True:
            if hub.last_update > last_update:
                last_update = hub.last_update
                data = hub.to_dict()
                yield {"event": "state-update", "data": json.dumps(data)}
            await asyncio.sleep(0.2)

    return EventSourceResponse(event_generator())


async def sse_logs(request: Request):
    db = request.app.state.db

    async def event_generator():
        last_id = 0
        while True:
            logs = await db.get_order_logs(limit=5)
            for log in reversed(logs):
                if log["id"] > last_id:
                    last_id = log["id"]
                    yield {"event": "new-log", "data": json.dumps(log)}
            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


async def update_settings(request: Request):
    hub = request.app.state.hub
    db = request.app.state.db
    form = await request.form()

    if "hedge_ratio" in form:
        hub.hedge_ratio = float(form["hedge_ratio"])
        await db.set_config("hedge_ratio", str(hub.hedge_ratio))
    if "max_exposure_pct" in form:
        hub.max_exposure_pct = float(form["max_exposure_pct"])
        await db.set_config("max_exposure_pct", str(hub.max_exposure_pct))
    if "repost_depth" in form:
        hub.repost_depth = int(form["repost_depth"])
        await db.set_config("repost_depth", str(hub.repost_depth))

    return HTMLResponse('<div id="settings-status">Settings saved</div>')
