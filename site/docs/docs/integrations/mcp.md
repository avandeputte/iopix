# MCP server for AI assistants

ioPix is also an [MCP](https://modelcontextprotocol.io) server, so an AI assistant such as Claude can search and look at your library through the running app. It is at `/mcp` on the same port, the same in every app of the suite. Nothing extra is installed. **Every request signs in with an app password** (**Settings › Apps and devices**), sent as `Authorization: Bearer <app password>` or as HTTP Basic with your user name; without one `/mcp` answers 401, whether or not Require sign-in is on. The assistant then acts as that account.

It is **off by default**: turn it on in **Settings › MCP server** (or set `MCP=1`). While it is off, `/mcp` does not answer.

## Library tools (read-only)

What the web app shows that account, read-only:

| Tool | Does |
| --- | --- |
| `library_overview` | totals and what the library holds |
| `search_photos` | every filter the web app has, plus `looks_like` text and `similar_to` a photo |
| `count_by` | counts grouped by year, person, tag, place, camera… |
| `get_photo` | everything about one photo (the file's path for admins) |
| `view_photo` | the picture itself, so the assistant can look at it |
| `list_people`, `list_tags`, `list_albums`, `list_places`, `list_app_albums` | the collections |
| `find_duplicates` | duplicate files |

Results carry links into ioPix (set `PUBLIC_URL` to your public address).

## Admin tools

These take an **admin's account** (the app password of an admin), because they reveal paths, log lines and settings or change the library:

`diagnostics`, `server_log`, `job_queue`, `photo_file`, `date_suggestions` (the year suggestions for undated photos, with their clues), `set_date_hint` (a folder's earliest and latest possible year), `place_suggestions` (where photos without a position were probably taken), `jersey_suggestions` (players for the shirt numbers read on unnamed people), `clothing_suggestions` (people probably present, by their clothes), `rotation_findings` (photos stored on their side or upside down), `run_analysis` (start a background analysis, as Maintenance does), and `update_viewer`, which installs a new version. `check_for_update` answers any account.

!!! danger "Treat an admin's app password like a root password"
    `update_viewer` replaces the code ioPix runs. Give the assistant its own app password and revoke it (Settings › Apps and devices) if it was ever shared.

## Connecting Claude

=== "Claude Code"

    ```sh
    claude mcp add --transport http iopix https://photos.example.com/mcp --header "Authorization: Bearer <app password>"
    ```

=== "Claude Desktop"

    Claude Desktop reaches custom connectors from the internet, so for a server at home use the `mcp-remote` bridge (needs Node.js). Edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`) and restart Claude Desktop:

    ```json
    {
      "mcpServers": {
        "iopix": {
          "command": "npx",
          "args": ["-y", "mcp-remote", "https://photos.example.com/mcp",
                   "--header", "Authorization: Bearer <app password>"]
        }
      }
    }
    ```

    Without HTTPS, use `http://<host>:8600/mcp` and add `"--allow-http"` to the arguments.

=== "On the server (stdio)"

    `.venv/bin/python -m app.mcp --user NAME` (or `MCP_USER=NAME`) serves the same tools on stdio, as that account.

## Exposing it safely

Nothing on `/mcp` answers without an account, so the endpoint can sit behind the same reverse proxy as the web app. Treat an admin's app password like a password: it also runs the updater. Never expose port 8600 directly.

Reverse proxies must not buffer `/mcp`, because it streams. See [HTTPS and a reverse proxy](../install/reverse-proxy.md).
