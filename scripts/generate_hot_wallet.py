"""Gera uma hot wallet Ethereum nova localmente (offline).

NÃO conecta na internet, NÃO faz nada além de gerar um keypair e imprimir.
Use uma vez pra setup do .env, depois APAGUE o output do terminal.

Run: python scripts/generate_hot_wallet.py
"""
from eth_account import Account
import secrets


def main():
    # 32 bytes de entropia direto do OS RNG (urandom).
    privkey = "0x" + secrets.token_hex(32)
    acct = Account.from_key(privkey)

    print("=" * 60)
    print("  HOT WALLET GERADA — guarde com cuidado")
    print("=" * 60)
    print()
    print(f"  Address:     {acct.address}")
    print(f"  Private Key: {privkey}")
    print()
    print("=" * 60)
    print()
    print("Pra .env:")
    print(f"  WALLET_ADDRESS={acct.address}")
    print(f"  WALLET_PRIVATE_KEY={privkey}")
    print()
    print("AVISOS:")
    print("- Essa private key NUNCA pode sair desta máquina")
    print("- Não cola em chat, github, lugar nenhum publico")
    print("- Após copiar pro .env, apague esse output (Ctrl+L)")
    print("- Mantenha só o capital da operação ($300-500) nessa wallet")
    print("- Capital extra fica no Ledger, nunca aqui")


if __name__ == "__main__":
    main()
