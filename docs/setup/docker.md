# Docker Compose: install with one command

xomtra on any Linux machine with Docker (a virtual machine, a mini PC, a NAS). Nothing to
fill in before the start (ADR 077): the internal passwords and keys are generated on the
first start, and Home Assistant is connected in the browser afterwards.

Requirements: Docker Engine with the Compose plugin v2 ([official install](https://docs.docker.com/engine/install/),
check with `docker compose version`), 2 CPU
cores and 4 GB RAM, a few GB of disk,
and Home Assistant reachable from this machine, usually on port 8123
(`http://<ha-address>:8123`).

Tools: `git` and `curl` to download the release (`sudo apt-get install -y git curl` on
Debian and Ubuntu, `sudo dnf install -y git curl` on Fedora and RHEL). Verifying the
signatures of the images is optional, with cosign in its container
([releasing.md](../releasing.md#installing-a-release)).

## 0. Address (optional, before the first start)

Nothing is needed: without settings xomtra uses this machine's LAN address
(`https://192.168.1.20/`). Reserve that address in your router so that DHCP does not
change it.

If you want a name instead (`https://xomtra.home.arpa/`), set it in step 1 after the download
and before the first `docker compose up`:

```bash
echo 'XOMTRA_LOCAL_HOST=xomtra.home.arpa' >> .env   # in xomtra/deploy/compose
```

The name only has to resolve to this machine on the clients (browser, Claude Desktop,
Claude Code), e.g. a DNS record in your router or Pi-hole; the server itself does not
look it up, and no public DNS or internet access is needed. xomtra then answers only
under that name, not under the IP address.

You can change it later (see [Optional settings](#optional-settings-env)), so a final
name is not a precondition; it only saves you from updating the clients afterwards.

## 1. Start

Download the release (or clone the repository), start the stack and print the setup
link:

```bash
git clone --depth 1 --branch v<version> https://github.com/xomtra/xomtra.git
cd xomtra/deploy/compose
docker compose up -d && docker compose exec admin xomtra-admin setup-link
```

`docker compose up -d` returns once the admin GUI is up (it waits for the databases,
which takes a minute or two on the first start); the second command then prints the
one-time setup link for step 2.

On the first start the service `secrets-init`:

- generates every internal password and key into the Docker volume `secrets` (each
  service gets a file with only the values it needs, readable only by its user);
- finds this machine's address in the LAN; the server is then reachable at
  `https://<that address>/` (the local access mode, LAN only).

Later starts keep everything; a new version only adds keys it introduces.

## 2. Connect Home Assistant

The installation ends with the one-time setup link:

```text
Finish the setup of xomtra in a browser on your network:

    https://192.168.1.20/admin/setup?code=ABCD-EFGH-IJKL-MNOP
```

Print it again at any time with `docker compose exec admin xomtra-admin setup-link`; it
is also in the log of the admin container (`docker compose logs admin`).

Open it in a browser in your LAN. The browser warns about the certificate (xomtra uses
its own certificate authority in the LAN, see [local access](local-access.md#2-trust-the-servers-certificate));
continue to the page. Enter:

- **Home Assistant URL** as this machine reaches it, e.g. `http://192.168.1.10:8123`;
- a **long-lived access token** of a user for xomtra. In Home Assistant create a user
  (it does not need to be an administrator), sign in as that user, open its profile →
  Security → Long-lived access tokens → Create token.

xomtra checks the connection before it saves it; the token is stored encrypted in its
database, not in a file. The code stops working once Home Assistant is connected. The
live stream and the history import start right away (within 15 seconds).

## 3. Sign in and connect Claude

Open `https://<address>/admin/` and sign in with your **Home Assistant administrator**
account. Then continue with [local access](local-access.md#3-issue-a-token): issue a
personal token and connect Claude Desktop, Claude Code or another MCP client.

To change the Home Assistant URL or token later: Overview → "Change the Home Assistant
URL or token" (signed-in administrators only). The live stream picks up a new token
after `docker compose restart ingest-stream`.

## Optional settings (.env)

Every setting has a default. To change one, copy `.env.example` to `.env` and set only
what you need, for example:

| Setting | Default | When to change it |
| --- | --- | --- |
| `XOMTRA_LOCAL_HOST` | the LAN address found at start | a DNS name (`xomtra.home.arpa`), or the address changes (DHCP) |
| `ADMIN_CIDR` | all private ranges | limit the admin GUI to your admin machine's network |
| `LAN_CIDR` | all private ranges | limit who may use the MCP endpoint |
| `XOMTRA_HA_URL`, `XOMTRA_HA_TOKEN` | entered in the admin GUI | non-interactive install (they win over the GUI) |
| `XOMTRA_ACCESS_MODE=public` | `local` | Claude connector on a public host, see [claude-connector.md](claude-connector.md) |

`./init-env.sh` asks these questions and writes `.env` for you. After changing `.env`,
run `docker compose up -d --force-recreate`: the address is part of the links of the
admin GUI and of the Home Assistant sign-in, and services that did not change otherwise
would keep the old one. The data, tokens and the Home Assistant connection stay (they
are in the volumes).

The address is found at every start. If the machine gets a new address from DHCP, or
you change `XOMTRA_LOCAL_HOST` later, run `docker compose up -d --force-recreate`. Caddy
issues the certificate for the new address from the same local CA, so clients that
trust the CA keep trusting the server; only the URL in the clients (MCP endpoint,
bookmarks) has to be updated.

## Backups

Back up the Docker volumes `secrets` (passwords and keys), `postgres-data` (catalog,
tokens, settings, the Home Assistant connection) and the lake (`seaweedfs-data`).
Without the volume `secrets` the databases and the stored Home Assistant token cannot
be opened. The simplest robust backup is a backup of
the whole virtual machine.

## Upgrading from a version with secrets in .env

Installations before ADR 077 keep their passwords in `.env` (written by the old
`init-env.sh`). On the first start of the new version `secrets-init` takes them over
into the volume `secrets`, so the Postgres roles and S3 keys keep matching
(`docker compose logs secrets-init` says "taken over from .env"). Afterwards the lines
`POSTGRES_PASSWORD`, `*_PG_PASSWORD`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`,
`XOMTRA_JWT_SIGNING_KEY` and `XOMTRA_ADMIN_SESSION_KEY` can be deleted from `.env`;
values in the volume always win. `XOMTRA_HA_URL` and `XOMTRA_HA_TOKEN` in `.env` keep
working; to move them to the admin GUI, delete them from `.env`, run
`docker compose up -d` and use the setup link.

## Uninstall

To remove xomtra with its data, images, the clients' extension and certificate and the
token in Home Assistant, see [uninstall.md](uninstall.md).
