"""Operation lifecycle orchestrator: bootstrap (swap+deposit+hedge) + teardown.

State machine persisted in DB via Database.update_bootstrap_state. Idempotent —
each step writes state BEFORE on-chain action; on restart, resume_in_flight
reads state and continues from the next pending step.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Any

from config import Settings
from state import StateHub
from db import Database
from engine.operation import Operation, OperationState
from engine.lp_math import compute_optimal_split
from chains.uniswap import UniswapV3PoolReader, tick_to_price
from chains.beefy import BeefyClmReader
from chains.uniswap_executor import UniswapExecutor
from chains.beefy_executor import BeefyExecutor
from exchanges.base import ExchangeAdapter

logger = logging.getLogger(__name__)

GAS_RESERVE_ETH = 0.005  # ~$15 at $3000/ETH; alert if below
DEPOSIT_MIN_SHARES_TOLERANCE = 0.99  # accept >= 99% of computed expected shares
DEFAULT_DEADLINE_SECONDS = 300  # 5 min

# Arbitrum native USDC. Used as the swap-input token in cross-pair (dual-leg)
# bootstrap, where neither token0 nor token1 is the user's holding currency.
_USDC_ADDRESS_ARBITRUM = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"

# Uniswap V3 Factory + Quoter on Arbitrum.
# Factory: looks up which fee tier has the most liquid pool for a pair.
# Quoter: simulates the swap and returns the EXACT amountIn the pool would
#   charge for a desired amountOut (accounts for fee + price impact + tick
#   geometry). We use this instead of `mid × (1 + slippage)` so the user's
#   slippage tolerance is the budget for chain-time price drift, not for the
#   pool's fee or trade impact.
_UNISWAP_V3_FACTORY_ARB = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
_UNISWAP_V3_QUOTER_ARB = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"  # QuoterV2
_QUOTER_V2_ABI = [{
    "inputs": [{
        "components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "fee", "type": "uint24"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "name": "params", "type": "tuple",
    }],
    "name": "quoteExactOutputSingle",
    "outputs": [
        {"name": "amountIn", "type": "uint256"},
        {"name": "sqrtPriceX96After", "type": "uint160"},
        {"name": "initializedTicksCrossed", "type": "uint32"},
        {"name": "gasEstimate", "type": "uint256"},
    ],
    "stateMutability": "nonpayable",
    "type": "function",
}]


async def _fetch_coinbase_spot_usd(symbol: str) -> float | None:
    """Query Coinbase public spot price for `symbol-USD` (e.g. ETH, ARB).
    Returns price as float, or None on failure. No auth, no key needed.
    Used as a fallback when the perp venue's oracle is unreachable.
    """
    import httpx
    url = f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            return float(data["data"]["amount"])
    except Exception as e:
        logger.warning(f"Coinbase spot fetch for {symbol}-USD failed: {e}")
        return None


async def _quote_exact_output(
    w3, token_in: str, token_out: str, fee: int, amount_out: int,
) -> int | None:
    """Ask Uniswap V3 QuoterV2 how much `token_in` is needed to receive
    `amount_out` of `token_out` through the pool at `fee` tier. Returns the
    integer amountIn, or None if the quote fails (pool empty/illiquid).
    Uses eth_call (no state change) since QuoterV2 is read-via-call."""
    quoter = w3.eth.contract(
        address=w3.to_checksum_address(_UNISWAP_V3_QUOTER_ARB),
        abi=_QUOTER_V2_ABI,
    )
    try:
        result = await quoter.functions.quoteExactOutputSingle({
            "tokenIn": w3.to_checksum_address(token_in),
            "tokenOut": w3.to_checksum_address(token_out),
            "amount": amount_out,
            "fee": fee,
            "sqrtPriceLimitX96": 0,
        }).call()
        # QuoterV2 returns (amountIn, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)
        return int(result[0])
    except Exception as e:
        logger.warning(f"Quoter failed: {e}")
        return None
_FEE_TIERS = [100, 500, 3000, 10000]  # 0.01%, 0.05%, 0.30%, 1.00%
_FACTORY_ABI = [{
    "inputs": [
        {"name": "tokenA", "type": "address"},
        {"name": "tokenB", "type": "address"},
        {"name": "fee", "type": "uint24"},
    ],
    "name": "getPool",
    "outputs": [{"type": "address"}],
    "stateMutability": "view",
    "type": "function",
}]
_POOL_LIQ_ABI = [{
    "inputs": [], "name": "liquidity",
    "outputs": [{"type": "uint128"}],
    "stateMutability": "view", "type": "function",
}]
_ZERO_ADDR = "0x0000000000000000000000000000000000000000"


async def _best_swap_fee_tier(w3, token_a: str, token_b: str) -> int | None:
    """Return the Uniswap V3 fee tier (100/500/3000/10000) of the most-liquid
    pool for a token pair, or None if no pool exists.

    Why: Uniswap V3 has up to 4 fee tiers per pair. Liquidity is often
    concentrated in ONE tier (varies per pair: USDC-WETH → 0.05%, USDC-ARB
    → 0.30%, etc.). The CLM's LP pool fee is fixed by the strategy, but our
    bootstrap swaps are free to use whichever tier is deepest.
    """
    factory = w3.eth.contract(
        address=w3.to_checksum_address(_UNISWAP_V3_FACTORY_ARB),
        abi=_FACTORY_ABI,
    )
    best_fee = None
    best_liq = 0
    for fee in _FEE_TIERS:
        try:
            addr = await factory.functions.getPool(
                w3.to_checksum_address(token_a),
                w3.to_checksum_address(token_b),
                fee,
            ).call()
        except Exception:
            continue
        if not addr or addr.lower() == _ZERO_ADDR:
            continue
        try:
            pool = w3.eth.contract(address=addr, abi=_POOL_LIQ_ABI)
            liq = await pool.functions.liquidity().call()
        except Exception:
            continue
        if liq > best_liq:
            best_liq = liq
            best_fee = fee
    return best_fee

# State -> next-step continuation map.
# - 'with_hash' means: if tx_hash exists, wait for receipt, then continue.
# - 'without_hash' means: re-execute the step (safe because on-chain state hasn't changed).
_BOOTSTRAP_STATES_RESUMABLE = {
    "approving",
    "swap_pending",
    "swap_confirmed",
    # Dual-leg cross-pair: single swap USDC→token0 (vault zaps internally
    # to V3 ratio). `swap_token1_pending` was a legacy state from the
    # 2-swaps model — removed.
    "swap_token0_pending",
    "swap_token0_done",
    "swaps_done",
    "deposit_pending",
    "deposit_confirmed",
    "deposit_done",
    "snapshot",
    "hedge_pending",
    "hedge_confirmed",
    "hedge_done",
    "teardown_grid_cancel",
    "teardown_short_close",
    "teardown_withdraw_pending",
    "teardown_withdraw_confirmed",
    "teardown_swap_pending",
    "teardown_swap_confirmed",
    # Dual-leg teardown states
    "teardown_close_pending",
    "teardown_close_done",
    "teardown_swap_token0_pending",
    "teardown_swap_token0_done",
    "teardown_swap_token1_pending",
    "teardown_swap_done",
}


class OperationLifecycle:
    def __init__(
        self, *,
        settings: Settings, hub: StateHub, db: Database,
        exchange: ExchangeAdapter,
        uniswap: UniswapExecutor, beefy: BeefyExecutor,
        pool_reader: UniswapV3PoolReader, beefy_reader: BeefyClmReader,
        decimals0: int = 18, decimals1: int = 6,  # WETH=18, USDC=6
    ):
        self._settings = settings
        self._hub = hub
        self._db = db
        self._exchange = exchange
        self._uniswap = uniswap
        self._beefy = beefy
        self._pool_reader = pool_reader
        self._beefy_reader = beefy_reader
        self._decimals0 = decimals0
        self._decimals1 = decimals1
        self._cloid_seq = 0

    def _next_cloid(self, base: int) -> int:
        self._cloid_seq += 1
        return (base * 1_000_000) + self._cloid_seq + (int(time.time()) & 0xFFFF)

    async def _read_wallet_balance(self) -> dict[str, float]:
        """Returns {token0, token1, eth} balances in display units."""
        token0 = self._uniswap._erc20(self._settings.token0_address)
        token1 = self._uniswap._erc20(self._settings.token1_address)
        eth_raw, token0_raw, token1_raw = await asyncio.gather(
            self._uniswap._w3.eth.get_balance(self._uniswap.address),
            token0.functions.balanceOf(self._uniswap.address).call(),
            token1.functions.balanceOf(self._uniswap.address).call(),
        )
        return {
            "token0": token0_raw / (10 ** self._decimals0),
            "token1": token1_raw / (10 ** self._decimals1),
            "eth": eth_raw / 1e18,
        }

    async def wallet_summary(self) -> dict:
        """Return wallet snapshot priced in USD using exchange oracle prices.

        Used by the start-modal so the user's "max budget" reflects the TOTAL
        spendable value (native USDC + token0 in USD + token1 in USD), not
        just the native USDC. Cross-pair vaults have a 4th balance (the
        Arbitrum-native USDC) on top of token0 / token1.

        Returns:
          {
            "usdc_balance": float,         # native USDC (1:1 USD)
            "token0_balance": float,       # display units
            "token1_balance": float,       # display units
            "token0_symbol": str,
            "token1_symbol": str,
            "token0_usd_price": float,
            "token1_usd_price": float,
            "eth_balance": float,          # native gas
            "total_usd": float,            # sum of all USD-priced legs
          }
        """
        is_dual_leg = bool(self._settings.dydx_symbol_token1)
        # Read native ETH + token0 + token1 balances. In single-leg, token1
        # IS USDC, so we don't need a separate USDC read.
        token0 = self._uniswap._erc20(self._settings.token0_address)
        token1 = self._uniswap._erc20(self._settings.token1_address)
        wallet = self._uniswap.address

        if is_dual_leg:
            usdc = self._uniswap._erc20(_USDC_ADDRESS_ARBITRUM)
            eth_raw, token0_raw, token1_raw, usdc_raw = await asyncio.gather(
                self._uniswap._w3.eth.get_balance(wallet),
                token0.functions.balanceOf(wallet).call(),
                token1.functions.balanceOf(wallet).call(),
                usdc.functions.balanceOf(wallet).call(),
            )
            usdc_balance = usdc_raw / 1e6  # USDC = 6 decimals on Arbitrum
        else:
            eth_raw, token0_raw, token1_raw = await asyncio.gather(
                self._uniswap._w3.eth.get_balance(wallet),
                token0.functions.balanceOf(wallet).call(),
                token1.functions.balanceOf(wallet).call(),
            )
            # Single-leg: token1 is USDC. Reuse the token1 balance as
            # native USDC; don't double-count.
            usdc_balance = token1_raw / (10 ** self._decimals1)

        token0_balance = token0_raw / (10 ** self._decimals0)
        token1_balance = token1_raw / (10 ** self._decimals1)
        eth_balance = eth_raw / 1e18

        # Oracle prices: dual-leg has both perp symbols; single-leg only
        # token0's symbol (token1 is USDC = $1).
        if is_dual_leg:
            symbols = [self._settings.dydx_symbol_token0, self._settings.dydx_symbol_token1]
        else:
            symbols = [self._settings.dydx_symbol]
        try:
            oracle = await self._exchange.get_oracle_prices(symbols)
        except Exception as e:
            logger.warning(f"wallet_summary: oracle price fetch failed ({e})")
            oracle = {}

        if is_dual_leg:
            t0_usd = float(oracle.get(self._settings.dydx_symbol_token0, 0.0) or 0.0)
            t1_usd = float(oracle.get(self._settings.dydx_symbol_token1, 0.0) or 0.0)
            total_usd = (
                usdc_balance
                + token0_balance * t0_usd
                + token1_balance * t1_usd
            )
        else:
            t0_usd = float(oracle.get(self._settings.dydx_symbol, 0.0) or 0.0)
            t1_usd = 1.0  # USDC
            # In single-leg, token1 IS USDC — `usdc_balance` already counts it.
            # Don't add token1_balance * 1.0 again.
            total_usd = usdc_balance + token0_balance * t0_usd

        return {
            "usdc_balance": usdc_balance,
            "token0_balance": token0_balance,
            "token1_balance": token1_balance,
            "token0_symbol": self._settings.pool_token0_symbol,
            "token1_symbol": self._settings.pool_token1_symbol,
            "token0_usd_price": t0_usd,
            "token1_usd_price": t1_usd,
            "eth_balance": eth_balance,
            "total_usd": total_usd,
            "is_dual_leg": is_dual_leg,
        }

    async def _check_gas_balance(self) -> None:
        """Soft check — log a warning if gas is below the recommended reserve,
        but proceed regardless. The user owns the wallet and accepts the risk
        of a tx running out of gas mid-flow (which would surface as a clear
        revert and operation_state=failed)."""
        bal = await self._read_wallet_balance()
        self._hub.wallet_eth_balance = bal["eth"]
        if bal["eth"] < GAS_RESERVE_ETH:
            logger.warning(
                f"Wallet gas low: {bal['eth']:.4f} ETH < recommended "
                f"{GAS_RESERVE_ETH:.4f} ETH reserve. Proceeding — operation may "
                f"fail mid-flow if gas runs out."
            )

    async def _maybe_swap_to_token(
        self, *, strategy: str, target_amount: float, wallet_balance: float,
        token_out: str, token_out_symbol: str, token_out_decimals: int,
        slippage: float,
    ) -> str | None:
        """Execute a USDC -> token_out swap according to `strategy`. Returns
        the tx_hash, or None if no swap was performed.

        Strategies:
          - "use_existing": skip the swap. Caller asserted wallet already has
            the target. We validate ≥99% of target is present and raise
            otherwise (so we don't silently under-deposit).
          - "full_swap": swap exactly `target_amount` of token_out, ignoring
            the wallet balance entirely. Any pre-existing balance stays put
            and gets folded into the deposit later.
          - "swap_diff": swap only `max(0, target - wallet)`. If the wallet
            already covers ≥99% of target, skip. Default behavior.
        """
        if strategy == "use_existing":
            if wallet_balance < target_amount * 0.99:
                raise RuntimeError(
                    f"Strategy 'use_existing' for {token_out_symbol} requires "
                    f"wallet balance >= {target_amount:.6f}, but only "
                    f"{wallet_balance:.6f} available."
                )
            logger.info(
                f"use_existing {token_out_symbol}: wallet has "
                f"{wallet_balance:.6f} >= target {target_amount:.6f}, skipping swap"
            )
            return None

        if strategy == "full_swap":
            amount_to_swap = target_amount
        elif strategy == "swap_diff":
            amount_to_swap = max(0.0, target_amount - wallet_balance)
            if amount_to_swap <= 0 or wallet_balance >= target_amount * 0.99:
                logger.info(
                    f"swap_diff {token_out_symbol}: wallet has {wallet_balance:.6f}, "
                    f"covers target {target_amount:.6f}, skipping swap"
                )
                return None
        else:
            raise RuntimeError(
                f"Unknown swap strategy '{strategy}' for {token_out_symbol}"
            )

        # Live quote to size amount_in_max accurately. Pick deepest fee tier
        # for the USDC <-> token_out pair (independent from CLM LP pool fee).
        swap_fee = await _best_swap_fee_tier(
            self._uniswap._w3, _USDC_ADDRESS_ARBITRUM, token_out,
        ) or self._settings.uniswap_v3_pool_fee
        amount_out_raw = int(amount_to_swap * 10**token_out_decimals)
        quoted_in = await _quote_exact_output(
            self._uniswap._w3, _USDC_ADDRESS_ARBITRUM,
            token_out, swap_fee, amount_out_raw,
        )
        if quoted_in is None:
            raise RuntimeError(
                f"Quoter could not price USDC→{token_out_symbol} at fee {swap_fee}"
            )
        amount_in_max = int(quoted_in * (1 + slippage))
        logger.info(
            f"USDC→{token_out_symbol} fee {swap_fee} ({swap_fee/10000:.2f}%) "
            f"strategy={strategy} target={amount_to_swap:.6f} "
            f"quoted=${quoted_in/1e6:.4f} max=${amount_in_max/1e6:.4f} "
            f"(+{slippage*100:.2f}%)"
        )
        return await self._uniswap.swap_exact_output(
            token_in=_USDC_ADDRESS_ARBITRUM,
            token_out=token_out,
            fee=swap_fee,
            amount_out=amount_out_raw,
            amount_in_maximum=amount_in_max,
            recipient=self._uniswap.address,
            deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
        )

    async def bootstrap_preview(self, *, usdc_budget: float) -> dict:
        """Compute the swap+deposit+hedge plan for `usdc_budget` WITHOUT touching
        the chain. The UI calls this first, shows the plan to the user, and only
        triggers `bootstrap()` after explicit confirmation.

        Reads pool price, Beefy range, and dYdX oracle prices to size each leg;
        no transactions are sent.
        """
        if (await self._db.get_active_operation()) is not None:
            raise RuntimeError("Operation already active")
        await self._check_gas_balance()

        is_dual_leg = bool(self._settings.dydx_symbol_token1)
        p_now = await self._pool_reader.read_price()
        beefy_pos = await self._beefy_reader.read_position()
        p_a = tick_to_price(beefy_pos.tick_lower, self._decimals0, self._decimals1)
        p_b = tick_to_price(beefy_pos.tick_upper, self._decimals0, self._decimals1)

        # Convert USDC budget to "value in token1 units" for compute_optimal_split.
        # Single-leg: token1 IS USDC, so 1 USDC == 1 token1 unit.
        # Dual-leg: token1 is volatile (e.g. ARB at $0.12), so we must divide
        # the USDC budget by token1's USD oracle price to get token1 units.
        # Without this, $700 USDC was being treated as 700 ARB (= $83 USD)
        # and the resulting swap amounts under-allocated the pool by ~88%.
        if is_dual_leg:
            token0_sym_oracle = self._settings.dydx_symbol_token0
            token1_sym_oracle = self._settings.dydx_symbol_token1
            oracle = await self._exchange.get_oracle_prices(
                [token0_sym_oracle, token1_sym_oracle],
            )
            p0_usd = float(oracle.get(token0_sym_oracle, 0.0) or 0.0)
            p1_usd = float(oracle.get(token1_sym_oracle, 0.0) or 0.0)
            if p1_usd <= 0:
                raise RuntimeError(
                    f"Could not resolve USD oracle price for token1 "
                    f"({token1_sym_oracle}); cannot size cross-pair LP."
                )
            total_value_token1_units = usdc_budget / p1_usd
        else:
            p0_usd = p_now  # token1 == USDC, p_now is already USD/token0
            p1_usd = 1.0
            total_value_token1_units = usdc_budget

        amount_t0_target, amount_t1_target = compute_optimal_split(
            p=p_now, p_a=p_a, p_b=p_b, total_value_usdc=total_value_token1_units,
        )

        slippage_bps = self._settings.slippage_bps
        slippage = slippage_bps / 10000.0
        router = self._settings.uniswap_v3_router_address
        pool_fee = self._settings.uniswap_v3_pool_fee
        token0_addr = self._settings.token0_address
        token1_addr = self._settings.token1_address
        token0_sym_label = self._settings.pool_token0_symbol
        token1_sym_label = self._settings.pool_token1_symbol

        # Read current wallet balances per token so the UI can recommend a
        # swap strategy ('use_existing' / 'full_swap' / 'swap_diff') per leg.
        # Defensive: if RPC fails, treat as empty wallet — the user just sees
        # 'full_swap' across the board (the safe default) instead of the
        # whole preview blowing up.
        try:
            wallet_bal = await self._read_wallet_balance()
        except Exception as e:
            logger.warning(f"bootstrap_preview: wallet read failed ({e}); defaulting to zero balances")
            wallet_bal = {"token0": 0.0, "token1": 0.0, "eth": 0.0}

        def recommend_strategy(have: float, target: float) -> str:
            if have >= target * 0.99 and target > 0:
                return "use_existing"
            if have <= target * 0.01 or have <= 1e-12:
                return "full_swap"
            return "swap_diff"

        plan: dict = {
            "usdc_budget": usdc_budget,
            "is_dual_leg": is_dual_leg,
            "pool": {
                "p_now": p_now,
                "p_a": p_a,
                "p_b": p_b,
                "in_range": p_a < p_now < p_b,
                "address": self._settings.clm_pool_address,
                "fee_param": pool_fee,
                "fee_pct": pool_fee / 1_000_000.0,
                "token0_symbol": token0_sym_label,
                "token1_symbol": token1_sym_label,
            },
            "router": router,
            "slippage_bps": slippage_bps,
            "swaps": [],
            "wallet": {
                "token0_balance": wallet_bal["token0"],
                "token1_balance": wallet_bal["token1"],
                "eth_balance": wallet_bal["eth"],
            },
            "deposit": {
                "vault": self._settings.clm_vault_address,
                "amount0_target": amount_t0_target,
                "amount1_target": amount_t1_target,
            },
            "hedge": [],
        }

        if is_dual_leg:
            # Cross-pair single-swap model: ONE swap USDC → token0 for the
            # full budget. Beefy CLM v2 consumes only amount0 and zaps to
            # the V3 ratio internally. If the wallet has pre-existing
            # token1, we offer to consolidate it → token0 first.
            token0_sym = self._settings.dydx_symbol_token0
            token1_sym = self._settings.dydx_symbol_token1
            total_in_token0 = usdc_budget / p0_usd if p0_usd > 0 else 0.0
            usdc_for_t0 = total_in_token0 * p0_usd
            plan["swaps"] = [
                {
                    "leg": "token0",
                    "router": router,
                    "token_in_symbol": "USDC",
                    "token_in_address": _USDC_ADDRESS_ARBITRUM,
                    "token_out_symbol": token0_sym_label,
                    "token_out_address": token0_addr,
                    "amount_in_max_usdc": usdc_for_t0 * (1 + slippage),
                    "amount_out": total_in_token0,
                    "fee_param": pool_fee,
                },
            ]
            plan["strategies"] = {
                "token0": recommend_strategy(wallet_bal["token0"], total_in_token0),
                # token1 default = "keep" (don't touch). User opts in to
                # "consolidate" only when they explicitly want existing
                # token1 to be swapped into token0 and consumed.
                "token1": "keep" if wallet_bal["token1"] > 0 else None,
            }
            plan["hedge"] = [
                {
                    "symbol": token0_sym, "side": "sell",
                    "size": amount_t0_target * self._hub.hedge_ratio,
                    "ref_price_usd": p0_usd,
                    "notional_usd": amount_t0_target * self._hub.hedge_ratio * p0_usd,
                },
                {
                    "symbol": token1_sym, "side": "sell",
                    "size": amount_t1_target * self._hub.hedge_ratio,
                    "ref_price_usd": p1_usd,
                    "notional_usd": amount_t1_target * self._hub.hedge_ratio * p1_usd,
                },
            ]
        else:
            # Single-leg: token1 IS USDC. Only one swap (USDC->token0).
            usdc_to_swap = max(0.0, usdc_budget - amount_t1_target)
            plan["swaps"] = [
                {
                    "leg": "token0",
                    "router": router,
                    "token_in_symbol": token1_sym_label,
                    "token_in_address": token1_addr,
                    "token_out_symbol": token0_sym_label,
                    "token_out_address": token0_addr,
                    "amount_in_max_usdc": usdc_to_swap * (1 + slippage),
                    "amount_out": amount_t0_target,
                    "fee_param": pool_fee,
                },
            ]
            plan["hedge"] = [
                {
                    "symbol": self._settings.dydx_symbol_token0, "side": "sell",
                    "size": amount_t0_target * self._hub.hedge_ratio,
                    "ref_price_usd": p_now,
                    "notional_usd": amount_t0_target * self._hub.hedge_ratio * p_now,
                },
            ]
            plan["strategies"] = {
                "token0": recommend_strategy(wallet_bal["token0"], amount_t0_target),
            }
        return plan

    async def bootstrap(
        self, *, usdc_budget: float, swap_strategies: dict | None = None,
    ) -> int:
        """Execute swap -> deposit -> snapshot -> hedge. Returns operation_id.

        Dispatches on `settings.dydx_symbol_token1`:
          - empty (single-leg): token1 is a stable; existing path with one swap
            and one short.
          - non-empty (dual-leg / cross-pair): user holds USDC; performs two
            sequential swaps (USDC->token0, USDC->token1) and opens two perp
            shorts in parallel via asyncio.gather.

        `swap_strategies`: optional per-leg user choice from the preview UI.
        Dict shape `{"token0": "use_existing"|"full_swap"|"swap_diff",
                     "token1": "use_existing"|"full_swap"|"swap_diff"}`. When
        omitted, dual-leg falls back to legacy "swap_diff" behavior (skip if
        wallet already has ≥99% of the target). Single-leg path ignores the
        argument — its swap sizing is already deterministic from the budget.
        """
        if bool(self._settings.dydx_symbol_token1):
            return await self._bootstrap_dual_leg(
                usdc_budget=usdc_budget, swap_strategies=swap_strategies,
            )

        existing = await self._db.get_active_operation()
        if existing is not None:
            raise RuntimeError(f"Operation {existing['id']} already active")
        await self._check_gas_balance()

        p_now = await self._pool_reader.read_price()
        beefy_pos = await self._beefy_reader.read_position()
        p_a = tick_to_price(beefy_pos.tick_lower, self._decimals0, self._decimals1)
        p_b = tick_to_price(beefy_pos.tick_upper, self._decimals0, self._decimals1)

        amount_weth_target, amount_usdc_target = compute_optimal_split(
            p=p_now, p_a=p_a, p_b=p_b, total_value_usdc=usdc_budget,
        )
        logger.info(
            f"Bootstrap budget=${usdc_budget:.2f} p={p_now:.2f} range=[{p_a:.2f},{p_b:.2f}] "
            f"-> WETH={amount_weth_target:.6f}, USDC={amount_usdc_target:.2f}"
        )

        op_id = await self._db.insert_operation(
            started_at=time.time(),
            status=OperationState.STARTING.value,
            baseline_eth_price=p_now,
            baseline_pool_value_usd=usdc_budget,
            baseline_amount0=amount_weth_target,
            baseline_amount1=amount_usdc_target,
            baseline_collateral=self._hub.dydx_collateral,
            usdc_budget=usdc_budget,
        )
        self._hub.current_operation_id = op_id
        self._hub.operation_state = OperationState.STARTING.value

        try:
            await self._db.update_bootstrap_state(op_id, "approving")
            self._hub.bootstrap_progress = "Approving tokens..."
            await self._uniswap.ensure_approval(
                token_address=self._settings.token1_address,
                amount=2**256 - 1,
                spender=self._settings.uniswap_v3_router_address,
            )
            await self._beefy.ensure_approval(
                token_address=self._settings.token1_address, amount=2**256 - 1,
            )
            await self._beefy.ensure_approval(
                token_address=self._settings.token0_address, amount=2**256 - 1,
            )

            if amount_weth_target > 0 and amount_usdc_target < usdc_budget:
                await self._db.update_bootstrap_state(op_id, "swap_pending")
                self._hub.bootstrap_progress = "Swapping USDC -> WETH..."
                slippage = self._settings.slippage_bps / 10000.0
                amount_in_max = int(
                    (usdc_budget - amount_usdc_target) * (1 + slippage) * 10**self._decimals1
                )
                amount_out_raw = int(amount_weth_target * 10**self._decimals0)
                deadline = int(time.time()) + DEFAULT_DEADLINE_SECONDS
                tx = await self._uniswap.swap_exact_output(
                    token_in=self._settings.token1_address,
                    token_out=self._settings.token0_address,
                    fee=self._settings.uniswap_v3_pool_fee,
                    amount_out=amount_out_raw,
                    amount_in_maximum=amount_in_max,
                    recipient=self._uniswap.address,
                    deadline=deadline,
                )
                await self._db.update_bootstrap_state(op_id, "swap_confirmed", swap_tx_hash=tx)
            else:
                await self._db.update_bootstrap_state(op_id, "swap_confirmed")

            # Use the wallet's actual post-swap balance, not the pre-swap
            # target — slippage/rounding mean the swap may net more or less
            # than amount_weth_target, and Beefy mints shares on what we send.
            await self._db.update_bootstrap_state(op_id, "deposit_pending")
            self._hub.bootstrap_progress = "Depositing in Beefy..."
            bal = await self._read_wallet_balance()
            amount0_raw = int(bal["token0"] * 10**self._decimals0)
            amount1_raw = int(bal["token1"] * 10**self._decimals1)
            tx = await self._beefy.deposit(
                amount0=amount0_raw, amount1=amount1_raw, min_shares=0,
            )
            await self._db.update_bootstrap_state(op_id, "deposit_confirmed", deposit_tx_hash=tx)

            # Snapshot the real baseline AFTER the deposit settles. The
            # Beefy strategy may rebalance ranges or skim dust on deposit,
            # so the post-deposit position is the only authoritative starting
            # point for the operation's PnL accounting.
            await self._db.update_bootstrap_state(op_id, "snapshot")
            self._hub.bootstrap_progress = "Snapshotting baseline..."
            beefy_pos_after = await self._beefy_reader.read_position()
            my_amount0 = beefy_pos_after.amount0 * beefy_pos_after.share
            my_amount1 = beefy_pos_after.amount1 * beefy_pos_after.share
            real_pool_value = my_amount0 * p_now + my_amount1
            await self._db.update_baseline_amounts(
                op_id,
                amount0=my_amount0, amount1=my_amount1,
                pool_value_usd=real_pool_value,
            )

            await self._db.update_bootstrap_state(op_id, "hedge_pending")
            self._hub.bootstrap_progress = (
                f"Opening short on {self._settings.active_exchange or 'exchange'}..."
            )
            target_short = my_amount0 * self._hub.hedge_ratio
            if target_short > 0:
                await self._exchange.place_long_term_order(
                    symbol=self._settings.dydx_symbol,
                    side="sell", size=target_short,
                    price=p_now * 0.999,  # taker
                    cloid_int=self._next_cloid(998),
                    ttl_seconds=60,
                )
                slippage_usd = 0.0005 * target_short * p_now
                await self._db.add_to_operation_accumulator(
                    op_id, "bootstrap_slippage", slippage_usd,
                )
            await self._db.update_bootstrap_state(op_id, "hedge_confirmed")

            await self._db.update_bootstrap_state(op_id, "active")
            await self._db.update_operation_status(op_id, OperationState.ACTIVE.value)
            self._hub.operation_state = OperationState.ACTIVE.value
            self._hub.bootstrap_progress = ""
            logger.info(f"Operation {op_id} bootstrapped and ACTIVE")
            return op_id

        except Exception as e:
            logger.exception(f"Bootstrap failed at op_id={op_id}: {e}")
            await self._db.update_bootstrap_state(op_id, "failed")
            await self._db.update_operation_status(op_id, OperationState.FAILED.value)
            self._hub.operation_state = OperationState.FAILED.value
            self._hub.bootstrap_progress = f"FAILED: {e}"
            raise

    async def _bootstrap_dual_leg(
        self, *, usdc_budget: float, swap_strategies: dict | None = None,
    ) -> int:
        """Cross-pair bootstrap: USDC -> token0 swap (single, full budget),
        deposit into Beefy (vault zaps internally to V3 ratio), snapshot
        baseline, open BOTH perp shorts in parallel.

        Differs from single-leg in three ways:
        1. ONE Uniswap swap (USDC -> token0). The Beefy CLM v2 vault is a
           single-sided depositor: it accepts only `amount0` and rebalances
           to the V3 range internally (swapping token0 for token1 inside
           the strategy as needed). Sending `amount1 > 0` is ignored — and
           burning gas + slippage to acquire ARB pre-deposit just to have
           it sit in our wallet is wasteful.
        2. Baseline pool value computed using both tokens' USD oracle prices
           (token1 is no longer assumed to be 1.0 USD/unit).
        3. Two `place_long_term_order` calls dispatched in parallel via
           asyncio.gather, one per perp symbol — sized from `read_position`
           AFTER deposit (since the vault decides the actual t0/t1 split).

        `swap_strategies`: per-leg explicit choice from the user. In the
        new single-swap model only the `token0` key matters:
            - "use_existing": skip the swap; use whatever token0 is in the
              wallet (validates wallet balance >= 99% of full budget in t0).
            - "full_swap": always swap the full budget, regardless of
              wallet balance (pre-existing token0 stays and gets folded
              into the deposit too).
            - "swap_diff": swap only the gap (target - wallet_balance);
              if wallet already covers ≥99%, skip.
        Defaults to "swap_diff" when `swap_strategies` is None.
        """
        strategies = swap_strategies or {}
        strat_t0 = strategies.get("token0", "swap_diff")
        # token1 strategy in single-swap dual-leg model:
        #   "consolidate" → swap ALL wallet token1 → token0 before deposit.
        #     User opts in explicitly when they want to use existing
        #     token1 as capital (consumes it entirely).
        #   "keep" (DEFAULT) → don't touch wallet token1. Caller's budget
        #     determines what enters the LP via the USDC swap. Anything
        #     already in token1 stays put.
        strat_t1 = strategies.get("token1", "keep")
        existing = await self._db.get_active_operation()
        if existing is not None:
            raise RuntimeError(f"Operation {existing['id']} already active")
        await self._check_gas_balance()

        p_now = await self._pool_reader.read_price()
        beefy_pos = await self._beefy_reader.read_position()
        p_a = tick_to_price(beefy_pos.tick_lower, self._decimals0, self._decimals1)
        p_b = tick_to_price(beefy_pos.tick_upper, self._decimals0, self._decimals1)

        # Need oracle prices BEFORE the split: in cross-pair, token1 is volatile
        # so the USDC budget must be converted to token1 units before passing
        # to compute_optimal_split (whose `total_value_usdc` arg is actually
        # "total value in token1 units" — fine for USD pairs where token1=USDC,
        # but not here).
        token0_sym = self._settings.dydx_symbol_token0
        token1_sym = self._settings.dydx_symbol_token1
        oracle_prices = await self._exchange.get_oracle_prices([token0_sym, token1_sym])
        baseline_t0_usd = float(oracle_prices.get(token0_sym, 0.0) or 0.0)
        baseline_t1_usd = float(oracle_prices.get(token1_sym, 0.0) or 0.0)
        if baseline_t1_usd <= 0:
            raise RuntimeError(
                f"Could not resolve USD oracle price for token1 "
                f"({token1_sym}); cannot size cross-pair LP."
            )

        total_value_token1_units = usdc_budget / baseline_t1_usd
        amount_t0_target, amount_t1_target = compute_optimal_split(
            p=p_now, p_a=p_a, p_b=p_b, total_value_usdc=total_value_token1_units,
        )
        logger.info(
            f"Dual-leg bootstrap budget=${usdc_budget:.2f} "
            f"oracle[{token0_sym}]={baseline_t0_usd} oracle[{token1_sym}]={baseline_t1_usd} "
            f"-> t0={amount_t0_target:.6f}, t1={amount_t1_target:.6f}"
        )

        op_id = await self._db.insert_operation(
            started_at=time.time(),
            status=OperationState.STARTING.value,
            baseline_eth_price=p_now,
            baseline_pool_value_usd=usdc_budget,
            baseline_amount0=amount_t0_target,
            baseline_amount1=amount_t1_target,
            baseline_collateral=self._hub.dydx_collateral,
            usdc_budget=usdc_budget,
        )
        # Persist per-leg baseline oracle prices (cols added in Task 2).
        await self._db._conn.execute(
            "UPDATE operations SET baseline_token0_usd_price = ?, "
            "baseline_token1_usd_price = ? WHERE id = ?",
            (baseline_t0_usd, baseline_t1_usd, op_id),
        )
        await self._db._conn.commit()

        self._hub.current_operation_id = op_id
        self._hub.operation_state = OperationState.STARTING.value

        try:
            await self._db.update_bootstrap_state(op_id, "approving")
            self._hub.bootstrap_progress = "Approving tokens (dual-leg)..."
            # USDC must be approved for the router (we spend USDC for the
            # single token0 swap).
            await self._uniswap.ensure_approval(
                token_address=_USDC_ADDRESS_ARBITRUM,
                amount=2**256 - 1,
                spender=self._settings.uniswap_v3_router_address,
            )
            # token0 needs approval for the Beefy earn vault — that's
            # what gets transferred in by deposit(). The vault zaps token0
            # → token1 internally to the V3 ratio, so we don't approve or
            # send token1.
            await self._beefy.ensure_approval(
                token_address=self._settings.token0_address, amount=2**256 - 1,
            )

            slippage = self._settings.slippage_bps / 10000.0

            # Cross-pair single-swap model:
            # 1. Optionally consolidate any existing wallet token1 into
            #    token0 first (so prior residuals join the LP capital
            #    instead of staying stranded).
            # 2. ONE swap USDC → token0 covering whatever's still missing
            #    to hit the full budget expressed in token0 units.
            # 3. Deposit amount0 only; vault zaps internally.

            # ---- Step 1: consolidate token1 → token0 (optional) ----
            current_bal = await self._read_wallet_balance()
            if strat_t1 == "consolidate" and current_bal["token1"] > 0:
                token1_erc = self._uniswap._erc20(self._settings.token1_address)
                token1_raw = await token1_erc.functions.balanceOf(
                    self._uniswap.address,
                ).call()
                if token1_raw > 0:
                    self._hub.bootstrap_progress = (
                        f"Consolidando {self._settings.pool_token1_symbol} -> "
                        f"{self._settings.pool_token0_symbol}..."
                    )
                    await self._uniswap.ensure_approval(
                        token_address=self._settings.token1_address,
                        amount=2**256 - 1,
                        spender=self._settings.uniswap_v3_router_address,
                    )
                    fee_tier = await _best_swap_fee_tier(
                        self._uniswap._w3,
                        self._settings.token1_address,
                        self._settings.token0_address,
                    ) or self._settings.uniswap_v3_pool_fee
                    logger.info(
                        f"Consolidating {token1_raw} raw "
                        f"({token1_raw/10**self._decimals1:.6f}) "
                        f"{self._settings.pool_token1_symbol} -> "
                        f"{self._settings.pool_token0_symbol} at fee {fee_tier}"
                    )
                    await self._uniswap.swap_exact_input(
                        token_in=self._settings.token1_address,
                        token_out=self._settings.token0_address,
                        fee=fee_tier,
                        amount_in=token1_raw,
                        amount_out_minimum=0,
                        recipient=self._uniswap.address,
                        deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
                    )

            # ---- Step 2: single swap USDC → token0 ----
            # Budget expressed in token0 units = total USD / token0 USD price.
            await self._db.update_bootstrap_state(op_id, "swap_token0_pending")
            self._hub.bootstrap_progress = (
                f"Swapping USDC -> {self._settings.pool_token0_symbol}..."
            )
            total_in_token0 = (
                usdc_budget / baseline_t0_usd if baseline_t0_usd > 0 else 0.0
            )
            current_bal = await self._read_wallet_balance()
            tx0 = await self._maybe_swap_to_token(
                strategy=strat_t0,
                target_amount=total_in_token0,
                wallet_balance=current_bal["token0"],
                token_out=self._settings.token0_address,
                token_out_symbol=self._settings.pool_token0_symbol,
                token_out_decimals=self._decimals0,
                slippage=slippage,
            )
            await self._db.update_bootstrap_state(
                op_id, "swaps_done", swap_tx_hash=tx0,
            )

            # ---- Step 3: deposit token0 only ----
            # Beefy CLM v2 vaults consume only amount0 and zap to V3 ratio
            # internally. amount1=0 by design.
            # Use RAW uint256 from balanceOf (never `float × 10**dec` which
            # rounds up and triggers transferFrom STF).
            await self._db.update_bootstrap_state(op_id, "deposit_pending")
            self._hub.bootstrap_progress = "Depositing in Beefy..."
            token0_erc = self._uniswap._erc20(self._settings.token0_address)
            amount0_raw = await token0_erc.functions.balanceOf(
                self._uniswap.address,
            ).call()
            tx_dep = await self._beefy.deposit(
                amount0=amount0_raw, amount1=0, min_shares=0,
            )
            await self._db.update_bootstrap_state(
                op_id, "deposit_done", deposit_tx_hash=tx_dep,
            )

            # ---- Snapshot post-deposit baseline ----
            await self._db.update_bootstrap_state(op_id, "snapshot")
            self._hub.bootstrap_progress = "Snapshotting baseline..."
            beefy_pos_after = await self._beefy_reader.read_position()
            my_amount0 = beefy_pos_after.amount0 * beefy_pos_after.share
            my_amount1 = beefy_pos_after.amount1 * beefy_pos_after.share
            real_pool_value = my_amount0 * baseline_t0_usd + my_amount1 * baseline_t1_usd
            await self._db.update_baseline_amounts(
                op_id,
                amount0=my_amount0, amount1=my_amount1,
                pool_value_usd=real_pool_value,
            )

            # ---- Hedge: open BOTH perp shorts in parallel ----
            await self._db.update_bootstrap_state(op_id, "hedge_pending")
            self._hub.bootstrap_progress = (
                f"Opening shorts on {self._settings.active_exchange or 'exchange'} (dual-leg)..."
            )
            target_short_t0 = my_amount0 * self._hub.hedge_ratio
            target_short_t1 = my_amount1 * self._hub.hedge_ratio

            async def _open_short_t0() -> None:
                if target_short_t0 <= 0 or baseline_t0_usd <= 0:
                    return
                await self._exchange.place_long_term_order(
                    symbol=token0_sym,
                    side="sell", size=target_short_t0,
                    price=baseline_t0_usd * 0.999,  # taker
                    cloid_int=self._next_cloid(998),
                    ttl_seconds=60,
                )
                slippage_usd = 0.0005 * target_short_t0 * baseline_t0_usd
                await self._db.add_to_operation_accumulator(
                    op_id, "perp_fees_paid_token0", slippage_usd,
                )

            async def _open_short_t1() -> None:
                if target_short_t1 <= 0 or baseline_t1_usd <= 0:
                    return
                await self._exchange.place_long_term_order(
                    symbol=token1_sym,
                    side="sell", size=target_short_t1,
                    price=baseline_t1_usd * 0.999,  # taker
                    cloid_int=self._next_cloid(996),
                    ttl_seconds=60,
                )
                slippage_usd = 0.0005 * target_short_t1 * baseline_t1_usd
                await self._db.add_to_operation_accumulator(
                    op_id, "perp_fees_paid_token1", slippage_usd,
                )

            await asyncio.gather(_open_short_t0(), _open_short_t1())
            await self._db.update_bootstrap_state(op_id, "hedge_done")

            await self._db.update_bootstrap_state(op_id, "active")
            await self._db.update_operation_status(op_id, OperationState.ACTIVE.value)
            self._hub.operation_state = OperationState.ACTIVE.value
            self._hub.bootstrap_progress = ""
            logger.info(f"Operation {op_id} bootstrapped (dual-leg) and ACTIVE")
            return op_id

        except Exception as e:
            logger.exception(f"Dual-leg bootstrap failed at op_id={op_id}: {e}")
            await self._db.update_bootstrap_state(op_id, "failed")
            await self._db.update_operation_status(op_id, OperationState.FAILED.value)
            self._hub.operation_state = OperationState.FAILED.value
            self._hub.bootstrap_progress = f"FAILED: {e}"
            raise

    async def teardown(self, *, swap_to_usdc: bool = False, close_reason: str = "user") -> dict:
        """Cancel grid -> close short -> withdraw Beefy -> (optional) swap WETH to USDC.

        Returns final PnL breakdown dict.

        Dispatches on `settings.dydx_symbol_token1`:
          - empty (single-leg): existing path with one short close + optional WETH->USDC swap.
          - non-empty (dual-leg / cross-pair): closes BOTH shorts in parallel via
            asyncio.gather; optional swap_to_usdc does TWO sequential swaps
            (token0 -> USDC, token1 -> USDC).
        """
        op_row = await self._db.get_active_operation()
        if op_row is None:
            raise RuntimeError("No active operation to teardown")
        op_id = op_row["id"]
        is_dual_leg = bool(self._settings.dydx_symbol_token1)

        if is_dual_leg:
            return await self._teardown_dual_leg(
                op_id=op_id, swap_to_usdc=swap_to_usdc, close_reason=close_reason,
            )

        await self._db.update_operation_status(op_id, OperationState.STOPPING.value)
        self._hub.operation_state = OperationState.STOPPING.value
        self._hub.bootstrap_progress = "Cancelling grid..."

        try:
            await self._db.update_bootstrap_state(op_id, "teardown_grid_cancel")
            active_orders = await self._db.get_active_grid_orders()
            if active_orders:
                await self._exchange.batch_cancel([
                    dict(symbol=self._settings.dydx_symbol, cloid_int=int(r["cloid"]))
                    for r in active_orders
                ])
                for r in active_orders:
                    await self._db.mark_grid_order_cancelled(r["cloid"], time.time())

            await self._db.update_bootstrap_state(op_id, "teardown_short_close")
            self._hub.bootstrap_progress = "Closing short..."
            pos = await self._exchange.get_position(self._settings.dydx_symbol)
            p_now = await self._pool_reader.read_price()
            if pos and pos.size > 0:
                side = "buy" if pos.side == "short" else "sell"
                price = p_now * 1.001 if side == "buy" else p_now * 0.999
                await self._exchange.place_long_term_order(
                    symbol=self._settings.dydx_symbol,
                    side=side, size=pos.size, price=price,
                    cloid_int=self._next_cloid(997), ttl_seconds=60,
                )
                slippage = 0.0005 * pos.size * p_now
                await self._db.add_to_operation_accumulator(op_id, "perp_fees_paid", slippage)

            await self._db.update_bootstrap_state(op_id, "teardown_withdraw_pending")
            self._hub.bootstrap_progress = "Withdrawing Beefy..."
            beefy_pos = await self._beefy_reader.read_position()
            shares = beefy_pos.raw_balance
            if shares > 0:
                tx = await self._beefy.withdraw(shares=shares, min_amount0=0, min_amount1=0)
                await self._db.update_bootstrap_state(
                    op_id, "teardown_withdraw_confirmed", withdraw_tx_hash=tx,
                )
            else:
                await self._db.update_bootstrap_state(op_id, "teardown_withdraw_confirmed")

            if swap_to_usdc:
                await self._db.update_bootstrap_state(op_id, "teardown_swap_pending")
                self._hub.bootstrap_progress = "Swapping WETH -> USDC..."
                bal = await self._read_wallet_balance()
                if bal["token0"] > 0:
                    amount_in_raw = int(bal["token0"] * 10**self._decimals0)
                    p_now = await self._pool_reader.read_price()
                    slippage = self._settings.slippage_bps / 10000.0
                    min_out = int(bal["token0"] * p_now * (1 - slippage) * 10**self._decimals1)
                    tx = await self._uniswap.swap_exact_input(
                        token_in=self._settings.token0_address,
                        token_out=self._settings.token1_address,
                        fee=self._settings.uniswap_v3_pool_fee,
                        amount_in=amount_in_raw,
                        amount_out_minimum=min_out,
                        recipient=self._uniswap.address,
                        deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
                    )
                    await self._db.update_bootstrap_state(
                        op_id, "teardown_swap_confirmed", teardown_swap_tx_hash=tx,
                    )
                else:
                    await self._db.update_bootstrap_state(op_id, "teardown_swap_confirmed")

            op = Operation.from_db_row(await self._db.get_operation(op_id))
            from engine.pnl import compute_operation_pnl
            my_amount0 = beefy_pos.amount0 * beefy_pos.share
            my_amount1 = beefy_pos.amount1 * beefy_pos.share
            pool_value = my_amount0 * p_now + my_amount1
            breakdown = compute_operation_pnl(
                op,
                current_pool_value_usd=pool_value,
                current_eth_price=p_now,
                hedge_realized_since_baseline=self._hub.hedge_realized_pnl,
                hedge_unrealized_since_baseline=self._hub.hedge_unrealized_pnl,
            )

            await self._db.close_operation(
                op_id, ended_at=time.time(),
                final_net_pnl=breakdown["net_pnl"], close_reason=close_reason,
            )
            await self._db.update_bootstrap_state(op_id, "closed")
            self._hub.current_operation_id = None
            self._hub.operation_state = OperationState.NONE.value
            self._hub.bootstrap_progress = ""
            self._hub.operation_pnl_breakdown = {}
            return {"id": op_id, "final_net_pnl": breakdown["net_pnl"], "breakdown": breakdown}

        except Exception as e:
            logger.exception(f"Teardown failed at op_id={op_id}: {e}")
            await self._db.update_bootstrap_state(op_id, "failed")
            await self._db.update_operation_status(op_id, OperationState.FAILED.value)
            self._hub.operation_state = OperationState.FAILED.value
            self._hub.bootstrap_progress = f"TEARDOWN FAILED: {e}"
            raise

    async def _teardown_dual_leg(
        self, *, op_id: int, swap_to_usdc: bool, close_reason: str,
    ) -> dict:
        """Cross-pair teardown: cancel grid, close BOTH shorts in parallel via
        asyncio.gather, withdraw Beefy, optionally do TWO sequential swaps
        (token0 -> USDC, token1 -> USDC), and compute cross-pair PnL using
        both legs' oracle prices.
        """
        await self._db.update_operation_status(op_id, OperationState.STOPPING.value)
        self._hub.operation_state = OperationState.STOPPING.value
        self._hub.bootstrap_progress = "Cancelling grid (dual-leg)..."

        try:
            # Defensive: cancel any active grid orders. A future engine
            # implementation may post grid orders even in cross-pair mode;
            # here we keep the same shape as single-leg.
            await self._db.update_bootstrap_state(op_id, "teardown_grid_cancel")
            active_orders = await self._db.get_active_grid_orders()
            if active_orders:
                await self._exchange.batch_cancel([
                    dict(symbol=self._settings.dydx_symbol_token0, cloid_int=int(r["cloid"]))
                    for r in active_orders
                ])
                for r in active_orders:
                    await self._db.mark_grid_order_cancelled(r["cloid"], time.time())

            token0_sym = self._settings.dydx_symbol_token0
            token1_sym = self._settings.dydx_symbol_token1

            await self._db.update_bootstrap_state(op_id, "teardown_close_pending")
            self._hub.bootstrap_progress = "Closing shorts (dual-leg)..."

            oracle_prices = await self._exchange.get_oracle_prices(
                [token0_sym, token1_sym]
            )

            async def _close_leg(sym: str, accumulator_field: str) -> None:
                pos = await self._exchange.get_position(sym)
                if not pos or pos.size <= 0:
                    return
                ref = float(oracle_prices.get(sym, 0.0) or 0.0)
                if ref <= 0:
                    return
                side = "buy" if pos.side == "short" else "sell"
                price = ref * 1.001 if side == "buy" else ref * 0.999
                await self._exchange.place_long_term_order(
                    symbol=sym, side=side, size=pos.size, price=price,
                    cloid_int=self._next_cloid(997), ttl_seconds=60,
                )
                slippage_usd = 0.0005 * pos.size * ref
                await self._db.add_to_operation_accumulator(
                    op_id, accumulator_field, slippage_usd,
                )

            await asyncio.gather(
                _close_leg(token0_sym, "perp_fees_paid_token0"),
                _close_leg(token1_sym, "perp_fees_paid_token1"),
            )
            await self._db.update_bootstrap_state(op_id, "teardown_close_done")

            # Withdraw Beefy
            await self._db.update_bootstrap_state(op_id, "teardown_withdraw_pending")
            self._hub.bootstrap_progress = "Withdrawing Beefy..."
            beefy_pos = await self._beefy_reader.read_position()
            shares = beefy_pos.raw_balance
            if shares > 0:
                tx = await self._beefy.withdraw(
                    shares=shares, min_amount0=0, min_amount1=0,
                )
                await self._db.update_bootstrap_state(
                    op_id, "teardown_withdraw_confirmed", withdraw_tx_hash=tx,
                )
            else:
                await self._db.update_bootstrap_state(op_id, "teardown_withdraw_confirmed")

            # Optional: two sequential swaps back to USDC.
            if swap_to_usdc:
                await self._swap_residuals_to_usdc(op_id, is_dual_leg=True)

            # Compute final PnL via cross-pair signature.
            op = Operation.from_db_row(await self._db.get_operation(op_id))
            from engine.pnl import compute_operation_pnl
            my_amount0 = beefy_pos.amount0 * beefy_pos.share
            my_amount1 = beefy_pos.amount1 * beefy_pos.share
            p0_now = float(oracle_prices.get(token0_sym, 0.0) or 0.0)
            p1_now = float(oracle_prices.get(token1_sym, 0.0) or 0.0)
            pool_value = my_amount0 * p0_now + my_amount1 * p1_now
            breakdown = compute_operation_pnl(
                op,
                current_pool_value_usd=pool_value,
                current_token0_usd_price=p0_now,
                current_token1_usd_price=p1_now,
                hedge_realized_per_symbol=dict(self._hub.hedge_realized_pnls),
                hedge_unrealized_per_symbol=dict(self._hub.hedge_unrealized_pnls),
            )

            await self._db.close_operation(
                op_id, ended_at=time.time(),
                final_net_pnl=breakdown["net_pnl"], close_reason=close_reason,
            )
            await self._db.update_bootstrap_state(op_id, "closed")
            self._hub.current_operation_id = None
            self._hub.operation_state = OperationState.NONE.value
            self._hub.bootstrap_progress = ""
            self._hub.operation_pnl_breakdown = {}
            return {
                "id": op_id,
                "final_net_pnl": breakdown["net_pnl"],
                "breakdown": breakdown,
            }

        except Exception as e:
            logger.exception(f"Dual-leg teardown failed at op_id={op_id}: {e}")
            await self._db.update_bootstrap_state(op_id, "failed")
            await self._db.update_operation_status(op_id, OperationState.FAILED.value)
            self._hub.operation_state = OperationState.FAILED.value
            self._hub.bootstrap_progress = f"TEARDOWN FAILED: {e}"
            raise

    async def _swap_residuals_to_usdc(
        self, op_id: int, *, is_dual_leg: bool,
    ) -> None:
        """Swap residual token0 (and token1 in dual-leg) back to USDC, sequencial.

        Single-leg path: token0 -> token1 (token1 is USDC stable).
        Dual-leg path: token0 -> USDC, then token1 -> USDC. Uses min_out=0
        in dual-leg (no slippage check; accept the hit on residual cleanup).
        """
        bal = await self._read_wallet_balance()
        deadline = int(time.time()) + DEFAULT_DEADLINE_SECONDS
        slippage = self._settings.slippage_bps / 10000.0
        p_now = await self._pool_reader.read_price()

        if bal["token0"] > 0:
            await self._db.update_bootstrap_state(op_id, "teardown_swap_token0_pending")
            self._hub.bootstrap_progress = "Swapping token0 -> USDC..."
            amount_in = int(bal["token0"] * 10**self._decimals0)
            if is_dual_leg:
                token_out = _USDC_ADDRESS_ARBITRUM
                min_out = 0
            else:
                token_out = self._settings.token1_address
                min_out = int(
                    bal["token0"] * p_now * (1 - slippage) * 10**self._decimals1
                )
            tx = await self._uniswap.swap_exact_input(
                token_in=self._settings.token0_address,
                token_out=token_out,
                fee=self._settings.uniswap_v3_pool_fee,
                amount_in=amount_in,
                amount_out_minimum=min_out,
                recipient=self._uniswap.address,
                deadline=deadline,
            )
            await self._db.update_bootstrap_state(
                op_id, "teardown_swap_token0_done", teardown_swap_tx_hash=tx,
            )

        if is_dual_leg and bal["token1"] > 0:
            await self._db.update_bootstrap_state(op_id, "teardown_swap_token1_pending")
            self._hub.bootstrap_progress = "Swapping token1 -> USDC..."
            amount_in = int(bal["token1"] * 10**self._decimals1)
            tx = await self._uniswap.swap_exact_input(
                token_in=self._settings.token1_address,
                token_out=_USDC_ADDRESS_ARBITRUM,
                fee=self._settings.uniswap_v3_pool_fee,
                amount_in=amount_in,
                amount_out_minimum=0,
                recipient=self._uniswap.address,
                deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
            )
            await self._db.update_bootstrap_state(
                op_id, "teardown_swap_done", teardown_swap_tx_hash=tx,
            )

    async def recover_partial_position(
        self, *, swap_to_usdc: bool = False,
    ) -> dict:
        """Emergency recovery: withdraw any Beefy shares; optionally swap
        residuals to USDC.

        Use case: an operation failed mid-bootstrap (e.g. hedge open
        failed after deposit succeeded). The user is left with shares in
        the vault and/or residual tokens in the wallet, with no active
        operation tracking it.

        With `swap_to_usdc=False` (DEFAULT, what you usually want):
          1. Withdraw any shares — wallet gets back token0 + token1.
          2. STOP. Tokens stay in the wallet so the next start_operation
             can use them via use_existing/swap_diff without paying for
             a USDC round-trip.

        With `swap_to_usdc=True` (only when actually exiting):
          3. Swap residual token0 → USDC (or → token1 in single-leg).
          4. Swap residual token1 → USDC (dual-leg only).

        Returns a summary dict with tx hashes and amounts recovered.
        Idempotent: safe to call multiple times.
        """
        is_dual_leg = bool(self._settings.dydx_symbol_token1)
        result: dict = {
            "withdraw_tx": None,
            "swap_token0_tx": None,
            "swap_token1_tx": None,
            "before": {}, "after": {},
        }
        # Snapshot before
        bal_before = await self._read_wallet_balance()
        beefy_before = await self._beefy_reader.read_position()
        result["before"] = {
            "shares": beefy_before.raw_balance,
            "token0_balance": bal_before["token0"],
            "token1_balance": bal_before["token1"],
        }

        # Step 1: withdraw any shares
        if beefy_before.raw_balance > 0:
            logger.info(
                f"Recovery: withdrawing {beefy_before.raw_balance} shares from earn vault"
            )
            self._hub.bootstrap_progress = "Recuperando: withdraw Beefy..."
            tx = await self._beefy.withdraw(
                shares=beefy_before.raw_balance, min_amount0=0, min_amount1=0,
            )
            result["withdraw_tx"] = tx
        else:
            logger.info("Recovery: no Beefy shares to withdraw")

        # Steps 2/3 (swap residuals to USDC) only run when the caller
        # explicitly opts in. Default keeps token0/token1 in the wallet so
        # the next start_operation can reuse them via use_existing /
        # swap_diff without paying for a USDC round-trip.
        if not swap_to_usdc:
            bal_after = await self._read_wallet_balance()
            beefy_after = await self._beefy_reader.read_position()
            result["after"] = {
                "shares": beefy_after.raw_balance,
                "token0_balance": bal_after["token0"],
                "token1_balance": bal_after["token1"],
            }
            self._hub.bootstrap_progress = ""
            return result

        # Below: swap_to_usdc=True path.
        # IMPORTANT: in bootstrap we only approve USDC→router (the input
        # to bootstrap swaps), so token0/token1 are NOT yet approved for
        # the router. Recovery flips direction (token0/1 → USDC) and so
        # needs fresh approvals. Without these, the swap reverts with
        # `STF` (SafeTransferFrom failed) inside the SwapRouter.
        #
        # Use RAW balances (uint256 from balanceOf) — never `float ×
        # 10**decimals`, which rounds up and tries to spend more than the
        # wallet has, causing STF.
        deadline = int(time.time()) + DEFAULT_DEADLINE_SECONDS
        router = self._settings.uniswap_v3_router_address
        token0_erc = self._uniswap._erc20(self._settings.token0_address)
        token1_erc = self._uniswap._erc20(self._settings.token1_address)
        token0_raw = await token0_erc.functions.balanceOf(self._uniswap.address).call()
        token1_raw = await token1_erc.functions.balanceOf(self._uniswap.address).call()

        if token0_raw > 0:
            logger.info(
                f"Recovery: swapping {token0_raw} raw "
                f"({token0_raw/10**self._decimals0:.6f}) "
                f"{self._settings.pool_token0_symbol} → USDC"
            )
            self._hub.bootstrap_progress = "Recuperando: token0 → USDC..."
            await self._uniswap.ensure_approval(
                token_address=self._settings.token0_address,
                amount=2**256 - 1, spender=router,
            )
            token_out = _USDC_ADDRESS_ARBITRUM if is_dual_leg else self._settings.token1_address
            fee_tier = await _best_swap_fee_tier(
                self._uniswap._w3, self._settings.token0_address, token_out,
            ) or self._settings.uniswap_v3_pool_fee
            tx0 = await self._uniswap.swap_exact_input(
                token_in=self._settings.token0_address,
                token_out=token_out,
                fee=fee_tier,
                amount_in=token0_raw,
                amount_out_minimum=0,  # accept any — recovery, not entry
                recipient=self._uniswap.address,
                deadline=deadline,
            )
            result["swap_token0_tx"] = tx0

        if is_dual_leg and token1_raw > 0:
            logger.info(
                f"Recovery: swapping {token1_raw} raw "
                f"({token1_raw/10**self._decimals1:.6f}) "
                f"{self._settings.pool_token1_symbol} → USDC"
            )
            self._hub.bootstrap_progress = "Recuperando: token1 → USDC..."
            await self._uniswap.ensure_approval(
                token_address=self._settings.token1_address,
                amount=2**256 - 1, spender=router,
            )
            fee_tier = await _best_swap_fee_tier(
                self._uniswap._w3, self._settings.token1_address, _USDC_ADDRESS_ARBITRUM,
            ) or self._settings.uniswap_v3_pool_fee
            tx1 = await self._uniswap.swap_exact_input(
                token_in=self._settings.token1_address,
                token_out=_USDC_ADDRESS_ARBITRUM,
                fee=fee_tier,
                amount_in=token1_raw,
                amount_out_minimum=0,
                recipient=self._uniswap.address,
                deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
            )
            result["swap_token1_tx"] = tx1

        # Snapshot after
        bal_after = await self._read_wallet_balance()
        beefy_after = await self._beefy_reader.read_position()
        result["after"] = {
            "shares": beefy_after.raw_balance,
            "token0_balance": bal_after["token0"],
            "token1_balance": bal_after["token1"],
        }
        self._hub.bootstrap_progress = ""
        return result

    async def open_shorts_for_existing_position(self) -> dict:
        """Open the hedge shorts against the position currently sitting in
        the Beefy vault — without going through the full bootstrap.

        Use case: a previous bootstrap failed AFTER the deposit (e.g. the
        exchange API was unreachable so the shorts never opened). The LP
        position is healthy but unhedged. This method:
          1. Reads the current Beefy position (token0/token1 amounts).
          2. Inserts a new ACTIVE operation row with baselines equal to
             the current position so the engine main loop tracks it.
          3. Opens the 2 perp shorts (or 1 in single-leg) sized against
             the actual position.

        Errors out if the wallet has zero shares (nothing to hedge).
        """
        is_dual_leg = bool(self._settings.dydx_symbol_token1)

        if (await self._db.get_active_operation()) is not None:
            raise RuntimeError(
                "Operation already active — engine is already managing a hedge."
            )

        beefy_pos = await self._beefy_reader.read_position()
        if beefy_pos.raw_balance <= 0:
            raise RuntimeError(
                "Beefy wallet has zero shares. Nothing to hedge."
            )
        my_amount0 = beefy_pos.amount0 * beefy_pos.share
        my_amount1 = beefy_pos.amount1 * beefy_pos.share

        p_now = await self._pool_reader.read_price()

        # Resolve oracle prices (USD) for sizing the shorts and snapping
        # the operation baseline. Fallback chain (in priority):
        #   1. Exchange oracle (Lighter)
        #   2. Coinbase public spot API (no rate limit, no auth) for ETH-USD
        #      and derive ARB-USD via the pool ratio.
        if is_dual_leg:
            t0_sym = self._settings.dydx_symbol_token0
            t1_sym = self._settings.dydx_symbol_token1
            try:
                oracle = await self._exchange.get_oracle_prices([t0_sym, t1_sym])
            except Exception:
                oracle = {}
            p0_usd = float(oracle.get(t0_sym, 0.0) or 0.0)
            p1_usd = float(oracle.get(t1_sym, 0.0) or 0.0)
            if p0_usd <= 0 or p1_usd <= 0:
                # Fallback: Coinbase ETH-USD, derive token1 via pool ratio
                # (assumes token0 = ETH-like, token1 = ALT). Single-token
                # pairs can extend by querying their own coinbase ticker.
                fb_eth = await _fetch_coinbase_spot_usd("ETH")
                if fb_eth and fb_eth > 0:
                    if p0_usd <= 0:
                        p0_usd = fb_eth
                    if p1_usd <= 0 and p_now > 0:
                        # p_now is token1 per token0 (e.g. ARB per WETH).
                        # token1_usd = token0_usd / (token1 per token0)
                        p1_usd = p0_usd / p_now
            if p0_usd <= 0 or p1_usd <= 0:
                raise RuntimeError(
                    "Oracle prices unavailable from Lighter and Coinbase. "
                    "Try again in a minute."
                )
            pool_value_usd = my_amount0 * p0_usd + my_amount1 * p1_usd
        else:
            t0_sym = self._settings.dydx_symbol
            p0_usd = p_now
            p1_usd = 1.0
            pool_value_usd = my_amount0 * p_now + my_amount1

        # Insert as STARTING — promote to ACTIVE only after the shorts
        # confirm. Otherwise the engine main loop sees an ACTIVE op
        # without a hedge in place and may double-fire correction takers.
        op_id = await self._db.insert_operation(
            started_at=time.time(),
            status=OperationState.STARTING.value,
            baseline_eth_price=p_now,
            baseline_pool_value_usd=pool_value_usd,
            baseline_amount0=my_amount0,
            baseline_amount1=my_amount1,
            baseline_collateral=self._hub.dydx_collateral,
            usdc_budget=pool_value_usd,
        )
        if is_dual_leg:
            await self._db._conn.execute(
                "UPDATE operations SET baseline_token0_usd_price = ?, "
                "baseline_token1_usd_price = ? WHERE id = ?",
                (p0_usd, p1_usd, op_id),
            )
            await self._db._conn.commit()
        await self._db.update_bootstrap_state(op_id, "hedge_pending")
        self._hub.current_operation_id = op_id
        self._hub.operation_state = OperationState.STARTING.value

        # Open the shorts.
        target_short_t0 = my_amount0 * (self._hub.hedge_ratio or 1.0)
        target_short_t1 = my_amount1 * (self._hub.hedge_ratio or 1.0)
        result: dict = {
            "operation_id": op_id,
            "shorts": [],
            "position": {
                "token0": my_amount0, "token1": my_amount1,
                "pool_value_usd": pool_value_usd,
            },
        }

        async def _open_short(symbol: str, size: float, ref_usd: float, accumulator: str):
            if size <= 0 or ref_usd <= 0:
                return None
            self._hub.bootstrap_progress = (
                f"Opening short {symbol} on "
                f"{self._settings.active_exchange or 'exchange'}..."
            )
            await self._exchange.place_long_term_order(
                symbol=symbol, side="sell", size=size,
                price=ref_usd * 0.999,  # taker
                cloid_int=self._next_cloid(998),
                ttl_seconds=60,
            )
            slippage_usd = 0.0005 * size * ref_usd
            await self._db.add_to_operation_accumulator(
                op_id, accumulator, slippage_usd,
            )
            return {"symbol": symbol, "size": size, "ref_price_usd": ref_usd}

        try:
            if is_dual_leg:
                short_t0_task = _open_short(
                    t0_sym, target_short_t0, p0_usd, "perp_fees_paid_token0",
                )
                short_t1_task = _open_short(
                    t1_sym, target_short_t1, p1_usd, "perp_fees_paid_token1",
                )
                short_t0, short_t1 = await asyncio.gather(short_t0_task, short_t1_task)
                if short_t0:
                    result["shorts"].append(short_t0)
                if short_t1:
                    result["shorts"].append(short_t1)
            else:
                short = await _open_short(
                    t0_sym, target_short_t0, p_now, "bootstrap_slippage",
                )
                if short:
                    result["shorts"].append(short)
        except Exception as e:
            # Shorts failed → mark op as FAILED so the engine main loop
            # doesn't try to manage a half-built operation.
            logger.exception(f"open_shorts_for_existing_position: short open failed: {e}")
            await self._db.update_operation_status(op_id, OperationState.FAILED.value)
            await self._db.update_bootstrap_state(op_id, "failed")
            self._hub.operation_state = OperationState.FAILED.value
            self._hub.bootstrap_progress = f"Short open failed: {e}"
            self._hub.current_operation_id = None
            raise

        # Promote to ACTIVE only after shorts confirmed.
        await self._db.update_operation_status(op_id, OperationState.ACTIVE.value)
        await self._db.update_bootstrap_state(op_id, "active")
        self._hub.operation_state = OperationState.ACTIVE.value
        self._hub.bootstrap_progress = ""
        logger.info(
            f"open_shorts_for_existing_position: op_id={op_id}, "
            f"shorts={len(result['shorts'])}/{2 if is_dual_leg else 1}"
        )
        return result

    async def withdraw_partial(
        self, *, usd_amount: float | None = None,
        fraction: float | None = None,
    ) -> dict:
        """Withdraw a partial slice of the Beefy position back to the wallet.

        Pass EITHER `usd_amount` (we convert via oracle) OR `fraction`
        (0.0 to 1.0, direct share fraction). `fraction` is preferred when
        oracle prices are unreliable (e.g. exchange API blocked) — the
        user specifies the slice directly.

        Uses min_amount0/1=0 — no slippage protection on the residuals.
        Returns a summary with tx hash + shares burned.
        """
        if (usd_amount is None and fraction is None) or (
            usd_amount is not None and fraction is not None
        ):
            raise ValueError("pass exactly one of `usd_amount` or `fraction`")
        if fraction is not None and not (0 < fraction <= 1):
            raise ValueError("fraction must be in (0, 1]")
        if usd_amount is not None and usd_amount <= 0:
            raise ValueError("usd_amount must be positive")

        is_dual_leg = bool(self._settings.dydx_symbol_token1)
        beefy_pos = await self._beefy_reader.read_position()
        my_amount0 = beefy_pos.amount0 * beefy_pos.share
        my_amount1 = beefy_pos.amount1 * beefy_pos.share
        if beefy_pos.raw_balance <= 0:
            raise RuntimeError("No Beefy position to withdraw from")

        # Compute fraction if user passed usd_amount.
        p0_usd = 0.0
        p1_usd = 0.0
        position_usd = 0.0
        if fraction is None:
            if is_dual_leg:
                symbols = [
                    self._settings.dydx_symbol_token0,
                    self._settings.dydx_symbol_token1,
                ]
                try:
                    oracle = await self._exchange.get_oracle_prices(symbols)
                except Exception:
                    oracle = {}
                p0_usd = float(oracle.get(symbols[0], 0.0) or 0.0)
                p1_usd = float(oracle.get(symbols[1], 0.0) or 0.0)
                position_usd = my_amount0 * p0_usd + my_amount1 * p1_usd
            else:
                p_now = await self._pool_reader.read_price()
                p0_usd = p_now
                p1_usd = 1.0
                position_usd = my_amount0 * p_now + my_amount1
            if position_usd <= 0:
                raise RuntimeError(
                    "Cannot price position in USD (oracle unavailable). "
                    "Use `fraction` parameter instead."
                )
            if usd_amount > position_usd:
                raise ValueError(
                    f"usd_amount {usd_amount} > current position {position_usd:.2f}"
                )
            fraction = usd_amount / position_usd

        shares_to_burn = int(beefy_pos.raw_balance * fraction)
        if shares_to_burn <= 0:
            raise RuntimeError("computed zero shares to burn")

        usd_label = (
            f"${usd_amount:.2f}" if usd_amount is not None
            else f"{fraction*100:.1f}%"
        )
        logger.info(
            f"withdraw_partial: target={usd_label} → fraction {fraction*100:.2f}% → "
            f"{shares_to_burn} shares (of {beefy_pos.raw_balance})"
        )
        self._hub.bootstrap_progress = (
            f"Sacando {usd_label} da Beefy ({fraction*100:.1f}%)..."
        )
        tx = await self._beefy.withdraw(
            shares=shares_to_burn, min_amount0=0, min_amount1=0,
        )

        # Snapshot after to report what we got back.
        beefy_after = await self._beefy_reader.read_position()
        my_amount0_after = beefy_after.amount0 * beefy_after.share
        my_amount1_after = beefy_after.amount1 * beefy_after.share
        delta_t0 = my_amount0 - my_amount0_after
        delta_t1 = my_amount1 - my_amount1_after
        delta_usd = delta_t0 * p0_usd + delta_t1 * p1_usd
        self._hub.bootstrap_progress = ""
        return {
            "tx_hash": tx,
            "shares_burned": shares_to_burn,
            "fraction": fraction,
            "withdrew": {
                "token0": delta_t0,
                "token1": delta_t1,
                "usd": delta_usd,
            },
            "remaining": {
                "shares": beefy_after.raw_balance,
                "token0": my_amount0_after,
                "token1": my_amount1_after,
                "usd": (
                    my_amount0_after * p0_usd + my_amount1_after * p1_usd
                ),
            },
        }

    async def cashout_residual(self) -> dict:
        """Swap any residual token0 in the wallet to token1.

        Used when an operation is closed but token0 (e.g. WETH) is still
        sitting in the wallet — typically because teardown ran with
        swap_to_usdc=False. Returns {tx_hash, swapped_amount} or
        {swapped_amount: 0, message: "..."} if there's nothing to swap.
        """
        bal = await self._read_wallet_balance()
        if bal["token0"] <= 0:
            return {"swapped_amount": 0.0, "message": "No token0 balance to swap"}

        p_now = await self._pool_reader.read_price()
        slippage = self._settings.slippage_bps / 10000.0
        amount_in_raw = int(bal["token0"] * 10 ** self._decimals0)
        min_out = int(
            bal["token0"] * p_now * (1 - slippage) * 10 ** self._decimals1
        )
        tx_hash = await self._uniswap.swap_exact_input(
            token_in=self._settings.token0_address,
            token_out=self._settings.token1_address,
            fee=self._settings.uniswap_v3_pool_fee,
            amount_in=amount_in_raw,
            amount_out_minimum=min_out,
            recipient=self._uniswap.address,
            deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
        )
        return {"tx_hash": tx_hash, "swapped_amount": bal["token0"]}

    async def resume_in_flight(self) -> None:
        """Called at startup. For each in-flight operation:
        1. If state has a tx_hash and it's '_pending', wait for receipt then advance.
        2. If state has no tx_hash or is past confirmation, re-execute next step.
        3. If state is unknown/corrupted, mark failed.
        """
        in_flight = await self._db.get_in_flight_operations()
        if not in_flight:
            return
        for op_row in in_flight:
            op_id = op_row["id"]
            state = op_row.get("bootstrap_state")
            logger.info(f"Resuming operation {op_id} from state '{state}'")

            if state not in _BOOTSTRAP_STATES_RESUMABLE:
                logger.error(f"Operation {op_id} has unknown bootstrap_state '{state}' -- marking failed")
                await self._db.update_bootstrap_state(op_id, "failed")
                await self._db.update_operation_status(op_id, OperationState.FAILED.value)
                continue

            try:
                # Wait for any pending tx receipts first
                if state == "swap_pending" and op_row.get("bootstrap_swap_tx_hash"):
                    await self._uniswap.wait_for_receipt(op_row["bootstrap_swap_tx_hash"])
                    await self._db.update_bootstrap_state(op_id, "swap_confirmed")
                elif state == "deposit_pending" and op_row.get("bootstrap_deposit_tx_hash"):
                    await self._beefy.wait_for_receipt(op_row["bootstrap_deposit_tx_hash"])
                    await self._db.update_bootstrap_state(op_id, "deposit_confirmed")
                elif state == "teardown_withdraw_pending" and op_row.get("teardown_withdraw_tx_hash"):
                    await self._beefy.wait_for_receipt(op_row["teardown_withdraw_tx_hash"])
                    await self._db.update_bootstrap_state(op_id, "teardown_withdraw_confirmed")
                elif state == "teardown_swap_pending" and op_row.get("teardown_swap_tx_hash"):
                    await self._uniswap.wait_for_receipt(op_row["teardown_swap_tx_hash"])
                    await self._db.update_bootstrap_state(op_id, "teardown_swap_confirmed")

                # Continue from current state
                if state.startswith("teardown_"):
                    await self._continue_teardown(op_id, state, op_row)
                else:
                    await self._continue_bootstrap(op_id, state, op_row)
            except Exception as e:
                logger.exception(f"Resume failed for op {op_id}: {e}")
                await self._db.update_bootstrap_state(op_id, "failed")
                await self._db.update_operation_status(op_id, OperationState.FAILED.value)

    async def _continue_bootstrap(self, op_id: int, current_state: str, op_row: dict) -> None:
        """Re-enter bootstrap from `current_state`. MVP: mark failed and surface to operator."""
        logger.warning(
            f"Operation {op_id}: resume from bootstrap state '{current_state}' "
            f"requires manual review. Marking 'failed' to prevent automatic retry."
        )
        await self._db.update_bootstrap_state(op_id, "failed")
        await self._db.update_operation_status(op_id, OperationState.FAILED.value)

    async def _continue_teardown(self, op_id: int, current_state: str, op_row: dict) -> None:
        """Re-enter teardown from `current_state`. MVP: mark failed and surface to operator."""
        logger.warning(
            f"Operation {op_id}: resume from teardown state '{current_state}' "
            f"requires manual review. Marking 'failed' to prevent automatic retry."
        )
        await self._db.update_bootstrap_state(op_id, "failed")
        await self._db.update_operation_status(op_id, OperationState.FAILED.value)
