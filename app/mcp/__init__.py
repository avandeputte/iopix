"""MCP server for the digiKam library.

Lets an MCP client such as Claude search the library, read any photo's
metadata, browse people / tags / albums, count things, and look at photos
(all read-only), plus operator tools behind the admin token: diagnostics,
the server log, the job queue, a photo's files on disk, the maintenance
reports and edits, and running update.sh. It is served at /mcp by the web
app (main.py) and can also run on its own with `python -m app.mcp` (stdio
transport), sharing the web app's index and thumbnail cache.

The package is split by who may call a tool, not by subject:
  base.py   the MCPServer instance, the index connection, the admin-token gate and the
            helpers every tool module shares (name resolution, limits, the filter translation)
  read.py   the anonymous read-only tools: overview, search, counts, one photo, the lists
  files.py  the operator's diagnostics: health, log, job queue, one file on disk, repairs, the updater
  admin.py  the maintenance tools: reports, suggestions, edits and jobs the web app's pages offer
Importing the package imports the three tool modules, which registers every tool on `server`;
a tool's name, signature and docstring are its contract with the clients, so they do not change
when the code behind them moves.
"""
from app.mcp.base import server, token_given, db
from app.mcp import read, files, admin  # noqa: F401  importing them registers the tools on `server`

__all__ = ["server", "token_given", "db", "read", "files", "admin"]
