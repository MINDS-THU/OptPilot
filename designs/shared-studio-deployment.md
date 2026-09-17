# Shared OptPilot Studio deployment

Status: implemented by the shared Studio deployment profile.

This document defines the smallest shared deployment that preserves the new
upstream Studio's local-runtime isolation while giving each student one browser
login across Studio, Code Server, and generated web interfaces.

## Goals

- Publish the current Studio to one class with invite-only student registration.
- Ask for one account login, not a separate password on every dynamic port.
- Keep student Conversations, Workspaces, work records, and dynamic runtime
  access private to the creating account while allowing the admin to inspect all.
- Keep Studio, OpenHands, Workspace Code Server, and presentation listeners on
  loopback. Only Nginx may listen on a non-loopback address.
- Add the latest DEVS generator beside the upstream resource as an explicitly
  versioned resource.
- Fail closed: loss of the authentication service or an incomplete proxy
  configuration must deny remote access.
- Keep the upstream local-only workflow unchanged when shared deployment is
  disabled.

## Non-goals

- Email verification, password recovery, self-service admin registration, or
  private per-student Catalog copies.
- Treating a classroom username as verified legal identity.
- Making arbitrary Studio deployments safe merely by binding them to
  `0.0.0.0`.
- Replacing OptPilot's Realm, Workspace, or presentation ownership checks.
- Hiding all internal ports behind path routing in this iteration.

## Existing upstream boundary

Upstream Studio is a single-user localhost application:

- Studio defaults to `127.0.0.1:8765`.
- the bundled service launcher uses `127.0.0.1:8866` for Studio and
  `127.0.0.1:8781` for OpenHands;
- each Workspace maps container Code Server port `8766` to a distinct
  loopback host port starting at `18766`;
- Workspace presentations use a separate, token-protected loopback origin
  starting at `19766`;
- Code Server authentication defaults to `none` and is safe only while its
  listener remains loopback-only;
- Studio mutation protection is a local anti-CSRF capability, not login or
  remote authorization;
- browser-facing URLs currently use the loopback bind address, so a remote
  browser cannot use them as published.

The shared deployment must extend this boundary. It must not weaken the
provider ownership checks in the Workspace runtime or presentation broker.

## Decisions

### Disjoint working and private roots

The mountable Studio working root and provider-private deployment root are
disjoint directories outside the source checkout. Credentials, session
storage, TLS keys, installed Catalog templates, runtime ownership records,
OpenHands state, logs, and gateway configuration remain under the private
root. Preflight rejects either root containing the other and requires both to
have mode `700`. This prevents the generic Code Server workspace path from
mounting deployment authority.

### Versioned DEVS generator

The shared deployment ships one tracked generator template:

```text
deploy/shared-studio/local-package-resources/devs-gen-interface-v3/
```

Deployment copies that template to
`<private-root>/catalog/<package-name>/resources/devs-gen-interface-v3/`,
removes the retired v2 source template from that deployment-owned package, and
creates package metadata when absent. It does not remove immutable Realm
revisions or user Workspaces. The launcher exports the Catalog directory as
`OPTPILOT_PACKAGES_ROOT`, so Studio publishes the first immutable revision at
startup and enables editable Workspace creation. The interface then receives
the Workspace runtime needed to execute generated simulations. The template
path in the source checkout is never indexed alongside the installed copy.

The default package name remains `devs_generator_v2` as a deployment identity
for compatibility with existing Catalog and Realm records. It is not the
resource version. Renaming it would create a distinct source and therefore
requires an explicit data migration. The package still declares category
`local`, preserving editable Workspace and runtime behavior.

Its public identifiers are:

```yaml
id: devs-gen-interface-v3
name: DEVS Simulation Generator Interface v3
interface:
  label: DEVS Generator v3
```

A short `SOURCE_SNAPSHOT.md` records the source history. The v3 template retains
the `devs.simulation.v2` manifest, metrics and policy outputs, event-trace
conformance, headless `generate` action, participant identity, ratings,
telemetry, and output repair contracts. Its default automatic check runs Pi
with DeepSeek V4.1 Flash inside the prepared Interface runtime.

The collector is a separate host-loopback data service and is not published by
this Nginx configuration. Collector URL and ingest token are optional and used
only for asynchronous durable reporting; preflight accepts all collector values
being absent. Its administrator token is never granted to Studio or a
Workspace. The ingest token cannot administer or publish records and should be
rotated after a class or suspected disclosure. Model checking is not a
collector responsibility and does not depend on collector availability.

### Reproducible inputs and lifecycle

The shared profile fixes the inputs that run student code. The Workspace image
uses an exact code-server release and multi-architecture image digest. Node and
uv are exact versions with checked SHA-256 downloads, and the built image carries
a revision label derived from the Dockerfile and base image. V3 keeps reviewed
direct requirements separately from a fully pinned universal Python 3.10+
closure. OpenHands uses its own exact requirements file and virtual environment.

Deployment has three distinct phases:

1. source preflight validates configuration and copies Catalog sources only to
   a temporary directory;
2. activation copies those sources into the deployment-owned Catalog after the
   current Studio has stopped;
3. deployed preflight checks the installed package, prepared image, certificate,
   and a temporary nginx rendering before services start.

The public `check` command runs only the third, non-mutating validation. The
public `prepare` command refuses to activate while Studio is running. `start`
and `restart` do slow image preparation before stop, then activate and check.
An image build or source validation failure therefore leaves the current service
running, while the live Catalog and nginx configuration are never rewritten as
a side effect of `check`.

### Authentication division of responsibility

Authentication is deliberately split rather than assigned exclusively to
Nginx or Studio:

- Studio owns the login page, password verification, opaque sessions, logout,
  expiry, and revocation.
- Nginx terminates TLS, applies the approved source-IP policy, and uses
  `auth_request` to require the same Studio session before forwarding every
  public Studio, Code Server, or presentation request.
- Nginx logs request methods and normalized paths but omits query strings and
  Cookies, so launch tokens and account sessions are not persisted in logs.
- Nginx Basic Auth is not used. It has per-origin browser behavior and causes
  repeated prompts across ports.
- Workspace applications do not implement or receive the outer login secret.

The public gateway and the application check are both required. An application
check alone cannot protect Code Server or presentation processes on their own
ports. Nginx alone should not own the password database or browser UI because
Studio needs revocable sessions and explicit logout.

### Account and session form

Use a server-side opaque session, not a JWT.

- Cookie name: `__Host-optpilot_session`.
- Cookie attributes: `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`; no
  `Domain` attribute.
- The cookie value contains at least 256 random bits.
- Only a SHA-256 digest of the token is stored server-side.
- Default absolute lifetime: 7 days, configurable with a bounded setting.
- Closing a tab does not log the browser out; explicit logout or expiry does.
- The reserved `admin` account cannot be registered. Its password is read from
  `OPTPILOT_ADMIN_PASSWORD` at process startup, hashed in memory, removed from
  Studio's environment, and never stored in the account database. A restart
  invalidates prior admin sessions.
- Students register a unique username and password using one class invitation
  code. `OPTPILOT_REGISTRATION_ENABLED=0` closes registration without disabling
  existing accounts. The invitation is a random shared secret, not a per-user
  token, and has no email or redemption table.
- Student password verifiers, opaque sessions, and asset ownership survive a
  Studio restart in private `CLASSROOM_AUTH_DB`, not the source checkout.
- Expired sessions are deleted opportunistically. Last-seen writes are
  throttled so static assets do not create one database write per request.

Student passwords are stored as salted `scrypt` verifiers. Plaintext passwords
and invitation codes are accepted only at the authentication boundary and are
never written to logs, browser storage, or command-line arguments.

### Account-owned assets

Studio keeps one small ownership registry in `CLASSROOM_AUTH_DB`. Newly created
Conversations, Workspaces, saved Study drafts, Study launch requests, Runs,
Operator Jobs, Resource actions, Interface launches, content-view handles, and
transient runtime Workspaces are claimed by the authenticated account. Student
list and exact-read/write paths apply the same ownership check. The admin may
read and manage all records. Unclaimed records from an older deployment are
admin-only rather than silently assigned to the first student who sees them.
Student-created blank Workspaces cannot name host paths, and opening the same
Catalog entry derives a separate Workspace/runtime coordinate for each account.

The same check is used by Nginx authorization for dynamic ports: Studio resolves
the live Code Server or presentation port to its provider-owned Workspace, Run,
Operator Job, or Interface runtime and then verifies the requesting account.
Generated applications still do not receive the outer login Cookie.

### Code Server authentication

This deployment uses Code Server `auth none`; retaining its per-instance
password would reintroduce a second login on every dynamic port and defeat the
shared-login experience. `auth none` is allowed only while all of these gates
pass:

1. every Code Server container publishes only to `127.0.0.1`;
2. every presentation broker listener is loopback-only;
3. Nginx is the only non-loopback listener in the configured public ranges;
4. every public Studio, Code Server, and presentation location uses the same
   fail-closed `auth_request` verifier;
5. an invalid, expired, logged-out, or missing session is rejected on every
   port;
6. stopping Studio makes Nginx authorization fail rather than bypass;
7. deployment preflight verifies configured bind addresses and rendered Nginx
   coverage, while startup verifies the main listeners it actually created;
8. TLS and the approved campus/VPN source-address policy are active.

After those gates, the outer session is the Code Server authentication layer.
Leaving `auth none` reachable on any non-loopback socket is a release-blocking
error.

### Review findings and accepted limitations

The independent reviews rejected several attractive but unrealistic claims:

- Registration proves possession of the class invitation, not a student's
  institutional identity. A student can share credentials or choose a misleading
  username; grading must not rely on the username alone.
- The configured port count is routing capacity and a coarse upper bound, not
  proof that the Mac can sustain that many simultaneous model generations.
  Per-container CPU, memory, and PID limits contain one Workspace; measured
  classroom load and host monitoring are still required before raising the
  range.
- One public port with path routing would reduce firewall surface, but safely
  adapting Code Server base paths, redirects, WebSockets, and fresh preview
  origins is a larger change. This version keeps bounded TLS ports and applies
  the same login plus active-port ownership check to every one.
- An established WebSocket outlives the HTTP authorization decision. Logout
  blocks all new requests immediately; an operator must stop the Workspace or
  reload Nginx to terminate an existing Code Server WebSocket immediately.
- The collector ingest token is visible to authorized Workspace code by design.
  It can submit records but cannot inspect, administer, publish, or execute
  model code. Treat it as a class-scoped capability and rotate it accordingly.

## Public and private addressing

Bind addresses and browser addresses are separate concepts.

Private listeners:

```text
Studio:       http://127.0.0.1:28666
OpenHands:    http://127.0.0.1:28681
Code Server:  http://127.0.0.1:28766+
Presentation: http://127.0.0.1:29766+
```

Browser-facing URLs use one configured HTTPS host while retaining the allocated
port for this iteration:

```text
https://studio.example.edu:28666/
https://studio.example.edu:28766/
https://studio.example.edu:29766/
```

Studio and the presentation broker therefore need separate bind and public URL
settings. Health probes always use the private loopback URL. Browser payloads
use the public scheme and host. Public host input is parsed as a host name or IP,
not interpolated into shell commands.

The trusted-proxy mode is opt-in. It is valid only when Studio itself is bound
to loopback and the direct peer is loopback. In that mode Studio accepts the
configured public origin and narrowly trusts Nginx's normalized
`X-Forwarded-Proto` and `X-Forwarded-Host`. Arbitrary forwarded values and
unconfigured origins remain rejected.

## HTTP endpoints

Classroom authentication is disabled unless a complete configuration is supplied.
When enabled, Studio adds:

| Endpoint | Method | Authentication | Purpose |
| --- | --- | --- | --- |
| `/login` | `GET` | public | Serve the login page. |
| `/register` | `GET` | public while enabled | Serve invite-only registration. |
| `/api/auth/register` | `POST` form or JSON | public while enabled, same-origin | Create one student account and session. |
| `/api/auth/login` | `POST` form or JSON | public, same-origin | Verify an account and create a session. |
| `/api/auth/session` | `GET` | optional | Report whether the current session is valid. |
| `/api/auth/logout` | `POST` | required, same-origin | Revoke the current session and expire the cookie. |
| `/api/auth/verify` | `GET` | session cookie | Return `204` for Nginx `auth_request`, otherwise `401`. |

Login failures return one generic message. Passwords and Cookie values must be
redacted from all logs. Login attempts are rate-limited per source address and
globally. The first implementation uses process-local rate-limit state; Nginx
source limits remain a second layer.

When classroom authentication is enabled, Studio itself also rejects unauthenticated
requests except the login endpoints and a narrowly scoped local health check.
This protects the main application from an accidental Nginx location omission.

## Nginx request flow

For every public listener:

1. apply the approved IP allowlist;
2. accept TLS only;
3. invoke an internal Nginx location with `auth_request`;
4. the internal location calls Studio `/api/auth/verify` over loopback and
   forwards only the browser Cookie, normalized request metadata, target kind,
   and public listener port;
5. `204` permits proxying; the main Studio page redirects an unauthenticated
   navigation to login, while APIs and dynamic ports return denial responses;
6. any timeout, connection refusal, or malformed verifier response denies the
   request.

The login page itself is exposed only on the main Studio listener and still
inherits TLS and source-IP restrictions.

Nginx must never forward `__Host-optpilot_session` into a Workspace. For direct
Code Server requests in final `auth none` mode, it strips the complete Cookie
header. Presentation requests keep the broker's launch-scoped presentation
Cookie; the trusted presentation broker removes both its own private token and
the outer OptPilot session before contacting the generated application.

Authentication is not sufficient proof that a dynamic port belongs to
OptPilot. For Code Server and presentation listeners, `/api/auth/verify` also
checks that the requested `$server_port` is currently registered to a live
Workspace Code Server or presentation lease. An authenticated request to an
unused, stale, unrelated, or differently owned process in the configured range
is denied. Students cannot open another account's active Workspace URL merely
by learning its port.

Generated applications must also be unable to replace the outer login Cookie
with a `Set-Cookie` response. The presentation broker treats
`__Host-optpilot_session` as a private Cookie name and removes it in both
request and response directions. The direct Code Server proxy suppresses
upstream Cookies once Code Server runs behind outer authentication only.

OpenHands is never assigned a public Nginx listener.

## Origin and CSRF checks

Login does not replace the existing mutation token.

- Nginx authentication answers "may this browser reach the service?"
- Studio's mutation token and Origin checks answer "did this mutation come from
  the loaded Studio page?"

The configured public origin is exact: scheme, host, and explicit port must
match. Nginx overwrites rather than appends trusted proxy headers. Studio trusts
them only from a loopback peer while trusted-proxy mode is enabled. Login and
logout use the same exact-origin rule; login accepts only a bounded HTML form
or JSON body.

## Port allocation and coverage

The original upstream Workspace allocator and presentation broker searched
different-sized ranges. A static Nginx deployment must not silently expose only
part of either allocator's possible range.

The shared deployment introduces one explicit bounded port count used by both
allocation and Nginx rendering. Preflight checks that:

- the configured count is positive, bounded by TCP limits, and does not cause
  range overlap (the operator still chooses it for expected class capacity);
- Code Server and presentation ranges do not overlap each other or Studio;
- every rendered public listener has `auth_request`;
- authorization accepts a dynamic listener only while Studio's process-local
  ownership registry identifies that exact active port (live ownership may be
  cached for no more than two seconds to collapse bursts of asset requests,
  while managed runtime record changes invalidate it immediately);
- no backend listener uses a wildcard or non-loopback address;
- Nginx is not configured to forward OpenHands.

Only one active listener per Workspace or presentation is expected; opening a
large static range has operating-system and Nginx costs. The initial default is
110 ports in each range, subject to a measured class concurrency test before
deployment.

A single public `443` router with paths or per-Workspace subdomains would avoid
opening a static port range. It is not the first implementation because Code
Server, WebSocket, base-path, redirect, and fresh-origin behavior would need a
larger routing change. Port ownership authorization closes the unrelated-port
exposure in the bounded-port design without weakening the existing presentation
lease checks. Moving to one public origin remains a later deploy-layer change.

## Deployment configuration

Machine-specific configuration and secrets stay outside Git. The committed
example contains names and safe defaults only. Required shared-deployment values
include:

```text
PUBLIC_HOST=studio.example.edu
PUBLIC_BIND_IP=<approved interface address>
PUBLIC_SERVER_NAME=studio.example.edu
STUDIO_PORT=28666
STUDIO_HOST=127.0.0.1
OPENHANDS_HOST=127.0.0.1
WORKSPACE_RUNTIME_HOST=127.0.0.1
WORKSPACE_RUNTIME_PORT_START=28766
WORKSPACE_RUNTIME_PORT_COUNT=110
PREVIEW_PORT_OFFSET=1000
SHARED_AUTH_CREDENTIALS_FILE=<private scrypt-verifier file>
SHARED_AUTH_SESSION_TTL_SECONDS=43200
OPTPILOT_STATE_ROOT=<private mode-700 path outside checkout>
```

Preflight refuses remote startup unless the public origin is HTTPS, backend
addresses are loopback, the password verifier is valid, the session database
parent is private, the public bind is one specific non-backend address, and
Nginx configuration tests successfully.

## Failure behavior

| Failure | Required behavior |
| --- | --- |
| Studio/auth verifier stopped | Nginx returns an error or unauthorized response; it never proxies through. |
| Session expired/revoked | All new HTTP requests and WebSocket handshakes are denied. |
| Nginx stopped | No service remains reachable on a non-loopback listener. |
| Code Server container binds incorrectly | Preflight/startup fails and Nginx is not started. |
| Public origin is wrong | Startup fails before serving rather than generating loopback or mixed-content links. |
| Presentation lease ends or port is reused | Existing provider ownership validation rejects the request. |
| Session database unavailable | Authentication fails closed; login and verification do not create an in-memory bypass. |

An already-established WebSocket cannot be re-authenticated by Nginx after its
HTTP upgrade. The existing presentation broker continues to revalidate endpoint
ownership, and the deployment sets bounded idle/lifetime timeouts. Revoking a
session immediately blocks new connections; terminating all already-open Code
Server WebSockets requires stopping the affected Workspace or reloading the
gateway and is an explicit operator action.

## Verification plan

Automated tests must cover:

- password verifier parsing and constant-time validation;
- session creation, digest-only persistence, expiry, throttled touch, logout,
  restart persistence, and concurrent reads;
- generic and rate-limited login failure responses;
- Cookie attributes and absence of plaintext credentials in responses/logs;
- exact public-origin handling and rejection of spoofed forwarded headers;
- unauthenticated denial and authenticated success for Studio APIs;
- Nginx rendering for Studio, every Code Server port, and every presentation
  port, with no OpenHands listener;
- dynamic-port authorization permits only ports held by a live Code Server or
  presentation lease and denies an unrelated loopback process in the range;
- fail-closed behavior when `/api/auth/verify` is unavailable;
- Cookie stripping at Code Server and presentation upstream boundaries;
- remote browser URLs use the public origin while health probes stay loopback;
- WebSocket handshake succeeds with a valid session and fails without it;
- every runtime port published by Docker is loopback-only.

Resource tests must additionally validate `devs-gen-interface-v3`, launch its
prepared runtime, generate a small model, run the generated output using the
declared originating runtime action, open its presentation, and confirm
collector configuration is passed only through declared grants. They also
verify the resource retains `devs.simulation.v2`, metrics, policy, event-trace
conformance, and the headless `generate` action.

## Rollout and rollback

1. Install and validate `devs-gen-interface-v3` in the deployment's private
   local Catalog package without changing the existing gallery resource.
2. Implement shared auth disabled by default and run upstream tests.
3. Start Studio, Code Server, and presentations on loopback only.
4. test Nginx on a separate local/public port set with Code Server password
   still enabled;
5. verify all authentication and listener gates, including service failure;
6. switch Code Server to `auth none` only for the fully gated deployment and
   rerun the entire matrix;
7. perform bounded concurrency and resource tests before classroom use.

Rollback stops the new Nginx instance first. Because all upstream services are
loopback-only, stopping Nginx removes remote reachability immediately. The
previous deployment is not overwritten, and the upstream `devs-gen-interface`
remains available independently of v3.
