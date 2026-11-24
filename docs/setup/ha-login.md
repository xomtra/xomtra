# Sign-in with a Home Assistant account

With `HAMMOND_AUTH_MODE=ha` (ADR 053) the people of your household sign in to HAmmond
with their Home Assistant account: no Entra tenant, app registration or extra
passwords. It applies to

* the admin GUI (only HA administrators), in both access modes, and
* the Claude connector in the [public access mode](claude-connector.md): Claude
  registers with HAmmond, HAmmond sends your browser to the HA sign-in page.

In the [local access mode](local-access.md) MCP clients use personal tokens instead;
HA sign-in is then used for the admin GUI only.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `HAMMOND_AUTH_MODE` | `entra` | `ha` for this guide |
| `HAMMOND_HA_URL` | – | HA as the HAmmond server reaches it (token exchange, identity) |
| `HAMMOND_HA_AUTH_URL` | `HAMMOND_HA_URL` | HA as **your browser** reaches it (sign-in page) |
| `HAMMOND_HA_ALLOWED_USERS` | empty = all | comma separated HA user ids or names allowed to sign in |
| `HAMMOND_HA_ROLE_MAP` | `admin=HAmmond.Admin,user=HAmmond.Reader` | role of HA administrators (and the owner) and of other HA users; `user=` lets only administrators in |
| `HAMMOND_BASE_URL` | – | HAmmond's own URL; it is the `client_id` HAmmond presents to HA |

Nothing has to be registered in Home Assistant: HA identifies clients by their URL
(IndieAuth) and accepts the redirect back to `<base-url>/auth/callback` and
`<base-url>/admin/auth/callback` because they are on the same host as the client id.

## How it works

1. Your browser goes to HA's sign-in page (`HAMMOND_HA_AUTH_URL/auth/authorize`); 2FA set
   up in HA applies.
2. HAmmond exchanges the code at `HAMMOND_HA_URL/auth/token` and asks HA who you are
   (WebSocket `auth/current_user`, cached for 60 s). HA administrators and the owner get
   the admin role, other users the reader role.
3. Admin GUI: HAmmond revokes the HA token right away and keeps only its own session
   cookie. Claude connector: HAmmond keeps the HA tokens encrypted (like the Entra
   tokens, ADR 026), uses them only to check who you are and refreshes them itself.
   Claude never sees an HA token, only HAmmond's own.
4. Removing or deactivating a user in HA ends their access within 30 minutes (the HA
   access token expires and cannot be refreshed). For the Claude connector a changed HA
   admin flag takes effect within a minute; an admin GUI session ends after
   `HAMMOND_ADMIN_SESSION_HOURS` (default 8) or on sign-out.

HA tokens have no scopes: they carry all rights of the user. That is why HAmmond uses
them for nothing but the identity check, and why the admin GUI revokes its token at
once.

## Limits

* Your browser must reach `HAMMOND_HA_AUTH_URL` when you sign in. If HA is reachable
  only at home, you can connect the Claude connector only from home; afterwards it
  works from anywhere, because refreshing the tokens happens between the HAmmond server
  and HA.
* HA is not a full identity provider: no conditional access or enforced MFA. Enable 2FA
  for HA accounts. For companies and the AWS deployment keep `entra`.
* Audit records carry `idp = ha`, the HA user id as subject and the HA display name.
