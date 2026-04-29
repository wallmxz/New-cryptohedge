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
        "slippage_bps": settings.slippage_bps,
    })


async def update_settings(request: Request):
    hub = request.app.state.hub
    db = request.app.state.db
    form = await request.form()

    if "hedge_ratio" in form:
        hub.hedge_ratio = float(form["hedge_ratio"])
        await db.set_config("hedge_ratio", str(hub.hedge_ratio))
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

    # Parse optional JSON body for Phase 2.0 budget
    usdc_budget = None
    try:
        body = await request.json()
        if "usdc_budget" in body:
            usdc_budget = float(body["usdc_budget"])
            if usdc_budget <= 0:
                return JSONResponse({"error": "usdc_budget must be positive"}, status_code=400)
    except Exception:
        pass  # No body or invalid JSON; legacy mode

    try:
        op_id = await engine.start_operation(usdc_budget=usdc_budget)
        return JSONResponse({"id": op_id, "status": "active"}, status_code=201)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


async def stop_operation(request: Request):
    if not hasattr(request.app.state, "engine"):
        return JSONResponse(
            {"error": "Engine not running"}, status_code=503,
        )
    engine = request.app.state.engine

    swap_to_usdc = False
    try:
        body = await request.json()
        swap_to_usdc = bool(body.get("swap_to_usdc", False))
    except Exception:
        pass

    try:
        result = await engine.stop_operation(
            close_reason="user", swap_to_usdc=swap_to_usdc,
        )
        return JSONResponse(result, status_code=200)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


async def cashout(request: Request):
    """Manual swap WETH -> USDC. Used after teardown when user wants USDC out.

    Only operates when there's NO active operation (otherwise teardown handles it).
    """
    if not hasattr(request.app.state, "engine"):
        return JSONResponse({"error": "Engine not running"}, status_code=503)
    engine = request.app.state.engine
    db = request.app.state.db

    active = await db.get_active_operation()
    if active is not None:
        return JSONResponse(
            {"error": "Active operation in progress; use stop_operation with swap_to_usdc=true instead"},
            status_code=409,
        )
    if engine._lifecycle is None:
        return JSONResponse({"error": "Lifecycle not configured"}, status_code=503)

    try:
        bal = await engine._lifecycle._read_wallet_balance()
        if bal["weth"] <= 0:
            return JSONResponse({"weth_swapped": 0.0, "message": "No WETH in wallet"}, status_code=200)
        import time
        p_now = await engine._lifecycle._pool_reader.read_price()
        slippage = engine._lifecycle._settings.slippage_bps / 10000.0
        amount_in_raw = int(bal["weth"] * 10**engine._lifecycle._decimals0)
        min_out = int(bal["weth"] * p_now * (1 - slippage) * 10**engine._lifecycle._decimals1)
        tx_hash = await engine._lifecycle._uniswap.swap_exact_input(
            token_in=engine._lifecycle._settings.weth_token_address,
            token_out=engine._lifecycle._settings.usdc_token_address,
            fee=500,
            amount_in=amount_in_raw, amount_out_minimum=min_out,
            recipient=engine._lifecycle._uniswap.address,
            deadline=int(time.time()) + 300,
        )
        return JSONResponse({"tx_hash": tx_hash, "weth_swapped": bal["weth"]}, status_code=200)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def wallet_balance(request: Request):
    if not hasattr(request.app.state, "engine"):
        return JSONResponse({"usdc_balance": 0, "weth_balance": 0, "eth_balance": 0})
    engine = request.app.state.engine
    if engine._lifecycle is None:
        return JSONResponse({"usdc_balance": 0, "weth_balance": 0, "eth_balance": 0})
    bal = await engine._lifecycle._read_wallet_balance()
    return JSONResponse({
        "usdc_balance": bal["usdc"],
        "weth_balance": bal["weth"],
        "eth_balance": bal["eth"],
    })


from engine import metrics as engine_metrics


async def metrics(request: Request):
    body = engine_metrics.render_metrics()
    return Response(body, media_type=engine_metrics.render_content_type())
