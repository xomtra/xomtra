# Uninstall the Docker Compose installation

How to remove xomtra installed with [Docker Compose](docker.md) completely: the server,
its data and images, the clients' extension and certificate, and the token in Home
Assistant. xomtra never changed anything in Home Assistant itself, so nothing else has
to be undone there. (The Home Assistant add-on is removed in Home Assistant: Settings →
Add-ons → xomtra → Uninstall.)

> **This cannot be undone.** Step 2 deletes the history collected by xomtra (the lake),
> its catalog and settings, the issued tokens, the internal passwords and keys (volume
> `secrets`) and the local certificate authority (volume `caddy-data`). Without the
> volume `secrets` a backup of the databases cannot be opened, and a new installation
> creates a new CA, so clients have to trust it again. Home Assistant keeps its own
> history; only xomtra's copy is deleted. If you might come back, make the backup in
> step 1 first.

Run the commands on the server in `xomtra/deploy/compose` (the directory where you ran
`docker compose up`). The lite edition needs the same `-f` options as at the start
(not needed if `.env` sets `COMPOSE_FILE=compose.yaml:compose.lite.yaml`), otherwise
Compose does not see its volume `lake-data` and its `worker`.

## 1. Back up the volumes (optional)

Stop the stack so that the databases are consistent, then pack every volume of the
project into `~/xomtra-backup`:

```bash
docker compose stop        # lite: docker compose -f compose.yaml -f compose.lite.yaml stop
mkdir -p ~/xomtra-backup
for v in $(docker volume ls -q --filter label=com.docker.compose.project=xomtra); do
  docker run --rm -v "$v":/v:ro -v ~/xomtra-backup:/b alpine \
    tar czf "/b/$v.tgz" -C /v .
done
cp .env ~/xomtra-backup/ 2>/dev/null   # if you have one
ls -lh ~/xomtra-backup
```

The archive `xomtra_secrets.tgz` contains every internal password and key, and `.env`
may contain the Home Assistant token: keep the backup like a password, not in a shared
folder. Afterwards `docker rmi alpine` removes the helper image.

## 2. Remove the server

```bash
# full edition
docker compose down -v --rmi all --remove-orphans
# lite edition
docker compose -f compose.yaml -f compose.lite.yaml down -v --rmi all --remove-orphans
```

This stops and deletes the containers and the networks of the project `xomtra`, its
volumes (`-v`: `secrets`, `postgres-data`, `seaweedfs-data` or `lake-data`,
`caddy-data`, `caddy-config`, `mcp-data`, `ingest-data`, in Docker named `xomtra_…`)
and the images it used (`--rmi all`: `ghcr.io/xomtra/xomtra-*`, `postgres`,
`chrislusf/seaweedfs`). An image that another container on this machine still uses is
kept, and Docker says so.

Check that nothing is left, then delete the downloaded release (with `.env`) and unused
image layers:

```bash
docker ps -a --filter label=com.docker.compose.project=xomtra       # empty
docker volume ls --filter label=com.docker.compose.project=xomtra   # empty
cd ../../.. && rm -rf xomtra                                         # the cloned directory
docker image rm $(docker images -q ghcr.io/sigstore/cosign/cosign) 2>/dev/null   # cosign container, if used
docker image prune -f
```

If you reserved an address or added a DNS name (`XOMTRA_LOCAL_HOST`) in your router or
DNS server for xomtra, remove that entry too.

## 3. Clients

On every computer that used xomtra:

- **Claude Desktop:** Settings → Extensions → the extension from the admin GUI
  (shown as xomtra) → Uninstall. Delete the downloaded `xomtra.mcpb`.
- **Claude Code:** the server may have any name you gave it in `claude mcp add`. Find
  the one with the address of your server, then remove it by that name:

  ```bash
  claude mcp list                  # find the server with https://<local-host>/mcp
  claude mcp remove <name>         # add -s user or -s project if you added it with that scope
  ```

  Remove `NODE_EXTRA_CA_CERTS` from your shell profile if you set it for xomtra.
- **Other MCP clients:** delete the entry with the address of your server
  (`https://<local-host>/mcp`) from their configuration, whatever its name.
- **The CA certificate**, if you added it to the computer's trust store
  ([local access, step 2](local-access.md#2-trust-the-servers-certificate)). The
  certificate is named `xomtra local CA - <year> ECC Root`. Remove only this one:

  ```bash
  # macOS: show the SHA-1 hash of the certificate, then delete it by that hash
  security find-certificate -a -c "xomtra local CA" -Z /Library/Keychains/System.keychain \
    | grep -E 'SHA-1|"labl"'
  sudo security delete-certificate -Z <sha1-hash> /Library/Keychains/System.keychain
  # Windows (PowerShell as administrator): show it, then delete it by its hash
  certutil -store Root "xomtra local CA"
  certutil -delstore Root <cert-hash>
  # Debian/Ubuntu
  sudo rm /usr/local/share/ca-certificates/xomtra-ca.crt
  sudo update-ca-certificates --fresh
  ```

  Browsers with their own certificate store (Firefox) keep an imported CA in their
  settings (Privacy & Security → Certificates → View Certificates → Authorities).
  Delete the downloaded `xomtra-ca.crt` as well. If you only used
  `NODE_EXTRA_CA_CERTS` or the certificate in the extension, there is nothing to
  remove from the system.

## 4. Home Assistant

- **Token of xomtra:** sign in to Home Assistant as the user whose long-lived access
  token you entered during the setup, open its profile → Security → Long-lived access
  tokens and delete the token for xomtra. Until you do, the token stays valid even
  though xomtra is gone.
- **User for xomtra:** if you created a user only for xomtra, delete it in Settings →
  People → Users.
- Sign-in with your Home Assistant account leaves nothing to delete: xomtra revokes the
  Home Assistant token right after the sign-in ([ha-login.md](ha-login.md)).

## 5. Public setup (external Claude connector)

If xomtra ran in the public mode ([claude-connector.md](claude-connector.md)):

- remove the connector in claude.ai (Settings → Connectors);
- delete the port forward of 443 to the server in your router and the DNS record of
  `<public-host>`;
- if you used Entra ID: delete the app registration in the Entra admin center
  ([entra-app-registration.md](entra-app-registration.md)), which also invalidates its
  client secret.
