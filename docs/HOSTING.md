# Hosted friends beta

The hosted portal is a separate deployment from the local prototype. Vercel serves
`web/`; an always-on Agent37 control-plane VM runs `harness.hosting.server`. Each
invited user receives one isolated tenant VM only when starting a session. Existing
local and cloud-coding installations are never adopted or reset by the platform.

## Authentication and keys

The beta uses private, high-entropy invitation links. They are reusable access
credentials: share them privately. The fragment is removed before API sign-in.
The server stores invitation/session hashes, and the portal issues a Secure,
HttpOnly, host-only SameSite cookie. State-changing browser requests enforce Origin.
Only an admin can create invitations. The initial beta is capped at ten accounts.

Provider keys and generated machine credentials are encrypted with Fernet in the
platform SQLite database. Real inference keys never enter tenant machines. Their
provider adapters call a fixed-provider inference broker with a user-scoped token;
the broker inserts the actual owner's provider key. Requests cannot select another
account or arbitrary upstream host. No cloud administration key enters tenant VMs.
The control-plane database and encryption key must be backed up together and stored
privately. Account revocation/rotation and off-machine automated backup need further
work before broader distribution.

Any one of OpenAI, Anthropic or DeepSeek can start a swarm. Provider settings show
only a short key prefix plus a fixed mask; full keys are never returned. The model
list uses credential-scoped provider discovery cached for one hour and invalidated
when keys change. Add/delete operations require a paused workspace.

At startup the seed uses an available model from the connected providers, preferring
gpt-6-luna, Claude Haiku 5.5 or DeepSeek Flash, then a known lightweight model.
Existing agents retain their immutable model assignments when keys change; keep
the corresponding provider connected for agents you want to continue using. OpenAI-backed web search is exposed only when the user has
an OpenAI key; web retrieval remains available to all agents.

Avatar generation is paid by the host via HOSTING_AVATAR_OPENAI_KEY. Tenant machines
receive a scoped token for an image-only broker endpoint, not the host's key. That
endpoint fixes the model, image count, size and quality and rejects text inference.
The host key is never advertised as an inference provider. Hosted ChatGPT-plan
inference is not enabled: it needs an approved integration, not the local OSS flow.

## Deployment and lifecycle

The tenant source revision is pinned. A durable creation claim is saved before the
billable API request; ambiguous outcomes are reconciled against instance metadata
rather than blindly creating another machine. Installer progress survives controller
restarts. Cloud commands never include real provider keys or log raw outputs to users.
Failed setup stops allocated compute and preserves the disk for inspection/retry.

A user starts a 2-, 8-, or 24-hour session and can extend it. The platform stops the
controller and machine on pause or expiry, preserving the working volumes. Resume
uses the same instance and data. The portal remains available while it is stopped.
Automatic browser-inactivity suspension is deliberately disabled: background agent
work is authorized for the selected session. Timed shutdown must remain independent
of the tenant's own UI/controller responsiveness.

The native services currently use private Agent37 preview URLs reached through
short-lived signed links. The global Agent37 API key never reaches the browser.
The portal is branded at swarm.forum; native service hostnames are not yet branded.
A tenant gateway listens on ports 18065 (Mattermost), 13000 (Gitea), 14096
(OpenCode) and 18066 (analytics). Native backends stay on loopback. Portal service
links include a single-use, 60-second handoff bound to the user, portal session and
specific service. The gateway exchanges it for a Secure/HttpOnly host-only cookie,
then removes the ticket from the URL. Logout revokes handoffs and service sessions;
HTTP sessions and open sockets recheck the portal session within five seconds.

Mattermost uses its normal login API and native browser cookies, with the desktop
landing page disabled. Gitea trusts only the host bridge address for proxy identity;
the gateway strips caller-supplied identity headers and inserts the owner account.
OpenCode receives its existing Basic credentials from the gateway. Unsafe browser
requests require the exact service Origin. Uploads, SSE and WebSockets stream
through the gateway. Service passwords are never placed in URLs.

Google sign-in uses authorization code + PKCE, state, browser-binding and nonce,
with verified RS256 issuer/audience/expiry and stable Google subject IDs. New
Google identities create locked accounts. After sign-in, redeem an invite to unlock
provider settings and provisioning. Redemption atomically attaches the Google identity
and its portal sessions to the existing invited workspace, preserving its data and role.
Existing accounts default to activated during migration. Pending identities do not
consume invitation places and cannot access resource endpoints. Invite links carry
the code through Google sign-in in tab-scoped session storage, cleared on redemption.
The legacy invite login API remains available for operator recovery. See GOOGLE_SIGN_IN.md.

Keep ports private; do not add public tenant service ports. The only public port is
the hosting broker/control-plane listener, which checks either a server-side service
credential plus a user session, or an owner-scoped inference capability.

Archive-to-object-storage, restoration from off-machine backup, paid billing,
commercial ChatGPT sign-in and custom service domains are
follow-on work. The beta intentionally does not claim these features are available.

## Operator configuration

Run `python -m harness.hosting.server` with:

- HOSTING_DB: private persistent SQLite path.
- HOSTING_VAULT_KEY: Fernet encryption key.
- HOSTING_SERVICE_KEY: Vercel-to-control-plane credential.
- HOSTING_OWNER_CODE: initial operator invite code.
- HOSTING_PUBLIC_URL: inference broker URL.
- HOSTING_SOURCE_REF: deployed full Git commit SHA.
- AGENT37_API_KEY: platform cloud credential.
- HOSTING_AVATAR_OPENAI_KEY: host-funded avatar generation only.
- HOSTING_GOOGLE_CLIENT_ID / HOSTING_GOOGLE_CLIENT_SECRET: optional Google OAuth web client.

Vercel needs only CONTROL_PLANE_URL and CONTROL_PLANE_KEY. No user's provider key
or global cloud key belongs in its public environment variables.

The platform VM and tenant VMs both retain disk while stopped. At the documented
rates, a 2 vCPU/4 GB/12 GB platform VM is about $12.08/month continuously running;
a 4 vCPU/8 GB/20 GB tenant costs about $0.72 per awake day plus $1.80/month retained
disk, excluding inference/backups. These are estimates, not capacity guarantees.

Hosted Mattermost bootstrapping skips the native welcome tour and hides its onboarding
checklist for the owner. It installs the same Discord Dark palette as the local swarm
once per workspace, preserving later manual theme changes across resumes. Existing
sleeping workspaces receive these defaults on their next resume.

Resume reuses existing containers when a successful-install checkpoint matches the
runtime files and generated tenant configuration. Its readiness checks start Postgres
first, then Mattermost/Gitea/OpenCode, then the controller and service gateway. The fast
path does not fetch Git, sync packages, run bootstrap, or build images. Portal, docs,
and test changes are excluded from the runtime fingerprint. Runtime/configuration
changes, missing containers, or a missing checkpoint run the normal installer. The
checkpoint is cleared before installing and written only after successful setup;
failed updates cannot be treated as ready. Existing deployments require one installer
run to establish this checkpoint. VM boot and service readiness still take time.


The signed-in portal shows the analytics view in place, using a same-origin embedded
view and an authenticated WebSocket bridge. Provider settings, sign-out, and the
operator's invitations live in Settings. The bridge reuses tenant SSO server-side;
service cookies and infrastructure keys never enter browser JavaScript. Stream
connection tickets are single-use, expire after 60 seconds, and bind to one portal
session and tenant. Logout, pause, stop, and expiry revoke streaming access within
five seconds. Avatar/activity requests recheck portal authentication and expose only
fixed read-only endpoints. Native service links acquire SSO and retain their requested
relative channel/repository destination.

Portal Pause preserves the VM and work. Portal Stop permanently deletes that tenant
VM, including agents, messages, repositories and working files; account settings and
provider keys are retained. Stop immediately blocks further inference through the
broker. Play after Stop provisions a fresh VM and seed. The destructive Stop button
explains this before executing. This differs from Mattermost's existing `/stop` work
control, which does not delete the hosting instance.
