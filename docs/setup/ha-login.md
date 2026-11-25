# Sign-in with a Home Assistant account

With `XOMTRA_AUTH_MODE=ha` (ADR 053) the people of your household sign in to Xomtra
with their Home Assistant account: no Entra tenant, app registration or extra
passwords. It applies to

* the admin GUI (only HA administrators), in both access modes, and
* the Claude connector in the [public access mode](claude-connector.md): Claude
  registers with Xomtra, Xomtra sends your browser to the HA sign-in page.

In the [local access mode](local-access.md) MCP clients use personal tokens instead;
HA sign-in is then used for the admin GUI only.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `XOMTRA_AUTH_MODE` | `entra` | `ha` for this guide |
| `XOMTRA_HA_URL` | – | HA as the Xomtra server reaches it (token exchange, identity) |
| `XOMTRA_HA_AUTH_URL` | `XOMTRA_HA_URL` | HA as **your browser** reaches it (sign-in page) |
| `XOMTRA_HA_ALLOWED_USERS` | empty = all | comma separated HA user ids or names allowed to sign in |
| `XOMTRA_HA_ROLE_MAP` | `admin=Xomtra.Admin,user=Xomtra.Reader` | role of HA administrators (and the owner) and of other HA users; `user=` lets only administrators in |
| `XOMTRA_BASE_URL` | – | Xomtra's own URL; it is the `client_id` Xomtra presents to HA |

Nothing has to be registered in Home Assistant: HA identifies clients by their URL
(IndieAuth) and accepts the redirect back to `<base-url>/auth/callback` and
`<base-url>/admin/auth/callback` because they are on the same host as the client id.

## How it works

1. Your browser goes to HA's sign-in page (`XOMTRA_HA_AUTH_URL/auth/authorize`); 2FA set
   up in HA applies.
2. Xomtra exchanges the code at `XOMTRA_HA_URL/auth/token` and asks HA who you are
   (WebSocket `auth/current_user`, cached for 60 s). HA administrators and the owner get
   the admin role, other users the reader role.
3. Admin GUI: Xomtra revokes the HA token right away and keeps only its own session
   cookie. Claude connector: Xomtra keeps the HA tokens encrypted (like the Entra
   tokens, ADR 026), uses them only to check who you are and refreshes them itself.
   Claude never sees an HA token, only Xomtra's own.
4. Removing or deactivating a user in HA ends their access within 30 minutes (the HA
   access token expires and cannot be refreshed). For the Claude connector a changed HA
   admin flag takes effect within a minute; an admin GUI session ends after
   `XOMTRA_ADMIN_SESSION_HOURS` (default 8) or on sign-out.

HA tokens have no scopes: they carry all rights of the user. That is why Xomtra uses
them for nothing but the identity check, and why the admin GUI revokes its token at
once.

## Limits

* Your browser must reach `XOMTRA_HA_AUTH_URL` when you sign in. If HA is reachable
  only at home, you can connect the Claude connector only from home; afterwards it
  works from anywhere, because refreshing the tokens happens between the Xomtra server
  and HA.
* HA is not a full identity provider: no conditional access or enforced MFA. Enable 2FA
  for HA accounts. For companies and the AWS deployment keep `entra`.
* Audit records carry `idp = ha`, the HA user id as subject and the HA display name.
