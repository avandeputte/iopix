# Sign-in through a proxy

If a proxy in front of iopix already makes people sign in (Cloudflare Access, Authelia, Authentik, oauth2-proxy), iopix can take that sign-in instead of asking again. It matches the person to an iopix account by **email address**, then by name.

The built-in sign-in keeps working alongside, for example at home where requests skip the proxy.

## Cloudflare Access (recommended for a Cloudflare Tunnel)

1. In Cloudflare Zero Trust, add an **Access application** for iopix's hostname, with a policy for the people allowed in.
2. Copy the application's **Audience (AUD) tag** (Access › Applications › your app › Overview).
3. Give each iopix account the email address it signs in to Cloudflare with (**Settings › Users and sign-in**).
4. Set:

```sh
AUTH_PROXY=cloudflare
CF_ACCESS_TEAM=yourteam.cloudflareaccess.com
CF_ACCESS_AUD=<the AUD tag>
```

With the team and AUD set, iopix checks Cloudflare's signed token on every request against your team's keys, so the sign-in cannot be faked.

**Sign out** in iopix also ends the Cloudflare Access session. A sign-in through Cloudflare starts an iopix session too, so the same browser stays signed in back on the home network.

Requests that come through Cloudflare never get one-tap sign-in: passwordless accounts are hidden from the picker and refused.

## Other proxies

| Setting | Meaning |
| --- | --- |
| `AUTH_PROXY` | `cloudflare`, `haproxy`, `nginx`, `caddy`, `traefik` or `header`; `off` by default |
| `AUTH_PROXY_HEADER` | the header(s) carrying the user. Defaults: `Cf-Access-Authenticated-User-Email` for Cloudflare; otherwise `Remote-User`, `X-Forwarded-User`, `X-Auth-Request-User`, `X-Auth-Request-Email`, `X-Forwarded-Email` |
| `AUTH_PROXY_TRUSTED` | addresses the proxy connects from, such as `172.18.0.0/16` for a Docker network; default the same machine only |
| `AUTH_PROXY_CREATE` | `viewer` or `editor` to create an account for someone the proxy lets in who has none; `deny` to show "No access"; empty (default) to show the sign-in screen |

The header is believed **only from the proxy's address**. The proxy must replace it on every request, and iopix must not be reachable except through the proxy.

=== "Caddy + Authelia"

    ```
    photos.example.com {
        forward_auth authelia:9091 {
            uri /api/authz/forward-auth
            copy_headers Remote-User Remote-Email
        }
        reverse_proxy localhost:8600
    }
    ```

=== "Traefik"

    A `forwardAuth` middleware with `authResponseHeaders: [Remote-User, Remote-Email]`.

=== "nginx"

    ```nginx
    auth_request /auth;
    auth_request_set $user $upstream_http_remote_user;
    proxy_set_header Remote-User $user;
    ```

    With oauth2-proxy, `X-Auth-Request-Email` works as is.

=== "HAProxy"

    Delete the header first, then set it after the auth check:

    ```
    http-request del-header Remote-User
    http-request set-header Remote-User %[var(req.auth_response_header.remote_user)]
    ```

These settings can also be made in the app (**Settings › Users and sign-in › Sign-in at a proxy**); `iopix.env` wins when both are set.

Turn on **Require sign-in** as well, so nobody gets in without the proxy's sign-in.
