"""
Offline stress tests for KeycloakTokenProvider: a fake IdP replaces _grant,
so concurrency, expiry, failure and cancellation paths run without Docker.

Run: python test_provider.py
"""
import asyncio
import base64
import json
import time

from app import KeycloakTokenProvider, OAuthError, token_exp


def make_jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp)}).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


class FakeIdP:
    """Injectable _grant: controllable latency, token lifespan and failures."""

    def __init__(self, lifespan: float = 60, latency: float = 0.05):
        self.lifespan = lifespan
        self.latency = latency
        self.fail_with: Exception | None = None
        self.omit_refresh_token = False
        self.grants: list[str] = []

    async def grant(self, fields: dict) -> dict:
        await asyncio.sleep(self.latency)
        self.grants.append(fields["grant_type"])
        if self.fail_with is not None:
            raise self.fail_with
        tokens = {"access_token": make_jwt(time.time() + self.lifespan)}
        if not self.omit_refresh_token:
            tokens["refresh_token"] = f"rt-{len(self.grants)}"
        return tokens


def make_provider(idp: FakeIdP) -> KeycloakTokenProvider:
    provider = KeycloakTokenProvider("u", "p")
    provider._grant = idp.grant
    return provider


async def test_cold_start_stampede():
    """50 concurrent first calls -> one password grant, one shared token."""
    idp = FakeIdP()
    provider = make_provider(idp)
    tokens = await asyncio.gather(*[provider() for _ in range(50)])
    assert len(set(tokens)) == 1
    assert idp.grants == ["password"]
    print("OK cold_start_stampede")


async def test_expiry_burst_single_refresh():
    """Expired token + 50 concurrent calls -> exactly one refresh grant."""
    idp = FakeIdP()
    provider = make_provider(idp)
    await provider()
    provider._tokens["access_token"] = make_jwt(time.time() - 1)
    provider._renewed_at -= provider.BURST_WINDOW + 1
    tokens = await asyncio.gather(*[provider() for _ in range(50)])
    assert len(set(tokens)) == 1
    assert idp.grants == ["password", "refresh_token"]
    print("OK expiry_burst_single_refresh")


async def test_valid_token_reused_without_grants():
    """A valid token inside the burst window is served without IdP traffic."""
    idp = FakeIdP()
    provider = make_provider(idp)
    first = await provider()
    again = await asyncio.gather(*[provider() for _ in range(20)])
    assert set(again) == {first}
    assert idp.grants == ["password"]
    print("OK valid_token_reused_without_grants")


async def test_rejected_valid_token_renews_after_burst_window():
    """A locally-valid token outside the burst window is treated as rejected."""
    idp = FakeIdP()
    provider = make_provider(idp)
    await provider()
    provider._renewed_at -= provider.BURST_WINDOW + 1
    await provider()
    assert idp.grants == ["password", "refresh_token"]
    print("OK rejected_valid_token_renews_after_burst_window")


async def test_dead_refresh_falls_back_once():
    """invalid_grant on refresh -> one password grant, even for 50 callers."""
    idp = FakeIdP()
    provider = make_provider(idp)
    await provider()
    provider.corrupt_refresh_token()
    provider.drop_access_token()

    real_grant = idp.grant

    async def grant(fields):
        if fields["grant_type"] == "refresh_token":
            idp.grants.append("refresh_token")
            raise OAuthError("invalid_grant", "dead")
        return await real_grant(fields)

    provider._grant = grant
    tokens = await asyncio.gather(*[provider() for _ in range(50)])
    assert len(set(tokens)) == 1
    assert idp.grants == ["password", "refresh_token", "password"]
    print("OK dead_refresh_falls_back_once")


async def test_renewal_error_leaves_state_recoverable():
    """A failing IdP propagates to every waiter; state recovers afterwards."""
    idp = FakeIdP()
    provider = make_provider(idp)
    await provider()
    old_tokens = dict(provider._tokens)
    provider.drop_access_token()
    idp.fail_with = OAuthError("temporarily_unavailable", "boom")

    results = await asyncio.gather(*[provider() for _ in range(10)],
                                   return_exceptions=True)
    assert all(isinstance(r, OAuthError) for r in results)
    assert provider._tokens.get("refresh_token") == old_tokens["refresh_token"]

    idp.fail_with = None
    token = await provider()
    assert time.time() < token_exp(token)
    print("OK renewal_error_leaves_state_recoverable")


async def test_refresh_response_without_refresh_token():
    """RFC 6749 allows omitting refresh_token; the stored one must survive."""
    idp = FakeIdP()
    provider = make_provider(idp)
    await provider()
    kept = provider._tokens["refresh_token"]
    idp.omit_refresh_token = True
    provider.drop_access_token()
    await provider()
    assert provider._tokens["refresh_token"] == kept
    assert idp.grants == ["password", "refresh_token"]
    print("OK refresh_response_without_refresh_token")


async def test_cancelled_waiter_does_not_poison_lock():
    """Cancelling a caller queued on the lock leaves others unaffected."""
    idp = FakeIdP(latency=0.2)
    provider = make_provider(idp)
    leader = asyncio.create_task(provider())
    victim = asyncio.create_task(provider())
    survivor = asyncio.create_task(provider())
    await asyncio.sleep(0.05)  # all three inside __call__, leader in _grant
    victim.cancel()
    tokens = await asyncio.gather(leader, survivor)
    assert len(set(tokens)) == 1
    assert victim.cancelled()
    assert idp.grants == ["password"]
    print("OK cancelled_waiter_does_not_poison_lock")


async def test_driver_style_invocation():
    """The driver calls the provider from an executor thread and awaits the
    returned coroutine on the loop; both provider call styles must work."""
    idp = FakeIdP()
    provider = make_provider(idp)
    result = await asyncio.get_running_loop().run_in_executor(None, provider)
    token = await result
    assert time.time() < token_exp(token)
    assert idp.grants == ["password"]
    print("OK driver_style_invocation")


async def main():
    for test in [
        test_cold_start_stampede,
        test_expiry_burst_single_refresh,
        test_valid_token_reused_without_grants,
        test_rejected_valid_token_renews_after_burst_window,
        test_dead_refresh_falls_back_once,
        test_renewal_error_leaves_state_recoverable,
        test_refresh_response_without_refresh_token,
        test_cancelled_waiter_does_not_poison_lock,
        test_driver_style_invocation,
    ]:
        await test()
    print("\nall provider tests passed")


if __name__ == "__main__":
    asyncio.run(main())
