"""Sign-in by a reverse proxy: the user header is believed only from the proxy (or a verified Cloudflare Access
token), matched to an account by email or name, optionally creating one."""
import time

import pytest

from app import proxyauth, users


@pytest.fixture
def account(fresh):
    u = users.create("proxy-probe", "longpassword1", "editor", email="probe@example.com")
    proxyauth._cache.clear()
    yield u
    proxyauth._cache.clear()


def test_off_by_default(monkeypatch, account):
    monkeypatch.delenv("AUTH_PROXY", raising=False)
    assert proxyauth.identity({"Remote-User": "proxy-probe"}, "127.0.0.1") is None


@pytest.mark.parametrize("preset", ["haproxy", "nginx", "caddy", "traefik", "header"])
def test_header_from_the_proxy_only(monkeypatch, account, preset):
    monkeypatch.setenv("AUTH_PROXY", preset)
    monkeypatch.delenv("AUTH_PROXY_HEADER", raising=False)
    monkeypatch.delenv("AUTH_PROXY_TRUSTED", raising=False)
    assert proxyauth.identity({"Remote-User": "proxy-probe"}, "127.0.0.1") == "proxy-probe"
    assert proxyauth.identity({"X-Auth-Request-Email": "probe@example.com"}, "::ffff:127.0.0.1") == "probe@example.com"
    assert proxyauth.identity({"Remote-User": "proxy-probe"}, "192.168.1.50") is None, "not from the proxy: ignored"
    monkeypatch.setenv("AUTH_PROXY_TRUSTED", "192.168.1.0/24")
    assert proxyauth.identity({"Remote-User": "proxy-probe"}, "192.168.1.50") == "proxy-probe"
    monkeypatch.setenv("AUTH_PROXY_HEADER", "X-Custom-User")
    assert proxyauth.identity({"Remote-User": "x", "X-Custom-User": "proxy-probe"}, "192.168.1.50") == "proxy-probe"
    assert proxyauth.user_for("PROBE@example.com")["id"] == account["id"] and proxyauth.user_for("Proxy-Probe")["id"] == account["id"]


def test_unknown_people_are_created_only_when_allowed(monkeypatch, account):
    monkeypatch.delenv("AUTH_PROXY_CREATE", raising=False)
    assert proxyauth.user_for("new-person@example.com") is None
    monkeypatch.setenv("AUTH_PROXY_CREATE", "editor")
    u = proxyauth.user_for("new-person@example.com")
    assert u and u["role"] == "editor" and u["email"] == "new-person@example.com"


def test_cloudflare_access_token_is_verified(monkeypatch, account):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key, other = (rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))
    monkeypatch.setenv("AUTH_PROXY", "cloudflare")
    monkeypatch.setenv("CF_ACCESS_TEAM", "myteam.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud-tag")

    class Signing:
        def __init__(self, k):
            self.key = k
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", lambda self, t: Signing(key.public_key()))

    def token(k=key, **over):
        claims = {"email": "probe@example.com", "aud": ["aud-tag"], "iss": "https://myteam.cloudflareaccess.com", "exp": int(time.time()) + 300} | over
        return jwt.encode(claims, k, algorithm="RS256")
    hdr = proxyauth.CF_JWT_HEADER
    # from anywhere: the token is the proof, and the email comes from it, not from a header someone could set
    assert proxyauth.identity({hdr: token(), proxyauth.CF_HEADER: "someone@else"}, "203.0.113.9") == "probe@example.com"
    assert proxyauth.identity({hdr: token(k=other)}, "127.0.0.1") is None, "signed by someone else"
    assert proxyauth.identity({hdr: token(aud=["another-app"])}, "127.0.0.1") is None
    assert proxyauth.identity({hdr: token(exp=int(time.time()) - 600)}, "127.0.0.1") is None
    assert proxyauth.identity({proxyauth.CF_HEADER: "probe@example.com"}, "127.0.0.1") is None, "no token: nobody"
    # without the team settings: the plain header, from the proxy (cloudflared) only
    monkeypatch.delenv("CF_ACCESS_TEAM")
    assert proxyauth.identity({proxyauth.CF_HEADER: "probe@example.com"}, "127.0.0.1") == "probe@example.com"
    assert proxyauth.identity({proxyauth.CF_HEADER: "probe@example.com"}, "203.0.113.9") is None


def test_requests_through_the_proxy_are_signed_in(client, monkeypatch, account):
    monkeypatch.setenv("AUTH_PROXY", "nginx")
    monkeypatch.setattr(proxyauth, "_from_trusted", lambda peer: True)
    me = client.get("/api/auth/me", headers={"Remote-User": "proxy-probe"}).json()
    assert me["user"]["name"] == "proxy-probe" and me["viaProxy"] and me["canWrite"]
    assert client.get("/api/auth/me").json()["user"] is None, "no header: not signed in"


def test_the_real_peer_survives_uvicorns_forwarded_rewrite():
    """uvicorn replaces the client address with X-Forwarded-For for its trusted hosts; proxy sign-in keeps the
    address the connection came from."""
    import asyncio
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    proxyauth.remember_peer()
    seen = {}

    async def inner(scope, receive, send):
        seen.update(client=scope["client"][0], peer=scope.get("iopix.peer"))
    mw = ProxyHeadersMiddleware(inner, trusted_hosts="127.0.0.1")
    scope = {"type": "http", "client": ("127.0.0.1", 5000), "headers": [(b"x-forwarded-for", b"203.0.113.9")], "scheme": "http"}
    asyncio.run(mw(scope, None, None))
    assert seen == {"client": "203.0.113.9", "peer": "127.0.0.1"}, seen
