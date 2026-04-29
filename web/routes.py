from __future__ import annotations
import asyncio
import json
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
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
        "logs_json": json.dumps(logs),
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


async def get_config(request: Request):
    settings = request.app.state.settings
    return JSONResponse({
        "arbitrum_rpc_url": settings.arbitrum_rpc_url,
        "arbitrum_rpc_fallback": settings.arbitrum_rpc_fallback,
        "clm_vault_address": settings.clm_vault_address,
        "clm_pool_address": settings.clm_pool_address,
        "wallet_address": settings.wallet_address,
        "active_exchange": settings.active_exchange,
        "symbol": settings.dydx_symbol,
        "alert_webhook_url": settings.alert_webhook_url,
        "pool_token0_symbol": settings.pool_token0_symbol,
        "pool_token1_symbol": settings.pool_token1_symbol,
        "max_open_orders": settings.max_open_orders,
        "threshold_aggressive": settings.threshold_aggressive,
        "threshold_recovery": settings.threshold_recovery,
    })


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
    if "pool_deposited_usd" in form:
        hub.pool_deposited_usd = float(form["pool_deposited_usd"])
        await db.set_config("pool_deposited_usd", str(hub.pool_deposited_usd))
    if "active_exchange" in form:
        await db.set_config("active_exchange", form["active_exchange"])
    if "symbol" in form:
        await db.set_config("symbol", form["symbol"])
    if "alert_webhook_url" in form:
        await db.set_config("alert_webhook_url", form["alert_webhook_url"])
    if "max_open_orders" in form:
        await db.set_config("max_open_orders", str(int(form["max_open_orders"])))
    if "threshold_aggressive" in form:
        await db.set_config("threshold_aggressive", str(float(form["threshold_aggressive"])))
    if "threshold_recovery" in form:
        await db.set_config("threshold_recovery", str(float(form["threshold_recovery"])))

    return HTMLResponse('<div id="settings-status">Configuracoes salvas (reinicie o engine para aplicar mudancas de exchange/symbol)</div>')


async def list_operations(request: Request):
    db = request.app.state.db
    limit = int(request.query_params.get("limit", "20"))
    rows = await db.get_operations(limit=limit)
    return JSONResponse(rows)


async def get_current_operation(request: Request):
    db = request.app.state.db
    hub = request.app.state.hub
    op = await db.get_active_operation()
    if op is None:
        return Response(status_code=204)
    return JSONResponse({
        "id": op["id"],
        "status": op["status"],
        "started_at": op["started_at"],
        "baseline": {
            "eth_price": op["baseline_eth_price"],
            "pool_value_usd": op["baseline_pool_value_usd"],
            "amount0": op["baseline_amount0"],
            "amount1": op["baseline_amount1"],
            "collateral": op["baseline_collateral"],
        },
        "accumulators": {
            "perp_fees_paid": op["perp_fees_paid"],
            "funding_paid": op["funding_paid"],
            "lp_fees_earned": op["lp_fees_earned"],
            "bootstrap_slippage": op["bootstrap_slippage"],
        },
        "current_pnl_breakdown": dict(hub.operation_pnl_breakdown),
    })


async def start_operation(request: Request):
    if not hasattr(request.app.state, "engine"):
        return JSONResponse(
            {"error": "Engine not running (set START_ENGINE=true)"}, status_code=503,
        )
    engine = request.app.state.engine
    try:
        op_id = await engine.start_operation()
        return JSONResponse({"id": op_id, "status": "active"}, status_code=201)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


async def stop_operation(request: Request):
    if not hasattr(request.app.state, "engine"):
        return JSONResponse(
            {"error": "Engine not running"}, status_code=503,
        )
    engine = request.app.state.engine
    try:
        result = await engine.stop_operation(close_reason="user")
        return JSONResponse(result, status_code=200)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


from engine import metrics as engine_metrics


async def metrics(request: Request):
    body = engine_metrics.render_metrics()
    return Response(body, media_type=engine_metrics.render_content_type())
