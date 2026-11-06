"""`python -m app.mcp [--user NAME]`: the MCP server over stdio, for clients that spawn a local process. It acts as
the account named by --user or MCP_USER (or the only admin, when there is just one), as the HTTP endpoint does for a
client signed in with an app password."""
import sys

from app import config, users
from app.mcp import current_user, server


def _pick_user():
    name = config.MCP_USER
    if "--user" in sys.argv[1:]:
        i = sys.argv.index("--user")
        name = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    accounts = users.list_users()
    if name:
        u = next((u for u in accounts if u["name"].lower() == name.lower()), None)
        if not u:
            sys.exit(f"no ioPix account named {name!r}")
        return u
    admins = [u for u in accounts if u.get("role") == "admin" or u.get("suiteAdmin")]
    if len(admins) == 1:
        return admins[0]
    if not accounts:
        return {"id": 0, "name": "owner", "role": "admin"}  # no accounts yet: the open install, as in the web app
    sys.exit("name the account: python -m app.mcp --user NAME (or MCP_USER=NAME)")


users.init()
current_user.set(_pick_user())
server.run()
