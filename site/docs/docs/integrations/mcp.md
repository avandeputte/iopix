# MCP server for AI assistants

iopix is also an [MCP](https://modelcontextprotocol.io) server, so an AI assistant such as Claude can search and look at your library through the running app. It is at `/mcp` on the same port. Nothing extra is installed.

## Library tools (read-only)

These show only what an anonymous visitor sees in the web app:

| Tool | Does |
| --- | --- |
| `library_overview` | totals and what the library holds |
| `search_photos` | every filter the web app has, plus `looks_like` text and `similar_to` a photo |
| `count_by` | counts grouped by year, person, tag, place, camera… |
| `get_photo` | everything about one photo |
| `view_photo` | the picture itself, so the assistant can look at it |
| `list_people`, `list_tags`, `list_albums`, `list_places`, `list_app_albums` | the collections |
| `find_duplicates` | duplicate files |

Results carry links into iopix (set `VIEWER_URL` to your public address).

## Admin tools

These need the **admin token** from **Maintenance › iopix › MCP update token** (or `MCP_ADMIN_TOKEN`), because they reveal paths, log lines and settings:

`diagnostics`, `server_log`, `job_queue`, `photo_file`, and `update_viewer`, which installs a new version. `check_for_update` is open.

!!! danger "Treat the admin token like a root password"
    `update_viewer` replaces the code iopix runs. Keep the token secret, and regenerate it if it was ever shared.

## Connecting Claude

=== "Claude Code"

    ```sh
    claude mcp add --transport http iopix https://photos.example.com/mcp
    ```

=== "Claude Desktop"

    Claude Desktop reaches custom connectors from the internet, so for a server at home use the `mcp-remote` bridge (needs Node.js). Edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`) and restart Claude Desktop:

    ```json
    {
      "mcpServers": {
        "iopix": {
          "command": "npx",
          "args": ["-y", "mcp-remote", "https://photos.example.com/mcp"]
        }
      }
    }
    ```

    Without HTTPS, use `http://<host>:8600/mcp` and add `"--allow-http"` to the arguments.

=== "With Require sign-in on"

    Every request must carry the admin token:

    ```json
    "args": ["-y", "mcp-remote", "https://photos.example.com/mcp",
             "--header", "Authorization: Bearer <token>"]
    ```

## Exposing it safely

The library tools answer without sign-in unless **Require sign-in** is on. If iopix is reachable from the internet, turn Require sign-in on (so `/mcp` needs the token), or keep `/mcp` reachable only from your own network. Never expose port 8600 directly.

Reverse proxies must not buffer `/mcp`, because it streams. See [HTTPS and a reverse proxy](../install/reverse-proxy.md).
