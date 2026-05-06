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


async def preview_operation(request: Request):
    """POST /operations/preview {usdc_budget} → returns the swap+deposit+hedge
    plan without sending any transactions. The UI shows this to the user for
    explicit confirmation before /operations/start is called."""
    if not hasattr(request.app.state, "engine"):
        return JSONResponse(
            {"error": "Engine not running (set START_ENGINE=true)"}, status_code=503,
        )
    engine = request.app.state.engine

    try:
        body = await request.json()
        usdc_budget = float(body.get("usdc_budget", 0))
    except Exception:
        return JSONResponse({"error": "Body must be {usdc_budget: number}"}, status_code=400)
    if usdc_budget <= 0:
        return JSONResponse({"error": "usdc_budget must be positive"}, status_code=400)

    try:
        # Engine routes through pair_factory when a vault is selected, so the
        # preview uses the SAME addresses the real operation would use.
        plan = await engine.preview_operation(usdc_budget=usdc_budget)
        return JSONResponse(plan, status_code=200)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception(f"preview_operation failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def start_operation(request: Request):
    if not hasattr(request.app.state, "engine"):
        return JSONResponse(
            {"error": "Engine not running (set START_ENGINE=true)"}, status_code=503,
        )
    engine = request.app.state.engine

    # Parse optional JSON body for Phase 2.0 budget + per-leg swap strategies
    usdc_budget = None
    swap_strategies = None
    try:
        body = await request.json()
        if "usdc_budget" in body:
            usdc_budget = float(body["usdc_budget"])
            if usdc_budget <= 0:
                return JSONResponse({"error": "usdc_budget must be positive"}, status_code=400)
        if "swap_strategies" in body and isinstance(body["swap_strategies"], dict):
            valid_choices = {"use_existing", "full_swap", "swap_diff"}
            raw = body["swap_strategies"]
            cleaned: dict = {}
            for leg in ("token0", "token1"):
                if leg in raw:
                    choice = str(raw[leg])
                    if choice not in valid_choices:
                        return JSONResponse(
                            {"error": f"swap_strategies.{leg} must be one of {sorted(valid_choices)}"},
                            status_code=400,
                        )
                    cleaned[leg] = choice
            if cleaned:
                swap_strategies = cleaned
    except Exception:
        pass  # No body or invalid JSON; legacy mode

    try:
        op_id = await engine.start_operation(
            usdc_budget=usdc_budget, swap_strategies=swap_strategies,
        )
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
    """Swap any residual token0 in the wallet to token1. Only allowed when
    no operation is active (otherwise teardown handles it via swap_to_usdc).
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
        return JSONResponse(await engine._lifecycle.cashout_residual(), status_code=200)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def curve_preview(request: Request):
    """GET /curve → V3 range + grid that the engine WILL post (or is
    posting) for the currently selected vault. Used by the dashboard to
    render the LP curve visually so the user can see where buy/sell
    orders trigger before/after starting an operation."""
    if not hasattr(request.app.state, "engine"):
        return JSONResponse({"error": "Engine not running"}, status_code=503)
    engine = request.app.state.engine
    try:
        return JSONResponse(await engine.compute_curve_preview(), status_code=200)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def recover_partial(request: Request):
    """Emergency recovery: withdraw any Beefy shares + swap residuals to USDC.

    For when an operation fails mid-bootstrap and leaves orphaned shares
    or wallet balances. Idempotent.
    """
    if not hasattr(request.app.state, "engine"):
        return JSONResponse({"error": "Engine not running"}, status_code=503)
    engine = request.app.state.engine
    db = request.app.state.db

    active = await db.get_active_operation()
    if active is not None:
        return JSONResponse(
            {"error": "Active operation in progress; use /operations/stop instead"},
            status_code=409,
        )

    try:
        result = await engine.recover_partial_position()
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def wallet_balance(request: Request):
    """Return wallet balances + total USD value via oracle.

    Routing mirrors preview_operation: pair-picker mode rebuilds the
    lifecycle from the selected vault, so cross-pair vaults expose all
    three balances (native USDC + token0 + token1) and the total in USD.

    Backwards-compatible fields: `usdc_balance`, `weth_balance`,
    `eth_balance` are still emitted (where `weth_balance` aliases
    `token0_balance`, regardless of whether token0 is actually WETH).
    New callers should use the explicit per-token fields.
    """
    empty = {
        "usdc_balance": 0.0, "weth_balance": 0.0, "eth_balance": 0.0,
        "token0_balance": 0.0, "token1_balance": 0.0,
        "token0_symbol": "", "token1_symbol": "",
        "token0_usd_price": 0.0, "token1_usd_price": 0.0,
        "total_usd": 0.0, "is_dual_leg": False,
    }
    if not hasattr(request.app.state, "engine"):
        return JSONResponse(empty)
    engine = request.app.state.engine
    summary = await engine.wallet_summary()
    if summary is None:
        return JSONResponse(empty)
    return JSONResponse({
        # Backwards-compat keys
        "usdc_balance": summary["usdc_balance"],
        "weth_balance": summary["token0_balance"],
        "eth_balance": summary["eth_balance"],
        # Explicit per-token + USD breakdown
        "token0_balance": summary["token0_balance"],
        "token1_balance": summary["token1_balance"],
        "token0_symbol": summary["token0_symbol"],
        "token1_symbol": summary["token1_symbol"],
        "token0_usd_price": summary["token0_usd_price"],
        "token1_usd_price": summary["token1_usd_price"],
        "total_usd": summary["total_usd"],
        "is_dual_leg": summary["is_dual_leg"],
    })


from engine import metrics as engine_metrics


async def metrics(request: Request):
    body = engine_metrics.render_metrics()
    return Response(body, media_type=engine_metrics.render_content_type())


async def list_pairs(request: Request):
    """GET /pairs - returns USD/cross-pairs from cache + selected_vault_id."""
    db = request.app.state.db
    from engine.pair_resolver import build_pair_list
    result = await build_pair_list(db=db)
    return JSONResponse(result, status_code=200)


async def select_pair(request: Request):
    """POST /pairs/select - body {vault_id}: validate + persist."""
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid or missing JSON body"}, status_code=400)
    vault_id = (body or {}).get("vault_id")
    if not vault_id or not isinstance(vault_id, str):
        return JSONResponse({"error": "vault_id required"}, status_code=400)

    pair = await db.get_pair_from_cache(vault_id)
    if pair is None:
        return JSONResponse(
            {"error": f"Vault {vault_id} not in cache. Refresh pair list first."},
            status_code=400,
        )

    decimals = (pair.get("token0_decimals"), pair.get("token1_decimals"))
    # Supported: (18, 6) USD-pairs (WETH/USDC) and (18, 18) cross-pairs (ARB/WETH)
    SUPPORTED_DECIMALS = {(18, 6), (18, 18)}
    if decimals not in SUPPORTED_DECIMALS:
        return JSONResponse(
            {"error": f"Unsupported decimals {decimals}; MVP supports {sorted(SUPPORTED_DECIMALS)}"},
            status_code=400,
        )

    # Cross-pair (is_usd_pair=False) requires token1's perp to be active
    # so the bot can hedge both legs.
    if not pair.get("is_usd_pair") and not pair.get("dydx_perp_token1"):
        return JSONResponse(
            {"error": "Cross-pair: token1 sem perp dYdX ativo"},
            status_code=400,
        )

    await db.set_selected_vault_id(vault_id)
    return JSONResponse(
        {"selected_vault_id": vault_id, "pair": pair},
        status_code=200,
    )


async def refresh_pairs(request: Request):
    """POST /pairs/refresh - re-fetch dYdX + Beefy and update caches.

    Builds an async Web3 client from settings.arbitrum_rpc_url so the Beefy
    fetcher can resolve ERC20 metadata (symbol/decimals) on-chain for any
    token addresses not yet in the DB cache.
    """
    db = request.app.state.db
    settings = request.app.state.settings
    from chains.dydx_markets import DydxMarketsFetcher
    from chains.beefy_api import BeefyApiFetcher

    w3 = None
    try:
        from web3 import AsyncWeb3, AsyncHTTPProvider
        if settings.arbitrum_rpc_url:
            w3 = AsyncWeb3(AsyncHTTPProvider(settings.arbitrum_rpc_url))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Could not init w3 for ERC20 reads ({e}); will rely on cache only"
        )

    try:
        dydx = DydxMarketsFetcher(db=db)
        n_dydx = await dydx.refresh()
        active = await dydx.get_active_tickers()

        beefy = BeefyApiFetcher(db=db, w3=w3)
        n_beefy = await beefy.refresh(active_dydx_tickers=active)

        import time
        return JSONResponse({
            "dydx_markets_count": n_dydx,
            "beefy_pairs_count": n_beefy,
            "last_refresh_ts": time.time(),
        }, status_code=200)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception(f"refresh_pairs failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
