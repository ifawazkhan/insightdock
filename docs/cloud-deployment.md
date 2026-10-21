# Azure Container Apps: restricted cloud PILOT

This is a deployment checklist, **not production certification**, infrastructure
automation or permission to spend money. No Azure resources are created by these
files or by CI. Do not open the pilot to users until every deployment gate below
has evidence and an accountable operator. Start with synthetic/non-sensitive data.

The cloud entry point runs native Google sign-in and database onboarding before
initializing private history. It uses only the returned guarded per-session
database, never the operator's local database. Offline integration tests cover
this boundary. Live Google sign-in, a real verified-TLS MySQL connection, Linux
container rendering and deployed network controls still require pilot validation;
passing unit tests alone is **not permission for a public launch**.

## 1. Account, approval and cost gate

1. Create an Azure account through the official Azure portal if none exists.
   Select the correct tenant and subscription; get explicit billing-owner and
   security approval before creating anything. Do not accept legal agreements on
   another person's behalf. Use MFA and least-privilege operator roles.
2. Agree a pilot region based on data residency, Container Apps workload-profile
   availability, AI deployment availability, firewall/NAT support and price. Keep
   related resources in that region where supported. Document cross-region data
   transfers to Google, the AI provider and invited users' MySQL servers.
3. Estimate costs in the Azure pricing calculator: ACR storage/builds, Container
   Apps CPU/memory with **minimum one replica**, VNet, fixed outbound IP/NAT,
   Azure Firewall or equivalent inspected egress, Key Vault and monitoring/log
   retention. Include Google/provider policies and AI token charges. NAT/firewall
   and an always-on replica are **not a free-tier deployment**. Budgets/alerts are
   not hard spending caps. Set an owner, budget alerts, end date and teardown plan.
4. Select a separate pilot resource group and naming scheme. This guide does not
   create a database, identity provider, network, registry or subscription for you.

## 2. Mandatory egress/network deployment gate

Do **not** substitute public ingress, TLS or application hostname checks for an
egress boundary. Before exposing the app, a network owner must implement and test:

- A VNet-integrated **workload profiles** Container Apps environment supporting
  UDR/controlled egress, not a legacy Consumption-only shortcut. Route application
  outbound traffic through a reviewed firewall/NVA and fixed public SNAT egress.
  Depending on the topology, fixed egress may be Azure Firewall public IP(s) or
  NAT Gateway attached to the firewall's subnet. NAT alone is not a firewall;
  NAT on the app subnet does not magically inspect traffic routed to a firewall.
- Default-deny application egress with narrowly approved DNS, HTTPS destinations
  for Google OIDC, the configured AI endpoint and required Azure platform/registry
  services. Permit TCP **3306 only to reviewed public MySQL destinations**; do not
  grant arbitrary internet ports or broad private network access.
- Block application connections to private, loopback, link-local, reserved and
  metadata destinations (including `169.254.169.254`), covering IPv4 and IPv6 and
  same-subnet/local routes. A default-route firewall alone may not see link-local
  or platform-local traffic. Prove effective blocks from the workload, using
  supported platform/workload controls; if this cannot be enforced, **no-go**.
  Isolate required platform/DNS/identity exceptions from user-directed DB traffic;
  do not break ACA's documented platform dependencies. Managed identity endpoints
  must not become reachable via user-selected database destinations.
- Validate allowed destinations and denied ranges without probing real metadata
  credentials; retain sanitized network-policy evidence. Recheck DNS changes and
  all possible egress IPs. Configure the MySQL server's firewall to accept only
  those fixed pilot egress IPs. Never enable “allow all Azure services” as a fix.

Application DNS validation and numeric-address pinning are an additional defense,
not a replacement for this gate. Private DB endpoints, IP literals, SSH tunnels,
custom ports, private CAs and private-network MySQL are **unsupported** in this
pilot. Azure infrastructure private endpoints, if required for registry/Key Vault,
are a separately reviewed platform exception, not a way to enable private DBs.

## 3. Image and dependency preparation

[Dockerfile](../Dockerfile) targets `python:3.14-slim`; the configured development
venv was Python **3.14.7** when the pins were captured. The tag floats to later
3.14 patches: record the resolved base digest, actual Python version and final
image digest for each approved build. Do not assume the tag always means 3.14.7.

[requirements-cloud.txt](../requirements-cloud.txt) is a curated runtime subset,
not a freeze of the local dataset/MCP/test environment. Direct runtime versions
were read with `importlib.metadata` after configuring the venv. Authlib was added
to that venv before pinning (native authentication needs **Authlib >=1.3.2**).
PyMySQL plus cryptography supports `caching_sha2_password`; verified TLS remains
mandatory. `python-dotenv` remains an import dependency, but cloud settings do not
load the local environment file. No main requirements or local application code
are changed by these deployment files.

The image runs UID/GID **10001**, with root-owned application code. Only the entry
script, chatbot package, assets, Streamlit config and cloud requirements are
copied. [.dockerignore](../.dockerignore) denies the rest of the context, including
environment files, secrets, local state, Git, venvs, datasets and tests. Never add
`COPY . .`, credential build arguments, dataset generation or loading to this
image. Scan approved source/assets for accidentally embedded secrets too.

Linux wheels are required (`--only-binary=:all:`); missing Python 3.14/Linux amd64
wheels fail the build rather than silently compiling. CA roots, fontconfig,
freetype and DejaVu fonts support TLS/static charts; ReportLab also bundles Vera.
Run the CI renderer check on Linux, since macOS wheels do not prove Linux support.
Direct pins do not lock transitive dependencies or OS packages: resolve/review an
SBOM and dependency/container vulnerability scan before accepting an image. A
`pip-audit`/container security audit is an additional gate, **not implemented or
claimed successful by this workflow**. Rebuild and retest security updates.

### Build/publish only after approval

On a Mac, local builds must target **Linux amd64**, not the host's arm64/macOS.
With a separately installed/approved Docker engine, an operator can build with
`docker build --platform linux/amd64 -t chatbot-cloud:pilot .` and run the offline
container checks in [.github/workflows/cloud-checks.yml](../.github/workflows/cloud-checks.yml).
Never accept a Docker/Apple license automatically just to run this check.

Alternatively, after creating an approved ACR in Azure Portal, an operator with
Azure CLI access can submit the allowlisted context to a charged ACR build:
`az acr build --registry <ACR_NAME> --image chatbot-cloud:<UNIQUE_PILOT_TAG> --platform linux/amd64 .`
Run it from the repository root, inspect the build context first, and never pass
secrets as build arguments. This command is documentation only, not executed here.
Use ACR's full login-server image reference and recorded digest in ACA. Prefer
managed-identity `AcrPull` scoped to this registry; do not enable ACR admin-user
credentials. Ensure registry pulls work through the approved network design.

## 4. Container Apps configuration checklist

In Azure Portal, create the reviewed VNet/workload-profile environment and app
only after the account/network gates. Keep ingress disabled or restricted to
operator source IPs during setup; an NSG on an external environment's subnet is
not a substitute for ACA ingress IP restrictions.

- Select the reviewed image digest; one app container, **one Streamlit process /
  one worker / one replica**. Set min replicas **1**, max replicas **1**, single
  revision mode, no autoscale rules, no Gunicorn/multiple worker wrapper. Start
  with an explicitly budgeted CPU/memory allocation (for example 1 vCPU/2 GiB,
  subject to available profile limits); load-test bounded reports before invites.
- Quotas are process-local. Revision changes may temporarily overlap replicas
  even with single revision mode: for this pilot use a maintenance window, stop
  admissions, terminate/deactivate the previous revision and verify only one
  serving process before resuming. No blue/green or traffic splitting. Scaling
  requires a shared atomic quota/session architecture, not just stickiness.
- Keep the image command: cloud mode, bind `0.0.0.0`, port **8501**, headless,
  file watcher `none`, XSRF protection `true`. No credential defaults are supplied.
  Use ACA HTTPS ingress target port 8501, disallow insecure HTTP, and confirm
  WebSocket support. Do not expose raw port 8501 to the internet or disable XSRF
  or origin protection to fix proxy/callback problems.
- Docker health uses stdlib `urllib` against `/_stcore/health` only. ACA does not
  execute Docker HEALTHCHECK: configure explicit HTTP startup/readiness/liveness
  probes on port 8501 and that path. Suggested starting values: startup period
  5s/timeout 3s/failure threshold 24; readiness period 10s/timeout 3s/threshold 3;
  liveness period 30s/timeout 3s/threshold 3. Tune based on measured startup/load.
  Health proves the HTTP process only, **not authentication, authorization, AI or
  DB readiness**. Missing OIDC secrets can still yield healthy HTTP while private
  app access fails closed.
- No persistent session/storage/recipient volume, no SMTP configuration and no
  dataset mounts. Temporary writable home/tmp may be needed by Streamlit/fonts;
  do not enable a read-only filesystem without testing those paths. Restrict
  operator console/log/secret access; never log form inputs, claims, prompts,
  query results, cookies, credentials or secret file contents. Retain minimal
  sanitized operational telemetry for a short approved interval.

## 5. Environment: operator AI settings, never tenant DB credentials

Use ACA environment variables and **secret references**, not a deployed local
environment file. Set explicitly:

| Variable | Pilot value / requirement |
| --- | --- |
| `CHATBOT_MODE` | `cloud` (already the image default; do not override to local) |
| `CHATBOT_OIDC_ISSUER` | `https://accounts.google.com` (exact issuer match) |
| `CHATBOT_CLOUD_ALLOWED_SUBJECTS` | Nonempty comma-separated invited Google **sub** values; optionally exact `issuer\|sub` pairs |
| `CHATBOT_AI_PROVIDER` | Explicitly `openai` or `azure` |
| `CHATBOT_ALLOW_MUTATIONS` | `false`; cloud also forces read-only |
| `CHATBOT_AI_SUMMARIES` | `false` as operator baseline; onboarding separately asks result-sharing consent |

For OpenAI, set `OPENAI_MODEL` to an approved available model supporting Chat
Completions JSON output and `OPENAI_API_KEY` via an ACA secret reference.
For Azure, set `AZURE_OPENAI_ENDPOINT` (approved HTTPS endpoint),
`AZURE_OPENAI_API_VERSION`, and `AZURE_OPENAI_DEPLOYMENT` (or
`AZURE_OPENAI_DEPLOYMENT_MINI`) plus `AZURE_OPENAI_API_KEY` via a secret reference.
The deployment name is not a generic model name. Keep unrelated provider keys
unset; provider configuration is required **before** DB onboarding. Review
provider retention, region and AI costs; these action quotas are not token caps.

Cloud onboarding requires users to confirm DB permission and schema/questions
sharing consent; it sets `openai_consent=True` for that connected session. Optional
result summaries have a separate per-session checkbox. Setting
`CHATBOT_OPENAI_CONSENT` does not skip onboarding. Questions can contain sensitive
data; redaction is best effort, not anonymization.

Do not set `MYSQL_*` passwords or a shared root account. Invited users submit
their own dedicated read-only database credentials over HTTPS; credentials and
connections stay session-local. Local mode and its existing environment file are
unchanged; **cloud mode does not read that file**.

## 6. Final hostname, native Google OIDC and private file mount

1. Determine the final ACA HTTPS FQDN (or approved custom domain with valid TLS)
   while access is restricted. In Google Cloud Console, configure the consent
   screen/branding, audience and pilot test users according to your organization.
   Create an OAuth **Web application** client. Set its authorized JavaScript
   origin to `https://<FINAL_HOST>` and its exact authorized redirect URI to
   `https://<FINAL_HOST>/oauth2callback`. No wildcard or guessed callback hostname.
2. Use [.streamlit/secrets.example.toml](../.streamlit/secrets.example.toml) only
   as a placeholder template. Privately replace the final `redirect_uri`, Google
   `client_id`/`client_secret` and a unique cryptographically random cookie secret
   (at least 32 random bytes from an approved password manager). Never paste
   secrets into chat, shell history, Git, image layers or build/CI logs. The cookie
   secret is an application signing secret, **not entered in Google Console**.
3. Store the complete TOML as one ACA secret, preferably a Key Vault reference
   with narrowly scoped access. In the revision's **Secret** volume, select only
   this secret (not “mount all secrets”) and map `secretRef: streamlit-auth` to
   relative `path: secrets.toml`. Mount that volume read-only at
   `/app/.streamlit`, resulting in **/app/.streamlit/secrets.toml**. It must be
   readable by UID 10001; verify permissions without printing contents.
4. A directory volume hides the image's config file at the same path. Preserve
   the checked-in non-secret config by including its content in a second ACA
   secret/volume item mapped to `config.toml`, in that same selective volume.
   (This configuration is not confidential, but keeps the two files on one ACA
   secret volume.) Do not use an unsupported single-file/subPath mount shortcut.
   Verify both files exist/read correctly and stats/XSRF settings remain intact.
5. Native `st.login("google")`, `st.user` and `st.logout()` perform OIDC; custom
   headers, query parameters, email text and local profiles are **not** login.
   Obtain each invited user's Google `sub` through an approved identity-admin
   workflow or verified Google-issued identity evidence, never by trusting a
   self-submitted string/unverified JWT or authorizing by email. Google's OAuth
   test-user list is separate from the app's subject invite list. An authenticated
   but uninvited user must be denied before DB forms/history are shown.
6. Keep issuer exact. Finalize **both** Google callback registration and TOML
   redirect before live login. Update both if hostname changes. Rotate secrets
   through a controlled stop/restart; do not assume changing an ACA secret alone
   refreshes existing revisions. Recheck mounts, one-process admission and login.

Streamlit's native identity cookie can last **30 days**; this is distinct from
the app lease: **3600 seconds absolute / 900 seconds idle**. Expiry checks run on
reruns/actions, not a background browser eraser. Expired app grants clear state
and call native logout; logging out of this app does not log out of Google or
instantly erase every already-open tab. Validate this behavior during acceptance.

## 7. Database, limits and data boundaries

- Public **DNS hostname only**, every resolved answer public; port **3306 only**.
  The engine pins one validated numeric address and retains the original hostname
  for TLS SNI/certificate verification. A new connection setup must validate DNS
  again; there is no unverified reconnect fallback.
- MySQL must offer TLS with a matching-hostname certificate chaining to the
  container's **system-trusted CA** roots. No TLS downgrade, root account,
  self-signed/private-CA override, uploaded trust bypass or `ssl_verify=false`.
- A real supported MySQL server with session **MAX_EXECUTION_TIME** capability
  is required. Cloud discovery fails closed without it; MariaDB is unsupported.
  Validate server privileges/timeouts with a dedicated SELECT-only account and
  confirm denial outside intended schemas. Avoid sensitive/raw data in the pilot.
- Default fixed-hour admission budgets per subject / global: **20 / 200 model**
  calls, **120 / 1000 DB** actions, **5 / 20 connection** attempts. Discovery and
  report queries/model calls count; admitted attempts count even if I/O fails.
  There are also at most **10 active DB connections** process-wide. Quotas reset
  on process restart and do not provide durable billing or cross-worker limits.
- Chats, DB credentials, reports and generated PDFs are nonpersistent in-memory
  session data; disconnect/logout/restart/revision replacement can lose them.
  Do not promise browser reconnect recovery or durable sessions. Cloud pilot
  offers **PDF download only**: email delivery and persistent recipient preferences
  are disabled. Downloaded files then fall under the user's device/data policy.
  Streamlit's native download media URLs are not an application-owned,
  per-request authorization endpoint: authorization is checked when rendering,
  not on every file fetch, and an already-issued URL/file cannot be revoked by
  signing out. Treat these URLs as confidential bearer links. Use synthetic or
  non-sensitive data for this pilot; sensitive-data production use requires an
  authenticated download endpoint and verified expiry/revocation behavior.

## 8. Validation and go/no-go evidence

The CI workflow installs cloud runtime plus separate pytest/Faker/openpyxl and
runs the **full** tests; live destructive MySQL integration stays disabled. It
also builds Linux amd64, checks nonroot/no local files, renders an offline static
chart, asserts missing native secrets stop private UI, and smokes process health
with no external network or real OIDC/DB/AI. It never logs in to Azure, pushes an
image or deploys. A passing health probe is not an authentication acceptance test.

Before inviting users, record actual results for:

- [ ] Full offline tests, clean Linux amd64 image build, offline renderer and
  image isolation/health checks; record image/base digests and runtime version.
- [ ] Dependency and OS/container security review/SBOM; resolve findings or obtain
  explicit risk acceptance. This guide does not claim a completed security audit.
- [ ] Effective fixed egress and denial of private/link-local/metadata access;
  independent network review and approved MySQL firewall restrictions.
- [ ] Real Google callback/login: invited accepted, uninvited/missing secrets
  denied, no pre-login DB/history access, logout/absolute/idle expiry and two
  concurrent identities isolated. Never put real identity tokens into test logs.
- [ ] Real public TLS MySQL: valid certificate succeeds; wrong hostname/untrusted
  CA/non-TLS and unsupported max execution time denied; SQL read-only/scope and
  model/DB/connection quotas demonstrated using a disposable synthetic database.
- [ ] PDF/rendering under bounded Linux load; no email/recipient persistence;
  restart clears session data and resets quotas as disclosed.
- [ ] Billing alerts, rollback/stop procedure and pilot end date approved. On a
  problem, restrict/disable ingress and stop admissions before restart/rollback;
  never run two revisions to preserve session availability. At pilot end remove
  unneeded app/network/registry resources with billing-owner approval, revoke
  Google/AI secrets and DB firewall invitations, and check residual charges.

At preparation time Docker was not installed on the development Mac. Linux
build/smoke, live Google auth, actual public TLS DB, Azure deployment/network
policy and security audit were **not verified**. Local offline results are
**1009 passed, 1 skipped**, with `pip check` passing. A separate cloud entry-point
smoke failed as documented above; the ordinary suite does not establish working
cloud onboarding. CI outcomes must be checked after the workflow actually runs.
Do not describe this checklist as public-production-ready.

## References

- [Streamlit native OIDC and cookies](https://docs.streamlit.io/develop/concepts/connections/authentication)
- [ACA secrets and selective secret-volume file paths](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets)
- [ACA network/firewall constraints](https://learn.microsoft.com/en-us/azure/container-apps/firewall-integration)
- [ACA controlled outbound routes](https://learn.microsoft.com/en-us/azure/container-apps/user-defined-routes)
- [ACA HTTP probes](https://learn.microsoft.com/en-us/azure/container-apps/health-probes)