# HTTPS and a reverse proxy

iopix speaks plain HTTP on port 8600 and has no HTTPS of its own. Put it behind a reverse proxy that handles HTTPS, such as [Caddy](https://caddyserver.com), [Nginx Proxy Manager](https://nginxproxymanager.com), Traefik or HAProxy, and reach it only through that.

HTTPS is not just about encryption: browsers only allow **Add to Home Screen / Install app** and offline support on a secure address.

## Caddy

Caddy gets certificates automatically:

```
photos.example.com {
    reverse_proxy localhost:8600
}
```

## nginx / Nginx Proxy Manager

Proxy to `http://<host>:8600`, and for the `/mcp` path turn buffering off, because it streams:

```nginx
location /mcp {
    proxy_pass http://iopix:8600;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

Allow request bodies of a few MB for GPX tracks, and larger ones if you import photos through the browser.

## Cloudflare Tunnel

A Cloudflare Tunnel publishes iopix without opening a port at home. Pair it with Cloudflare Access so people sign in before they reach iopix, and let iopix trust that sign-in: see [Sign-in through a proxy](../admin/proxy-signin.md).

## After the proxy is in place

- Set `VIEWER_URL=https://photos.example.com` so links in MCP results use the public address.
- Turn on **Require sign-in** (Settings › Users and sign-in) on anything that faces the internet.
- Do not expose port 8600 directly.

!!! warning "The MCP endpoint"
    The MCP server's library tools answer without sign-in unless **Require sign-in** is on. On a proxy that faces the internet, turn it on, or keep `/mcp` reachable only from your own network. See [MCP server](../integrations/mcp.md).
