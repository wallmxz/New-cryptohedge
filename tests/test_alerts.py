import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from web.alerts import post_alert


@pytest.mark.asyncio
async def test_post_alert_sends_payload():
    """post_alert sends JSON to webhook URL."""
    with patch("web.alerts.httpx.AsyncClient") as MockClient:
        client_instance = MockClient.return_value.__aenter__.return_value
        client_instance.post = AsyncMock(return_value=MagicMock(status_code=200))

        await post_alert(
            url="https://hooks.test/x",
            level="warning",
            message="Margin low",
            data={"ratio": 0.55},
        )
        client_instance.post.assert_called_once()
        kwargs = client_instance.post.call_args.kwargs
        assert "Margin low" in str(kwargs.get("json", {}))


@pytest.mark.asyncio
async def test_post_alert_skips_if_no_url():
    """No URL -> no-op (no error)."""
    await post_alert(url="", level="info", message="x", data={})
    # If no exception, pass
