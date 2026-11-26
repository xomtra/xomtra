# Sign-in with a Home Assistant account

Sign-in with a Home Assistant account is the default (`XOMTRA_AUTH_MODE=ha`, ADR 053 and
ADR 081): the people of your household sign in to xomtra with their Home Assistant
account, with no Entra tenant, app registration or extra passwords. Microsoft Entra ID
remains an option ([guide](entra-app-registration.md)). It applies to

* the admin GUI (only HA administrators), in both access modes, and
* the Claude connector in the [public access mode](claude-connector.md): Claude
  registers with xomtra, xomtra sends your browser to the HA sign-in page.

In the [local access mode](local-access.md) MCP clients use personal tokens instead;
HA sign-in is then used for the admin GUI only.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `XOMTRA_AUTH_MODE` | `ha` | `entra` for Entra ID instead |
| `XOMTRA_HA_URL` | – | HA as the xomtra server reaches it (token exchange, identity) |
| `XOMTRA_HA_AUTH_URL` | `XOMTRA_HA_URL` | HA as **your browser** reaches it (sign-in page) |
| `XOMTRA_HA_REQUIRE_MFA` | `true` | Claude connector: only HA users with two-factor authentication |
| `XOMTRA_SESSION_DAYS` | `180` | Claude connector: a sign-in ends this many days after it started (7–365) |
| `XOMTRA_HA_ROLE_MAP` | `admin=xomtra.Admin,user=` | role of HA administrators (and the owner) and the default role of other HA users; empty = no access until given on the page Access |
| `XOMTRA_HA_ALLOWED_USERS` | empty | optional hard limit: comma separated HA user ids or names; nobody else may sign in at all |
| `XOMTRA_BASE_URL` | – | xomtra's own URL; it is the `client_id` xomtra presents to HA |

Nothing has to be registered in Home Assistant: HA identifies clients by their URL
(IndieAuth) and accepts the redirect back to `<base-url>/auth/callback` and
`<base-url>/admin/auth/callback` because they are on the same host as the client id.

## Who gets in (ADR 081)

* **HA administrators and the owner** get the role `xomtra.Admin`.
* **Other HA users** (children, guests, tablet accounts) get **no access** until an
  administrator gives them the role `xomtra.Reader` on the admin page **Access**. A user
  appears there after their first attempt to connect Claude.
* For the Claude connector the HA user needs **two-factor authentication** (Home
  Assistant → profile → Security → Multi-factor authentication modules). Without it the
  sign-in is refused with a hint.

## How it works

1. Your browser goes to HA's sign-in page (`XOMTRA_HA_AUTH_URL/auth/authorize`) and you
   sign in, with 2FA.
2. xomtra exchanges the code at `XOMTRA_HA_URL/auth/token` and asks HA who you are
   (WebSocket `auth/current_user`): name, admin flag, two-factor authentication.
3. If you may use xomtra, xomtra records a **session** (page Access) and **revokes your
   HA token right away**, for the admin GUI and for the Claude connector alike. No HA
   token of yours stays in xomtra; HA tokens have no scopes and would carry all your
   rights.
4. Claude gets xomtra's own tokens. Every request is checked against the session and
   the access list and, when xomtra's own Home Assistant connection may list users,
   against HA's user directory (refreshed every 5 minutes).

## Connect at home, use it anywhere

You need a browser that reaches Home Assistant **only when you connect** the connector.
Afterwards Claude's requests come from Anthropic's servers, not from your phone, and
xomtra no longer needs your HA sign-in. So: connect the connector at home (or over VPN),
then use Claude on your phone anywhere.

If Home Assistant is reachable from the internet anyway (Home Assistant Cloud or your
own reverse proxy), set `XOMTRA_HA_AUTH_URL` to that address and you can connect from
anywhere. xomtra does not make Home Assistant reachable from the internet itself.

You connect again when:

* the session ends (`XOMTRA_SESSION_DAYS`, default 180 days after the sign-in); in the
  last 14 days `server_info` asks the assistant to tell you,
* an administrator disconnects it on the page Access, removes your access, or removes
  or deactivates your HA user,
* `XOMTRA_JWT_SIGNING_KEY` changes or the `/data` volume of the MCP server is lost,
* you disconnect the connector in Claude or sign in with another Claude account.

Claude then shows that the connector needs to be connected again. Nothing is lost.

## Limits

* HA is not a full identity provider: no conditional access. Two-factor authentication
  is enforced by xomtra for the Claude connector (`XOMTRA_HA_REQUIRE_MFA`). If HA's
  sign-in page is reachable from the internet, also turn on HA's `ip_ban_enabled` and
  `login_attempts_threshold`.
* Without an administrator token in xomtra's own HA connection, a user removed or
  deactivated in HA keeps the connector until the session ends or an administrator
  disconnects it on the page Access.
* Audit records carry `idp = ha`, the HA user id as subject and the HA display name.
  Every sign-in of the connector is recorded as `connector:sign_in` (allowed or denied,
  with `ha_token_revoked`), a refused session as `connector:session_refused`, role
  changes and disconnects on the page Access as `admin:access_role` and
  `admin:access_disconnect`.
