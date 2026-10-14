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
Only the seed is reassigned when provider settings change; existing workers retain
their explicit models. OpenAI-backed web search is exposed only when the user has
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
Google identities cannot create accounts: first open an invite link and choose
Connect Google in the portal. Invites remain usable. See GOOGLE_SIGN_IN.md.

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
