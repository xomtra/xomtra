# xomtra-mcp

MCP server of Xomtra built on FastMCP, protected by an OAuth proxy to Microsoft Entra ID.

Tools, roles, SQL limits and audit: [docs/setup/mcp-tools.md](../../docs/setup/mcp-tools.md).

| Module | Purpose |
| --- | --- |
| `server.py` | FastMCP app and tools |
| `guard.py` | middleware: tool list and calls by role, audit of every call |
| `policy.py`, `policy.yaml` | roles -> tools, views, hidden entities, row limits |
| `data.py` | read-only, locked-down DuckDB over the lake; per-call filtered views |
| `sqlguard.py` | `query_sql` check (one SELECT over permitted views) |
| `audit.py` | audit records to the log and the INSERT-only Postgres table |
