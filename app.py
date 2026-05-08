from __future__ import annotations
import asyncio
import os
import logging
from dotenv import load_dotenv

load_dotenv()
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.responses import JSONResponse
from config import Settings
from state import StateHub
from db import Database
from web.auth import BasicAuthMiddleware
from web.logging_config import setup_logging
from web.routes import (
    dashboard, sse_state, sse_logs, update_settings, get_config,
    list_operations, get_current_operation, start_operation, stop_operation,
    preview_operation, metrics, cashout, recover_partial, withdraw_partial,
    hedge_existing, wallet_balance, curve_preview,
    list_pairs, select_pair, refresh_pairs,
)

setup_logging()


def create_app(start_engine: bool = True) -> Starlette:
    settings = Settings.from_env()
    state = StateHub(
        hedge_ratio=settings.hedge_ratio,
    )
    db_path = os.environ.get("DB_PATH", "automoney.db")
    db = Database(db_path)

    async def _load_persisted_config():
        for key, caster, attr in [
            ("hedge_ratio", float, "hedge_ratio"),
        ]:
            raw = await db.get_config(key)
            if raw is not None:
                try:
                    setattr(state, attr, caster(raw))
                except ValueError:
                    pass

    @asynccontextmanager
    async def lifespan(app):
        if db._conn is None:
            await db.initialize()
        await _load_persisted_config()
        app.state.settings = settings
        app.state.hub = state
        app.state.db = db
        if start_engine:
            from engine import GridMakerEngine
            from engine.lifecycle import OperationLifecycle
            from chains.uniswap_executor import UniswapExecutor
            from chains.beefy_executor import BeefyExecutor
            from chains.uniswap import UniswapV3PoolReader
            from chains.beefy import BeefyClmReader
            from web3 import AsyncWeb3, AsyncHTTPProvider
            from eth_account import Account
            # Defer importing the dYdX adapter — its native deps
            # (`ed25519-blake2b`, `coincurve`, `grpcio`) lack prebuilt
            # wheels on Windows + Python 3.13, so users running
            # ACTIVE_EXCHANGE=lighter shouldn't be forced to install
            # MSVC build tools just to boot the bot.

            w3 = AsyncWeb3(AsyncHTTPProvider(settings.arbitrum_rpc_url))
            # Lazy account: only create if private key looks plausible (real 0x-prefixed
            # 32-byte hex). Placeholder values like "0x2" or "0x0...01" will still parse,
            # but lifecycle will be left None when key is too short to be real.
            account = None
            pk = settings.wallet_private_key or ""
            if pk.startswith("0x") and len(pk) >= 64:
                try:
                    account = Account.from_key(pk)
                except Exception:
                    account = None

            pool_reader = UniswapV3PoolReader(
                w3, settings.clm_pool_address, 18, 6,
            )
            # Legacy startup fallback. CLM v2 reader needs both strategy +
            # earn addresses; without pair-picker metadata we only have one.
            # Pass the same address twice — first real read will fail with a
            # clear error if a real (non-placeholder) address is configured
            # without the picker.
            beefy_reader = BeefyClmReader(
                w3, settings.clm_vault_address, settings.clm_vault_address,
                settings.wallet_address, 18, 6,
            )

            # Choose perp exchange based on settings.active_exchange.
            # "lighter" = Lighter v1 (zero-fee ZK rollup). Anything else
            # (default "dydx") = dYdX v4. The engine treats both the same.
            if settings.active_exchange == "lighter":
                from exchanges.lighter import LighterAdapter
                exchange = LighterAdapter(
                    url=settings.lighter_url,
                    account_index=settings.lighter_account_index,
                    api_private_key=settings.lighter_api_private_key,
                    api_key_index=settings.lighter_api_key_index,
                )
            else:
                from exchanges.dydx import DydxAdapter
                exchange = DydxAdapter(
                    mnemonic=settings.dydx_mnemonic,
                    wallet_address=settings.dydx_address,
                    network=settings.dydx_network,
                    subaccount=settings.dydx_subaccount,
                )
            # Best-effort: when the venue's edge is rejecting requests
            # (CloudFront WAF, captcha, regional blocks) we still want the
            # rest of the system (chain, recovery endpoint, dashboard) to
            # boot. The engine + start_operation will retry connect()
            # later when the exchange becomes reachable.
            try:
                await exchange.connect()
            except Exception as e:
                logging.getLogger(__name__).warning(
                    f"Exchange connect failed at startup ({e!r}); "
                    f"engine will run with exchange offline."
                )

            lifecycle = None
            if account is not None:
                uniswap_exec = UniswapExecutor(
                    w3=w3, account=account,
                    router_address=settings.uniswap_v3_router_address,
                )
                beefy_exec = BeefyExecutor(
                    w3=w3, account=account, strategy_address=settings.clm_vault_address,
                )
                lifecycle = OperationLifecycle(
                    settings=settings, hub=state, db=db,
                    exchange=exchange, uniswap=uniswap_exec, beefy=beefy_exec,
                    pool_reader=pool_reader, beefy_reader=beefy_reader,
                )
                try:
                    await lifecycle.resume_in_flight()
                except Exception as e:
                    logging.getLogger(__name__).exception(f"resume_in_flight failed: {e}")

            factory_kwargs = {}
            if account is not None:
                factory_kwargs["pair_factory_w3"] = w3
                factory_kwargs["pair_factory_account"] = account

            engine = GridMakerEngine(
                settings=settings, hub=state, db=db,
                exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
                lifecycle=lifecycle,
                **factory_kwargs,
            )
            await engine.start()
            app.state.engine = engine
        yield
        if start_engine and hasattr(app.state, 'engine'):
            await app.state.engine.stop()
        await db.close()

    routes = [
        Route("/health", lambda r: JSONResponse({"status": "ok"})),
        Route("/", dashboard),
        Route("/sse/state", sse_state),
        Route("/sse/logs", sse_logs),
        Route("/config", get_config),
        Route("/settings", update_settings, methods=["POST"]),
        Route("/operations", list_operations),
        Route("/operations/current", get_current_operation),
        Route("/operations/preview", preview_operation, methods=["POST"]),
        Route("/operations/start", start_operation, methods=["POST"]),
        Route("/operations/stop", stop_operation, methods=["POST"]),
        Route("/operations/cashout", cashout, methods=["POST"]),
        Route("/operations/recover", recover_partial, methods=["POST"]),
        Route("/operations/withdraw-partial", withdraw_partial, methods=["POST"]),
        Route("/operations/hedge-existing", hedge_existing, methods=["POST"]),
        Route("/curve", curve_preview, methods=["GET"]),
        Route("/pairs", list_pairs),
        Route("/pairs/select", select_pair, methods=["POST"]),
        Route("/pairs/refresh", refresh_pairs, methods=["POST"]),
        Route("/wallet", wallet_balance),
        Route("/metrics", metrics),
        Mount("/static", StaticFiles(directory="web/static"), name="static"),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    # Eagerly set state so routes work even without lifespan context (e.g. TestClient without 'with')
    app.state.settings = settings
    app.state.hub = state
    app.state.db = db
    # Eagerly initialize the DB so routes work without lifespan (e.g. TestClient without context manager)
    try:
        asyncio.get_running_loop()
        # Already in an async context; lifespan will handle init
    except RuntimeError:
        asyncio.run(db.initialize())
    app.add_middleware(
        BasicAuthMiddleware,
        username=settings.auth_user,
        password=settings.auth_pass,
        exclude=["/health", "/metrics"],
    )
    return app


app = create_app(start_engine=os.environ.get("START_ENGINE", "false").lower() == "true")
