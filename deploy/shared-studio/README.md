# Shared Studio deployment

This deployment gives a class one shared OptPilot login while keeping Studio,
OpenHands, Code Server, and Preview providers on loopback. Only the isolated
nginx TLS gateway binds the configured campus address. The authoritative design
and threat model are in [`designs/shared-studio-deployment.md`](../../designs/shared-studio-deployment.md).

## Authentication boundary

- Studio owns the password verifier and restart-persistent opaque sessions.
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

The shared account controls entry to the service; it is not a multi-user
authorization model. Workspace privacy inside Studio is therefore not promised.
The DEVS interface still uses its launch-scoped participant identity for its own
history view and sends durable records to the configured collector.

## Collector prerequisite

This deployment reuses the separate trusted `devs-gen-collector`; it does not
start or publish that administrative service. Keep the collector and `/admin`
on `127.0.0.1:8010`. The Docker-host URL in `DEVS_COLLECTOR_URL` lets the v2
interface submit records and bounded finalizer bundles, while
`DEVS_COLLECTOR_HEALTHCHECK_URL` lets preflight verify the host-loopback service.
Only the ingest token belongs in `deploy.env`; never grant the collector admin
token to Studio, OpenHands, or a Workspace. Rotate the ingest token after a
class or suspected disclosure.

## First setup

1. Copy `deploy.env.example` to `deploy.env`, fill every placeholder, and run
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
2. Confirm `ALLOWED_CIDRS` with the campus network operator. The example ranges
   are configuration examples, not an authoritative current network list.
3. Create the password verifier interactively:

   ```bash
   bash deploy/shared-studio/deploy.sh init-credentials students
   ```

   When using an IP-derived `nip.io` hostname, install Certbot and issue the
   certificate before preflight. The helper verifies DNS first and exposes
   only Certbot's bounded HTTP-01 responder on privileged port 80. When this
   account cannot bind port 80 directly, it uses a short-lived official
   Certbot container and removes it immediately afterward. Daily Studio
   operation remains on unprivileged HTTPS ports.

   ```bash
   brew install certbot
   bash deploy/shared-studio/issue_certificate.sh
   ```

   Certificate issuance must be repeated before expiry. A stable institutional
   hostname and centrally managed certificate remain preferable for long-term
   service; `nip.io` is the short-term classroom deployment option.

   If the campus edge blocks inbound port 80, HTTP-01 cannot succeed merely by
   changing dynamic-DNS providers. A locally generated certificate preserves
   encryption for a bounded test, but browsers will warn until its issuing CA
   is trusted. Do not weaken the shared-session Cookie or publish the service
   over plaintext HTTP as a workaround.

4. Run the complete preflight, then start:

   ```bash
   bash deploy/shared-studio/deploy.sh check
   bash deploy/shared-studio/deploy.sh start
   ```

The preflight installs the tracked DEVS Generator v2 template into
`$OPTPILOT_CATALOG_ROOT/$OPTPILOT_LOCAL_PACKAGE_NAME`. The default source name
is the stable, version-specific `devs_generator_v2`, avoiding collision with a
previously registered global Realm source named `local_package`. Its category
remains `local`, so it can create the executable Workspace runtime required by
the generator. The launcher also treats this private Catalog as its packages
root, so Studio publishes an immutable first revision instead of leaving a
non-editable filesystem import. It does not replace the upstream gallery
version. Runtime state, login sessions, logs, and the editable package remain
outside the Git checkout.

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
shared session Cookie out of gateway logs. Login POSTs are additionally
rate-limited per source address by nginx; Studio applies its own per-address
and global failed-login limits.

Rotate the shared password by writing a new credentials file and restarting
Studio. Existing browser sessions are stored in a separate SQLite database; to
force immediate logout after a password rotation, stop the service and move
that session database to a retained incident/archive directory before restart.
Do not delete evidence during incident response.

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
