# Entra ID app registration

xomtra uses FastMCP's OAuth proxy (`AzureProvider`): Claude talks OAuth 2.1 + dynamic
client registration to xomtra, xomtra signs the user in at Entra ID with one fixed app
registration. (Connecting claude.ai directly to Entra does not work today because Entra
does not support dynamic client registration, tracked as claude.ai MCP issue #506.)

You need: an Entra tenant where you can register applications and grant admin consent.

## 1. Register the application

Entra admin center → **Identity → Applications → App registrations → New registration**

| Field | Value |
| --- | --- |
| Name | `xomtra MCP` |
| Supported account types | **Accounts in this organizational directory only** (single tenant) |
| Redirect URI | platform **Web**, `https://<public-host>/auth/callback` |

Note **Application (client) ID** and **Directory (tenant) ID** from the Overview page
→ `.env`: `XOMTRA_ENTRA_CLIENT_ID`, `XOMTRA_ENTRA_TENANT_ID`.

For local tests you can add a second Web redirect URI `http://localhost:8000/auth/callback`.

## 2. Use v2 access tokens

**Manifest** → set `"requestedAccessTokenVersion": 2` (in the `api` section of the new
manifest format, `accessTokenAcceptedVersion` in the old one) → Save.
FastMCP validates the v2 issuer `https://login.microsoftonline.com/<tenant-id>/v2.0`.

## 3. Expose an API scope

**Expose an API** → Application ID URI → **Add** → keep `api://<client-id>` → Save.
**Add a scope**:

| Field | Value |
| --- | --- |
| Scope name | `mcp.access` (must match `XOMTRA_ENTRA_REQUIRED_SCOPES`) |
| Who can consent | Admins only |
| Admin consent display name | `Access xomtra MCP` |
| State | Enabled |

## 4. API permissions

**API permissions** → Microsoft Graph, delegated: `openid`, `profile`, `email`,
`offline_access` (keep `User.Read`). Add your own API → `mcp.access`.
Then **Grant admin consent for <tenant>**.

## 5. Client secret

**Certificates & secrets → New client secret**, expiry max. 12 months (put a renewal
reminder in your calendar). Copy the *Value* → `.env`: `XOMTRA_ENTRA_CLIENT_SECRET`.
`chmod 600 .env`. Never paste the secret into chats, issues or commits.

## 6. App roles and assignment

**App roles → Create app role** (allowed member types: *Users/Groups*):

| Display name | Value | Purpose |
| --- | --- | --- |
| xomtra Admin | `xomtra.Admin` | administration, all data |
| xomtra Reader | `xomtra.Reader` | read catalog and data views |

**Enterprise applications → xomtra MCP → Properties → Assignment required? = Yes**
(only assigned users can sign in), then **Users and groups → Add user/group** and assign
yourself the `xomtra.Admin` role. The `whoami` tool shows the roles from the token.
What each role may do is described in [Catalog and MCP data tools](mcp-tools.md); a user
without a role can only call `whoami`.

## 7. Recommended hardening

- Conditional Access policy for the app: MFA, compliant/known devices or named locations.
- Sign-in logs of the enterprise app are the second audit trail next to xomtra's audit.

## `.env` summary

```dotenv
XOMTRA_AUTH_MODE=entra
XOMTRA_BASE_URL=https://<public-host>
XOMTRA_ENTRA_TENANT_ID=<tenant-id>
XOMTRA_ENTRA_CLIENT_ID=<client-id>
XOMTRA_ENTRA_CLIENT_SECRET=<secret value>
XOMTRA_ENTRA_REQUIRED_SCOPES=mcp.access
XOMTRA_JWT_SIGNING_KEY=<openssl rand -hex 32>
```
