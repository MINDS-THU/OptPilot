# Shared OptPilot Studio deployment

Status: implementation design for the `deployment/classroom` branch.

This document defines the smallest shared deployment that preserves the new
upstream Studio's local-runtime isolation while giving students one browser
login across Studio, Code Server, and generated web interfaces.

## Goals

- Publish the current Studio to one class without adding registration or
  per-student authorization.
- Ask for one shared login once per browser session, not once per port.
- Keep Studio, OpenHands, Workspace Code Server, and presentation listeners on
  loopback. Only Nginx may listen on a non-loopback address.
- Add the latest DEVS generator beside the upstream resource as an explicitly
  versioned resource.
- Fail closed: loss of the authentication service or an incomplete proxy
  configuration must deny remote access.
- Keep the upstream local-only workflow unchanged when shared deployment is
  disabled.

## Non-goals

- User registration, roles, tenant ownership, or private per-student Catalogs.
- Treating the shared account as an identity for grading or audit attribution.
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

### Versioned DEVS generator

Create the new resource from the upstream `devs-gen-interface` at OptPilot
commit `5094231`, then selectively merge the committed changes from
`/Users/minds/MINDS/devs-gen-interface-persistence-dev` commit `a2a3cd8` into:

```text
catalog/devs_gallery/resources/devs-gen-interface-v2/
```

Its public identifiers are:

```yaml
id: devs-gen-interface-v2
name: DEVS Simulation Generator Interface v2
interface:
  label: DEVS Generator v2
```

The existing upstream `devs-gen-interface` remains unchanged. A short
`SOURCE_SNAPSHOT.md` records the source repository and exact commit. The word
"Classroom" is not part of the new resource's ID, display name, or label.

The persistence source must not be copied wholesale over the upstream resource.
The upstream resource already carries newer OptPilot contracts that the
standalone development repository does not contain, including the
`devs.simulation.v2` manifest, metrics and policy outputs, event-trace
conformance, the `generate` resource action, and `headless_generate.py`. Those
remain authoritative. The v2 merge adds the persistence, participant identity,
collector, rating, telemetry, output repair, and remote-finalizer behavior
without regressing those contracts.

Collector configuration remains genuinely optional. A host variable declared
as a Resource grant is required by OptPilot, so optional collector variables
must not be added to the default launch grants unless the package contract
provides a separately selected managed launch profile. The first shared
deployment may deliberately make the collector required for v2, but that must
be stated in the resource description and preflight rather than presented as
optional.

### Authentication division of responsibility

Authentication is deliberately split rather than assigned exclusively to
Nginx or Studio:

- Studio owns the login page, password verification, opaque sessions, logout,
  expiry, and revocation.
- Nginx terminates TLS, applies the approved source-IP policy, and uses
  `auth_request` to require the same Studio session before forwarding every
  public Studio, Code Server, or presentation request.
- Nginx Basic Auth is not used. It has per-origin browser behavior and causes
  repeated prompts across ports.
- Workspace applications do not implement or receive the outer login secret.

The public gateway and the application check are both required. An application
check alone cannot protect Code Server or presentation processes on their own
ports. Nginx alone should not own the password database or browser UI because
Studio needs revocable sessions and explicit logout.

### Session form

Use a server-side opaque session, not a JWT.

- Cookie name: `__Host-optpilot_session`.
- Cookie attributes: `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`; no
  `Domain` attribute.
- The cookie value contains at least 256 random bits.
- Only a SHA-256 digest of the token is stored server-side.
- Default absolute lifetime: 12 hours, configurable with a bounded setting.
- Closing a tab does not log the browser out; explicit logout or expiry does.
- The shared username is informational. Every successful browser login receives
  a different session, allowing individual session revocation without claiming
  that it identifies a student.
- Session storage survives a Studio restart and lives in the private
  OS-local Studio project state, not in the source checkout.
- Expired sessions are deleted opportunistically. Last-seen writes are
  throttled so static assets do not create one database write per request.

The configured password is stored as a salted `scrypt` verifier. The plaintext
password is accepted only in the login request and is never written to logs,
settings, session storage, or command-line arguments.

### Code Server authentication

During implementation and proxy testing, Code Server remains in password mode.
That produces a second login and is therefore not the intended classroom
experience.

It may be changed to `auth none` only after all of these gates pass:

1. every Code Server container publishes only to `127.0.0.1`;
2. every presentation broker listener is loopback-only;
3. Nginx is the only non-loopback listener in the configured public ranges;
4. every public Studio, Code Server, and presentation location uses the same
   fail-closed `auth_request` verifier;
5. an invalid, expired, logged-out, or missing session is rejected on every
   port;
6. stopping Studio makes Nginx authorization fail rather than bypass;
7. deployment preflight verifies actual listener addresses and rendered Nginx
   coverage;
8. TLS and the approved campus/VPN source-address policy are active.

After those gates, the outer session is the Code Server authentication layer.
Leaving `auth none` reachable on any non-loopback socket is a release-blocking
error.

## Public and private addressing

Bind addresses and browser addresses are separate concepts.

Private listeners:

```text
Studio:       http://127.0.0.1:8866
OpenHands:    http://127.0.0.1:8781
Code Server:  http://127.0.0.1:18766+
Presentation: http://127.0.0.1:19766+
```

Browser-facing URLs use one configured HTTPS host while retaining the allocated
port for this iteration:

```text
https://studio.example.edu:8866/
https://studio.example.edu:18766/
https://studio.example.edu:19766/
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

Shared authentication is disabled unless a complete configuration is supplied.
When enabled, Studio adds:

| Endpoint | Method | Authentication | Purpose |
| --- | --- | --- | --- |
| `/login` | `GET` | public | Serve the login page. |
| `/api/auth/login` | `POST` JSON | public, same-origin | Verify the shared credential and create a session. |
| `/api/auth/session` | `GET` | optional | Report whether the current session is valid. |
| `/api/auth/logout` | `POST` JSON | required, same-origin | Revoke the current session and expire the cookie. |
| `/api/auth/verify` | `GET` | session cookie | Return `204` for Nginx `auth_request`, otherwise `401`. |

Login failures return one generic message. Passwords and Cookie values must be
redacted from all logs. Login attempts are rate-limited per source address and
globally. The first implementation uses process-local rate-limit state; Nginx
source limits remain a second layer.

When shared authentication is enabled, Studio itself also rejects unauthenticated
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
5. `204` permits proxying; `401` redirects a browser navigation to the main
   login page or returns an API unauthorized response;
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
unused, stale, or unrelated process in the configured range is denied. Because
all students use one shared account, this is deliberately service ownership
validation rather than per-student Workspace authorization: any authenticated
class participant may open any currently shared OptPilot Workspace URL.

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
logout use the same exact-origin rule and JSON-only bodies.

## Port allocation and coverage

The current upstream Workspace allocator searches 200 ports and the presentation
broker searches 1,000 ports. A static Nginx deployment must not silently expose
only part of either allocator's possible range.

The shared deployment introduces one explicit bounded port count used by both
allocation and Nginx rendering. Preflight checks that:

- the count can serve the intended class capacity;
- Code Server and presentation ranges do not overlap each other or Studio;
- every rendered public listener has `auth_request`;
- authorization accepts a dynamic listener only while Studio's process-local
  ownership registry identifies that exact active port;
- no backend listener uses a wildcard or non-loopback address;
- Nginx is not configured to forward OpenHands.

Only one active listener per Workspace or presentation is expected; opening a
large static range has operating-system and Nginx costs. The initial default is
200 ports in each range, subject to a measured class concurrency test before
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
PUBLIC_ORIGIN=https://studio.example.edu:8866
PUBLIC_BIND_IP=<approved interface address>
ALLOW_REMOTE_ACCESS=1
STUDIO_HOST=127.0.0.1
OPENHANDS_HOST=127.0.0.1
WORKSPACE_RUNTIME_HOST=127.0.0.1
WORKSPACE_RUNTIME_PORT_START=18766
PRESENTATION_PORT_START=19766
PUBLIC_RUNTIME_PORT_COUNT=200
SHARED_AUTH_USERNAME=optpilot
SHARED_AUTH_PASSWORD_HASH=<scrypt verifier>
SHARED_AUTH_SESSION_TTL_SECONDS=43200
SHARED_AUTH_SESSION_DB=<private path outside checkout>
```

Preflight refuses remote startup unless the public origin is HTTPS, backend
addresses are loopback, the password verifier is valid, the session database
parent is private, Nginx configuration tests successfully, and an explicit
remote-access flag is set.

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

Resource tests must additionally validate `devs-gen-interface-v2`, launch its
prepared runtime, generate a small model, run the generated output using the
declared originating runtime action, open its presentation, and confirm
collector/finalizer configuration is passed only through declared grants. They
also verify the merged resource retains `devs.simulation.v2`, metrics, policy,
event-trace conformance, and the headless `generate` action.

## Rollout and rollback

1. Import and validate `devs-gen-interface-v2` without changing deployment.
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
remains available independently of v2.
