"""One-time helper: register a Lighter API key from your hot eth wallet.

Run this AFTER you've created a Lighter account by depositing USDC from your
wallet via the official UI (https://app.lighter.xyz). Lighter assigns your
account a numeric `account_index` based on your eth address; this script
queries that index, generates a fresh API key pair, signs the registration
with your eth private key, and prints the values to put in `.env`.

Run:
    python scripts/lighter_setup.py

Reads from `.env`:
    WALLET_ADDRESS         — your hot wallet's eth address
    WALLET_PRIVATE_KEY     — same wallet's privkey (signs the registration tx)
    LIGHTER_URL            — defaults to mainnet.zklighter.elliot.ai

Writes to stdout the values to add to `.env`:
    LIGHTER_ACCOUNT_INDEX
    LIGHTER_API_PRIVATE_KEY
    LIGHTER_API_KEY_INDEX

The `api_private_key` is what the bot uses at runtime (NOT your eth privkey).
You can revoke and rotate API keys via app.lighter.xyz at any time.
"""
from __future__ import annotations
import asyncio
import os
import sys

from dotenv import load_dotenv


async def main() -> int:
    load_dotenv()

    eth_address = os.environ.get("WALLET_ADDRESS", "").strip()
    eth_pk = os.environ.get("WALLET_PRIVATE_KEY", "").strip()
    lighter_url = os.environ.get(
        "LIGHTER_URL", "https://mainnet.zklighter.elliot.ai",
    )
    if not eth_address or not eth_pk:
        print("ERROR: WALLET_ADDRESS and WALLET_PRIVATE_KEY must be set in .env",
              file=sys.stderr)
        return 1

    try:
        import lighter
        from lighter import (
            ApiClient, Configuration, AccountApi, SignerClient, create_api_key,
        )
    except ImportError:
        print("ERROR: pip install lighter-sdk", file=sys.stderr)
        return 1

    cfg = Configuration(host=lighter_url)
    api_client = ApiClient(configuration=cfg)
    account_api = AccountApi(api_client)

    # 1. Look up account_index by L1 (eth) address
    print(f"Looking up Lighter account for {eth_address}…")
    try:
        resp = await account_api.accounts_by_l1_address(l1_address=eth_address)
    except Exception as e:
        print(f"ERROR: accounts_by_l1_address failed: {e}", file=sys.stderr)
        await api_client.close()
        return 1

    sub_accounts = getattr(resp, "sub_accounts", None) or []
    if not sub_accounts:
        print(
            f"\nNo Lighter account found for {eth_address}.\n"
            f"You need to create one first by depositing USDC at "
            f"https://app.lighter.xyz (any amount works as a registration).\n"
            f"After the deposit confirms, re-run this script.",
            file=sys.stderr,
        )
        await api_client.close()
        return 1

    account_index = int(sub_accounts[0].index)
    print(f"  account_index = {account_index}")
    print(f"  l1_address    = {sub_accounts[0].l1_address}")

    # 2. Generate a fresh API keypair
    api_priv, api_pub, err = create_api_key()
    if err is not None:
        print(f"ERROR: create_api_key failed: {err}", file=sys.stderr)
        await api_client.close()
        return 1

    # 3. Pick an unused api_key_index (we'll just use the next slot)
    api_key_index = 1  # 0 is reserved by the SDK as default; start at 1

    # 4. Register via change_api_key signed with the eth wallet
    signer = SignerClient(
        url=lighter_url,
        account_index=account_index,
        api_private_keys={api_key_index: api_priv},
    )
    print(f"Registering API key (slot {api_key_index})…")
    tx, resp_send, err = await signer.change_api_key(
        eth_private_key=eth_pk,
        new_pubkey=api_pub,
        api_key_index=api_key_index,
    )
    if err is not None:
        print(f"ERROR: change_api_key failed: {err}", file=sys.stderr)
        await signer.close()
        await api_client.close()
        return 1
    print(f"  tx_hash = {getattr(resp_send, 'tx_hash', '?')}")

    await signer.close()
    await api_client.close()

    # 5. Print values to stdout for user to paste into .env
    print()
    print("=" * 60)
    print("  COPY THESE TO .env (do NOT share api_private_key)")
    print("=" * 60)
    print(f"LIGHTER_URL={lighter_url}")
    print(f"LIGHTER_ACCOUNT_INDEX={account_index}")
    print(f"LIGHTER_API_KEY_INDEX={api_key_index}")
    print(f"LIGHTER_API_PRIVATE_KEY={api_priv}")
    print(f"ACTIVE_EXCHANGE=lighter")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
