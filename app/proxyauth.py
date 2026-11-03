"""Sign-in done by a reverse proxy in front of the viewer (Cloudflare Access, HAProxy, nginx, Caddy or Traefik with
an authentication step): iothing_auth.proxyauth does the work, with the settings stored once for the whole suite
(or per app) in the auth database; the environment (iopix.env) still wins. This module keeps iopix's old names:

  AUTH_PROXY          off (default) | cloudflare | haproxy | nginx | caddy | traefik | header
  AUTH_PROXY_HEADER   the header(s) holding the user, comma-separated
  AUTH_PROXY_TRUSTED  addresses or networks the proxy connects from (default 127.0.0.1, ::1)
  AUTH_PROXY_CREATE   a role (viewer or editor) to create an account for someone the proxy lets in; deny; or empty
  CF_ACCESS_TEAM      the Cloudflare Access team domain, e.g. myteam.cloudflareaccess.com
  CF_ACCESS_AUD       the Access application's audience (AUD) tag
"""
from iothing_auth import proxyauth as shared
from iothing_auth.proxyauth import (CF_HEADER, CF_JWT_HEADER, EXTERNAL_HEADERS, GENERIC_HEADERS, KEYS, PRESETS, authlog,
                                    forget_identities, once, peer_of, remember_peer, seen, _cache, _logged)

__all__ = ["CF_HEADER", "CF_JWT_HEADER", "EXTERNAL_HEADERS", "GENERIC_HEADERS", "KEYS", "PRESETS", "authlog", "forget_identities", "once",
           "peer_of", "remember_peer", "seen", "_cache", "_logged"]  # re-exported for the rest of the app and the tests

from app.auth import auth

_proxy = auth.proxy
log = shared.log


def opt(name: str):
    return _proxy.opt(name)


def save(values: dict, suite_wide: bool = True):
    _proxy.save(values, suite_wide)


def mode() -> str:
    return _proxy.mode()


def headers() -> list:
    return _proxy.headers()


def trusted() -> list:
    return _proxy.trusted()


def cf_settings():
    return _proxy.cf_settings()


def _from_trusted(peer: str) -> bool:
    return _proxy.from_trusted(peer)


def note(headers_in, peer: str):
    _proxy.note(headers_in, peer)


def external(headers_in, peer: str = "") -> bool:
    return _proxy.external(headers_in, peer)


def visitor_of(request) -> str:
    return _proxy.visitor_of(request)


def source(request) -> str:
    return _proxy.source(request)


def identity(headers_in, peer: str):
    return _proxy.identity(headers_in, peer)


def deny_unknown() -> bool:
    return _proxy.deny_unknown()


def user_for(ident: str):
    return _proxy.user_for(ident)


def user(request):
    return _proxy.user(request)


def status() -> dict:
    return _proxy.status()
