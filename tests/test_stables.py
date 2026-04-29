from stables import STABLECOINS_ARBITRUM, DYDX_TOKEN_TO_PERP, is_stable, dydx_perp_for


def test_stables_set_contains_canonical_addrs():
    """Stables set should include native USDC, USDT, USDC.e, DAI on Arbitrum."""
    # Native USDC (most common)
    assert "0xaf88d065e77c8cC2239327C5EDb3A432268e5831" in STABLECOINS_ARBITRUM
    # USDT
    assert "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9" in STABLECOINS_ARBITRUM
    # USDC.e (legacy bridged)
    assert "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8" in STABLECOINS_ARBITRUM
    # DAI
    assert "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1" in STABLECOINS_ARBITRUM


def test_is_stable_case_insensitive():
    """is_stable should match regardless of address casing."""
    upper = "0xAF88D065E77C8CC2239327C5EDB3A432268E5831"
    lower = upper.lower()
    assert is_stable(upper)
    assert is_stable(lower)


def test_is_stable_rejects_non_stable():
    """A non-stable address (WETH) is rejected."""
    weth = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
    assert not is_stable(weth)


def test_dydx_perp_for_known_tokens():
    """Known wrapped/native tokens map to dYdX perp tickers."""
    assert dydx_perp_for("WETH") == "ETH-USD"
    assert dydx_perp_for("WBTC") == "BTC-USD"
    assert dydx_perp_for("ARB") == "ARB-USD"
    assert dydx_perp_for("LINK") == "LINK-USD"
    assert dydx_perp_for("SOL") == "SOL-USD"


def test_dydx_perp_for_unknown_returns_none():
    """Unknown symbols return None."""
    assert dydx_perp_for("UNKNOWN") is None
    assert dydx_perp_for("") is None


def test_dydx_perp_for_case_insensitive():
    """Symbols match regardless of case."""
    assert dydx_perp_for("weth") == "ETH-USD"
    assert dydx_perp_for("Arb") == "ARB-USD"
