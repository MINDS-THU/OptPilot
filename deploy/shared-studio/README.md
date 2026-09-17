# Shared Studio deployment

This deployment gives a class invite-only OptPilot accounts while keeping Studio,
OpenHands, Code Server, and Preview providers on loopback. Only the isolated
nginx TLS gateway binds the configured campus address. The authoritative design
and threat model are in [`designs/shared-studio-deployment.md`](../../designs/shared-studio-deployment.md).

## Authentication boundary

- Studio owns student password verifiers, restart-persistent opaque sessions,
  and private asset ownership in one SQLite database under the private root.
- The `admin` account cannot be registered. Its password is supplied as
  `OPTPILOT_ADMIN_PASSWORD` when Studio starts and is not stored in the account
  database. Student registration can be opened or closed independently.
- nginx does not use Basic Auth. It asks Studio to authorize every Studio,
  Code Server, and Preview request with `auth_request`.
- A valid login alone cannot expose an arbitrary process in a configured port
  range. Studio must also confirm that a live managed Code Server or Preview
  lease owns that exact port.
- The outer login Cookie is stripped before Code Server and generated
  applications. OpenHands has no public nginx listener.
- Code Server uses `--auth none` in this one-login deployment. That is safe only
  while all loopback/listener, TLS, allowlist, `auth_request`, and port-ownership
  preflight checks pass. Do not launch it this way outside this deployment.

New Conversations, Workspaces, background Resource actions, Interface launches,
and their dynamic Code Server/Preview ports are bound to the creating account.
Unclaimed data from an older shared-login deployment is visible only to the
admin. The DEVS interface still uses its launch-scoped participant identity for
its own history view and sends durable records to the configured collector.

New Catalog items published by an account start private. Their owner or the
admin can make an item public to every signed-in OptPilot account, or make it
private again, from the Catalog detail view. This visibility belongs to the
logical item across its immutable revisions; it never creates anonymous access
and does not turn an already running Interface into a shared Interface. Existing
Catalog items without an ownership record remain visible for compatibility and
can be adopted or privatized only by the admin. Visibility changes are retained
in the classroom account database for local auditing.

`INTERFACE_MAX_ACTIVE_PER_ACCOUNT` defaults to `1`. A live Interface keeps
application memory that cannot yet be reconstructed after its container stops,
so Studio does not idle-suspend it. A second launch is rejected with the
account's live Interface list and explicit Stop actions. Ordinary Workspace
runtimes keep their separate idle timeout and active-container limit.

## Optional collector integration

This deployment reuses the separate trusted `devs-gen-collector`; it does not
start or publish that administrative service. Keep the collector and `/admin`
on `127.0.0.1:8010`. The Docker-host URL in `DEVS_COLLECTOR_URL` lets the
interface submit durable records; the host-loopback URL in
`DEVS_HEADLESS_COLLECTOR_URL` does the same for Assistant actions, while
`DEVS_COLLECTOR_HEALTHCHECK_URL` lets preflight verify the host-loopback service.
Set both collector URLs, the healthcheck URL, and ingest token together, or
leave all four empty. Generation and the v3 Pi automatic check do not depend
on it; optional reporting failures stay out of user-facing action output.
Only the ingest token belongs in `deploy.env`; never grant the collector admin
token to Studio, OpenHands, or a Workspace. Rotate the ingest token after a
class or suspected disclosure.

## First setup

1. Prepare the source checkout and the dedicated OpenHands environment. The
   deployment uses the repository `.venv` for Studio and a separate pinned
   environment for OpenHands so upgrading either one cannot silently change
   the other:

   ```bash
   uv sync --all-packages --frozen
   uv venv --python 3.12 ~/.local/share/optpilot/openhands-venv-1.40.1
   uv pip install \
     --python ~/.local/share/optpilot/openhands-venv-1.40.1/bin/python \
     -r deploy/shared-studio/requirements-openhands.txt
   ```

   Set `OPENHANDS_AGENT_SERVER_BIN` to the resulting `agent-server` executable.
   Install nginx and a working Docker-compatible runtime before continuing;
   preflight checks both rather than installing host software itself.

2. Copy `deploy.env.example` to `deploy.env`, fill the required values, and run
   `chmod 600 deploy.env`. Use a certificate valid for `PUBLIC_HOST`. Keep
   `OPTPILOT_STATE_ROOT` (the mountable Studio working tree) disjoint from
   `OPTPILOT_PRIVATE_ROOT` (credentials, TLS keys, Catalog templates, runtime
   ownership records, logs, and nginx configuration). Preflight rejects nested
   or overlapping roots so opening the Studio root in Code Server cannot expose
   deployment authority.
   `PUBLIC_BIND_IP` must be one specific address assigned to this Mac; it must
   not be `0.0.0.0` or the `127.0.0.1` address used by private backends.
   Keep the certificate and state paths free of whitespace because they are
   rendered into an isolated nginx configuration.
3. Confirm `ALLOWED_CIDRS` with the campus network operator. The example ranges
   are configuration examples, not an authoritative current network list.
4. Set a strong `OPTPILOT_ADMIN_PASSWORD` in `deploy.env`. To open student
   registration, generate one random class invitation code:

   ```bash
   bash deploy/shared-studio/deploy.sh generate-invitation
   ```

   Put the output in `OPTPILOT_INVITATION_CODE` and set
   `OPTPILOT_REGISTRATION_ENABLED=1`. Both secrets must contain at least 12
   characters. Set the registration switch back to `0` and restart after the
   class has registered; existing accounts continue to work. Changing the
   startup admin password and restarting invalidates all existing admin
   sessions, without affecting student accounts.

   Install Certbot and issue the certificate before preflight. HTTP-01 uses a
   bounded standalone responder on privileged port 80. When inbound port 80 is
   unavailable, a direct `duckdns.org` hostname can instead use DNS-01 with a
   DuckDNS token stored under the private root at mode 600. The token is mounted
   read-only into the short-lived official Certbot container and is never put
   in a Workspace, command argument, or deployment log.

   ```bash
   brew install certbot
   bash deploy/shared-studio/issue_certificate.sh
   ```

   Set `CERTIFICATE_AUTO_RENEW_ENABLED=1` to install a daily user-level launchd
   check. It retains an unexpired certificate and reloads nginx only after the
   deployed certificate changes. A stable institutional hostname and centrally
   managed certificate remain preferable for long-term service; DuckDNS is the
   short-term classroom deployment option when DNS-01 is required.

   If the campus edge blocks inbound port 80, HTTP-01 cannot succeed merely by
   changing dynamic-DNS providers. DuckDNS works here because its TXT API
   enables DNS-01, which needs outbound HTTPS only. Do not weaken the shared
   session Cookie or publish the service over plaintext HTTP as a workaround.

5. Run the complete preflight, then start:

   ```bash
   bash deploy/shared-studio/deploy.sh check
   bash deploy/shared-studio/deploy.sh start
   ```

The preflight copies the tracked example packages into
`$OPTPILOT_CATALOG_ROOT` and installs only the DEVS Generator v3 template into
`$OPTPILOT_CATALOG_ROOT/$OPTPILOT_LOCAL_PACKAGE_NAME`. It also removes the
retired v2 template from that deployment-owned source directory; already saved
Workspaces and immutable Realm revisions are not modified. Packages named in
`OPTPILOT_SOURCE_CATALOG_EXCLUDES` are omitted. The default source name remains
`devs_generator_v2` solely as a compatibility identity for existing Realm and
Catalog records; it does not mean the v2 resource is installed. Changing that
name creates a distinct source and must be handled as a separate migration.
The package category remains `local`, so it can create the executable Workspace
runtime required by the generator. The launcher treats this deployment-owned
Catalog as its packages root, so Studio publishes immutable first revisions
instead of showing non-editable filesystem imports. Runtime state, login
sessions, logs, and all user Workspaces remain outside the Git checkout.

## Routine operations

```bash
bash deploy/shared-studio/deploy.sh status
bash deploy/shared-studio/deploy.sh logs 100
bash deploy/shared-studio/deploy.sh restart
bash deploy/shared-studio/deploy.sh stop
```

`stop` only uses PID files under the configured state root and the isolated
nginx PID. On macOS, Studio and OpenHands run as private user-level launchd
jobs so they survive the launching terminal and restart after an unexpected
exit; `stop` unloads those jobs before checking their PID files. It does not
kill an unrelated process merely because a port is in use. If preflight finds
an occupied managed port, resolve that conflict before starting.

The nginx access log records the request method and normalized path, but omits
query strings and Cookies. This keeps launch-scoped Preview tokens and the
account session Cookie out of gateway logs. Login and registration POSTs are additionally
rate-limited per source address by nginx; Studio applies its own per-address
and global failed-login limits.

Rotate the admin password by changing `OPTPILOT_ADMIN_PASSWORD` and restarting
Studio. The restart invalidates prior admin sessions. Student sessions and
account-owned work remain in `CLASSROOM_AUTH_DB`; retain that database during
backup or incident response.

## What remains private

The following must listen only on loopback:

- Studio (`STUDIO_HOST`);
- OpenHands (`OPENHANDS_HOST`);
- every Docker-published Workspace Code Server port;
- every Studio Preview broker port.

Verify the actual listeners after every deployment rather than relying only on
configuration. nginx is the only intended non-loopback listener, and its
configuration must include TLS, the source-IP allowlist, and Studio-backed
authorization on every published port.
