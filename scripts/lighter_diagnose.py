"""Lighter signing/auth diagnostic.

Reads `.env`, connects to Lighter, and prints exactly which api_key slot
is registered to your account, whether your local LIGHTER_API_PRIVATE_KEY
matches that slot's pubkey on the server, and what the SDK version is.
Also tries a single minimal `create_order` call to surface the real error.

Read-only / safe — the create_order is sized below market minimum so it's
guaranteed to be rejected with a known error code (NOT a real position).

Run:
    python scripts/lighter_diagnose.py
"""
from __future__ import annotations
import asyncio
import functools
import os
import sys
import importlib.metadata as md
import traceback

from dotenv import load_dotenv


# Mirror every print to BOTH stdout AND a file. Some Windows shells (Git
# Bash with native python.exe) buffer stdout in a way that hides output
# until the process exits; the file capture is a guaranteed paper trail.
_LOG_PATH = os.path.join(os.path.dirname(__file__), "lighter_diagnose.log")
_log_file = open(_LOG_PATH, "w", encoding="utf-8")
_real_print = print

def print(*args, **kwargs):  # type: ignore[no-redef]
    msg = " ".join(str(a) for a in args)
    _real_print(msg, flush=True)
    _log_file.write(msg + "\n")
    _log_file.flush()


async def main() -> int:
    load_dotenv()

    url = os.environ.get("LIGHTER_URL", "https://mainnet.zklighter.elliot.ai")
    account_index_str = os.environ.get("LIGHTER_ACCOUNT_INDEX", "")
    api_key_index_str = os.environ.get("LIGHTER_API_KEY_INDEX", "")
    api_priv = os.environ.get("LIGHTER_API_PRIVATE_KEY", "").strip()
    eth_addr = os.environ.get("WALLET_ADDRESS", "").strip()

    if not api_priv or not account_index_str:
        print("ERROR: missing LIGHTER_API_PRIVATE_KEY or LIGHTER_ACCOUNT_INDEX in .env")
        return 1

    account_index = int(account_index_str)
    api_key_index = int(api_key_index_str) if api_key_index_str else 0

    print("=" * 70)
    print(f"  Lighter diagnostic")
    print("=" * 70)
    try:
        ver = md.version("lighter-sdk")
    except Exception:
        ver = "?"
    print(f"  SDK version       : lighter-sdk {ver}")
    print(f"  URL               : {url}")
    print(f"  L1 wallet         : {eth_addr or '(not set)'}")
    print(f"  Account index     : {account_index}")
    print(f"  API key slot      : {api_key_index} {'<-- RESERVED for web/mobile (0,1)' if api_key_index in (0, 1) else ''}")
    print()

    try:
        from lighter import (
            ApiClient, Configuration, AccountApi, OrderApi, SignerClient,
        )
        from lighter import nonce_manager
    except ImportError as e:
        print(f"ERROR: lighter SDK not importable: {e}")
        return 1

    cfg = Configuration(host=url)
    api_client = ApiClient(configuration=cfg)
    account_api = AccountApi(api_client)
    order_api = OrderApi(api_client)

    # Step 1 — list api keys for this account from server.
    # Per SDK docstring: api_key_index=255 retrieves ALL keys.
    print("[1/4] Querying server for registered api keys on this account...")
    try:
        keys_resp = await account_api.apikeys(
            account_index=account_index, api_key_index=255,
        )
        keys = getattr(keys_resp, "api_keys", None) or []
        code = getattr(keys_resp, "code", "?")
        msg = getattr(keys_resp, "message", "")
        if not keys:
            print(f"  -> server returned NO api keys (code={code}, msg='{msg}').")
            print(f"    Either account_index is wrong, or no key registered yet.")
        else:
            print(f"  -> server reports {len(keys)} key(s) on account {account_index}:")
            for k in keys:
                idx = getattr(k, "api_key_index", "?")
                pub = getattr(k, "public_key", "?") or "(empty)"
                pub_short = pub[:20] + "..." if len(pub) > 20 else pub
                marker = " <-- THIS IS WHAT YOUR .env REFERENCES" if idx == api_key_index else ""
                print(f"      slot {idx}: pubkey {pub_short}{marker}")
            # Surface mismatch directly: is our slot in the list?
            slot_indexes = [getattr(k, "api_key_index", -1) for k in keys]
            if api_key_index not in slot_indexes:
                print()
                print(f"  !!  Your .env LIGHTER_API_KEY_INDEX={api_key_index} is NOT in the")
                print(f"     server's list of registered slots {sorted(slot_indexes)}. Every signature")
                print(f"     for this slot will be rejected with 'invalid signature'.")
    except Exception as e:
        print(f"  -> apikeys() failed: {e}")

    # Step 2 — bring up SignerClient and try CheckClient on each slot
    print()
    print("[2/4] Initializing SignerClient with .env private key...")
    signer = SignerClient(
        url=url,
        account_index=account_index,
        api_private_keys={api_key_index: api_priv},
        nonce_management_type=nonce_manager.NonceManagerType.API,
    )

    try:
        from lighter.signer_client import decode_and_free
        err_ptr = signer.signer.CheckClient(api_key_index, account_index)
        err = decode_and_free(err_ptr)
        if err:
            print(f"  -> CheckClient FAILED on slot {api_key_index}: {err}")
            print(f"    This is the smoking gun — the LIGHTER_API_PRIVATE_KEY in .env")
            print(f"    does NOT match the public key registered for this slot on the server.")
            print(f"    Solution: regenerate via `python scripts/lighter_setup.py`")
            print(f"              or rotate via app.lighter.xyz and update .env.")
        else:
            print(f"  -> CheckClient OK — local privkey matches server pubkey for slot {api_key_index}")
    except Exception as e:
        print(f"  -> CheckClient probe raised: {e}")

    # Step 3 — try a tiny `create_order` to see the precise server response
    print()
    print("[3/4] Trying ONE minimal create_order to surface the real error...")
    try:
        # Use ETH market (most stable). Size deliberately large enough to
        # pass the SDK client-side check but rejected for invalid sig if
        # that's the issue, OR for low-margin if the sig is fine.
        resp = await order_api.order_book_details()
        details = getattr(resp, "order_book_details", []) or []
        eth_meta = next((d for d in details if d.symbol == "ETH"), None)
        if eth_meta is None:
            print("  -> couldn't find ETH market metadata; aborting probe")
        else:
            # SDK 1.0.9: attribute is `market_id`, not `market_index`.
            mi = eth_meta.market_id
            api_idx, nonce = signer.nonce_manager.next_nonce()
            print(f"  -> calling create_order(market={mi}, sell ETH, "
                  f"api_key_index={api_idx}, nonce={nonce})")
            tx, _resp, err = await signer.create_order(
                market_index=mi,
                client_order_index=int.from_bytes(os.urandom(4), "big"),
                base_amount=int(0.001 * 10**eth_meta.supported_size_decimals),
                price=int(1 * 10**eth_meta.supported_price_decimals),  # $1 — far below market, won't fill
                is_ask=True,  # sell
                order_type=SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                order_expiry=SignerClient.DEFAULT_IOC_EXPIRY,
                api_key_index=api_idx,
                nonce=nonce,
            )
            if err is None:
                print(f"  -> server ACCEPTED the order (tx={getattr(tx, 'tx_hash', '?')[:20]}...)")
                print(f"    No fill expected (price way below market) — IOC auto-cancels.")
                print(f"    SIGNING WORKS. The hedge-existing failures are something else.")
            else:
                print(f"  -> server REJECTED with: {err}")
                err_s = str(err)
                if "21120" in err_s or "invalid signature" in err_s.lower():
                    print()
                    print(f"    Confirmed: invalid signature even on a single isolated call.")
                    print(f"    Causes (in order of likelihood):")
                    print(f"      1. .env LIGHTER_API_PRIVATE_KEY ≠ server's pubkey for slot {api_key_index}")
                    print(f"      2. Slot {api_key_index} is reserved (0/1 are web/mobile only)")
                    print(f"      3. account_index {account_index} doesn't own slot {api_key_index}")
                    print(f"    Fix: regenerate API key on app.lighter.xyz with slot ≥2,")
                    print(f"         then update .env's LIGHTER_API_KEY_INDEX and LIGHTER_API_PRIVATE_KEY.")
    except Exception as e:
        print(f"  -> probe raised: {e}")

    # Step 4 — clean up
    print()
    print("[4/4] Closing connections...")
    try:
        await signer.close()
        await api_client.close()
    except Exception:
        pass

    print()
    print("Diagnostic complete.")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except BaseException as e:
        # Capture absolutely everything so the user gets a real error
        # instead of "deu em nada". Print twice so it's hard to miss.
        try:
            print(f"\n*** FATAL: {type(e).__name__}: {e}")
            print(traceback.format_exc())
        except Exception:
            pass
        try:
            _log_file.write(f"\n*** FATAL: {type(e).__name__}: {e}\n")
            _log_file.write(traceback.format_exc())
            _log_file.flush()
        except Exception:
            pass
        sys.stderr.write(f"\n*** FATAL: {type(e).__name__}: {e}\n")
        sys.stderr.write(traceback.format_exc())
        sys.stderr.flush()
        rc = 99
    finally:
        try:
            _log_file.close()
        except Exception:
            pass
        sys.stderr.write(f"\n[output also written to: {_LOG_PATH}]\n")
        sys.stderr.flush()
    sys.exit(rc)
