# Security policy

## Supported versions

Security fixes are made on the latest released version. Upgrade before reporting a problem that is already fixed on `main`.

## Report a vulnerability

Do not post suspected vulnerabilities or sensitive reports publicly. Private
vulnerability reporting is currently disabled on the InsolventCapital fork.
For fork-specific sensitive reports, obtain an established private channel from the
fork maintainer before sending details; no private fork reporting channel or email
is advertised here.

For inherited upstream issues, follow [Cjbuilds upstream private disclosure
guidance](https://github.com/Cjbuilds/Codex-Orchestration/security/advisories/new).
Include the affected version and Codex client version, operating system and
installation scope, a minimal reproduction, security impact and known workaround.
Do not include credentials, tokens, or private configuration. Upstream disclosure
arrangements do not establish a private reporting channel for this fork.

## Security boundaries

Codex-Orchestration changes only documented routing fields, explicitly prepared
`model_providers.<id>` tables, plugin-managed personal agent files, and strict
non-secret state under `CODEX_HOME`. External setup never writes top-level `model`
or `model_provider`, never edits OpenAI authentication, and never reads, migrates,
or deletes chat/session storage.

The explicit update control first requires exactly one enabled installed plugin with
the fork identity `codex-orchestration@insolvent-capital-codex-orchestration`,
marketplace `insolvent-capital-codex-orchestration`, `sourceType` equal to `git`,
exact source `https://github.com/InsolventCapital/Codex-Orchestration.git` and
registered requested ref `main`. Missing provenance, duplicates and source/ref
drift fail closed. It then delegates refresh, transport,
process containment, cache mutation, and installation exclusively to Codex's native
`plugin marketplace upgrade` and `plugin add` commands, followed by a strict native
inventory check for canonical source, nondecreasing SemVer, and retained enabled
state. The skill introduces no downloader, Git client, subprocess wrapper, or
rollback claim and does not construct a credential-bearing environment. It never
invokes plugin removal, rewrites config, reads credentials, or reads/writes routing,
provider, chat, or session state.

Provider API keys are accepted only by a hidden local prompt outside chat and are
stored in the operating-system credential store. Codex retrieves a key at request
time through documented command-backed auth and a stable helper under `CODEX_HOME`;
the provider table stores only the helper
path and non-secret arguments. The plugin rejects secret-capable registry fields,
provider ID collisions, unsafe URLs, unknown manifest fields, symlinks, hardlinks,
stale compare-and-swap digests, unqualified adapters, unsupported efforts, and
changed helper or CLI bytes. A user-supplied helper is executable code and must be
explicitly trusted; byte drift changes its status to `CLI_CHANGED` and requires
re-trust.

The command-backed helper necessarily returns the credential over captured stdout
to the local readiness check or Codex provider process that invoked it. Those are
trusted recipients; the value is kept in memory only, discarded immediately, and
never included in diagnostics, model prompts, state files, or decorated output.

Role resolution is a fresh authorization check, not a registry lookup: it compares
the bundled adapter version and capability declaration, live App Server provider
table, qualification/readiness state, credential-helper identity, credential
availability, and selected personal-agent digest. Any mismatch blocks delegation.

External provider preparation and removal use exact App Server readback plus a
content-free recovery journal. Role files and registry state use a recoverable
multi-file transaction. Recovery rolls forward or back only when every digest and
ownership check matches; ambiguity becomes `RECOVERY_REQUIRED` without overwriting
user data. On Windows, replacement stages copy and canonically verify the existing
owner, group, DACL, and mandatory integrity label before publication; inability to
read, apply through Windows' `SetNamedSecurityInfoW` API, or re-read that
access-control metadata fails closed and rolls the transaction back.

Gate 0 is an explicit, potentially billable, ephemeral `codex exec` probe in an
isolated temporary `CODEX_HOME`. The pinned CLI must advertise every required flag
before the billable command starts. Decorated output is discarded, and only a
bounded, regular, single-link `--output-last-message` artifact can satisfy the fixed
signal. A successful response proves route acceptance, not the model's runtime
identity. Native providers remain
`ROUTE_ACCEPTED` unless the host exposes mechanical provider/model metadata; model
self-report is never confirmation.

Native setup/status/repair/disable and bundled Claude authorization retain their full-state
validators. Repair is allowed only when valid saved state exists, both live hint
strings retain the ownership marker, and namespace, spawn metadata, historical Fable
launcher enablement, scalar-conversion shape, and all other managed values still match. It
restores only drifted mode/usage bytes through App Server compare-and-swap, verifies
user and effective readback, rolls back on an override, preserves a concurrent edit,
detects concurrent saved-state replacement without overwriting it, and never changes
restore state, authentication, credentials, chats, or sessions.
The bundled Fable/Opus Planner-or-Advisor bridge gives authentication and model
subprocesses only a minimal platform environment, preserving home discovery for the
official CLI while excluding credential, config-redirection, provider/model/effort,
endpoint/gateway, proxy/CA/mTLS, and telemetry override families. It disables tools
and session persistence and requires structurally valid runtime usage metadata to
contain the pinned primary plus only the exact helper allowlist for that model. The
managed workflow authorizes only root to call planning tools, but MCP does not
provide caller identity; that caller boundary remains instruction-enforced rather
than server-authenticated.

Routing schema/policy version 5 adds the sealed Opus subscription route while
retaining strict validation for schemas 1–4. Legacy schemas cannot smuggle newer
fields, generic model routes cannot encode either reserved bundled Claude model, and
only one bundled Claude Planner-or-Advisor seat may exist. Persistent Designer
accepts only a direct same-provider model, never a privileged bundled Claude MCP
route or a project-shadowable unqualified agent name.
Cross-provider/custom Designers remain task-local and require current-project
validation immediately before use. Designer authority is
policy-bounded: it reports only to root, cannot contact other seats or spawn
descendants, may edit only explicitly delegated design artifacts, and cannot alter
the canonical plan, implementation code, approvals, or Executor release. These
behavioral limits are instruction-enforced; normal Codex sandbox and approval
controls remain the mechanical boundary.

External providers receive delegated prompt content and may retain it under their
own policies. OS credential stores, first-party subscription CLIs, Codex itself, and
provider endpoints are trusted dependencies. The plugin does not weaken sandbox or
approval settings and cannot guarantee that policy-guided delegation is
engine-enforced. See the README and External Models reference for the operational
contract.

## Fork distribution threat model

Assets are the exact reviewed repository/head, package provenance, saved policy,
personal Verifier extensions/receipt/registration, and unowned plugin settings.
Threats include upstream or arbitrary repository attestation replay, local-source
substitution, duplicate namespace discovery, source/ref drift, and destructive
remove/setup during namespace transfer. Mitigations are exact repository matching
in event and attestation, strict source/ref checks before and after native update,
negative/malformed regression tests, and fresh exact-head review. No caller-selected
repository or generic migration bypass is accepted.

Any future namespace transfer requires separate owner authority, prior source/policy
hashes, qualified native AppServer versioned CAS only on exact old/new plugin enabled
and Fable-MCP enabled fields, and tested rollback. Preserve saved routing state,
hint bytes, personal Verifier paragraphs, receipt and registration, and all unowned
fields. Retain the inactive old package installed but disabled: native removal erases
unowned plugin-local settings, and routing disable/setup loses personal Verifier
extensions. LOCAL_FIXTURE coverage proves neither real HTTPS nor live migration.
The standalone personal Opus Verifier and existing policy/review gates remain
unchanged. Preserve each user's applicable task-local review budgets and owner
authorization gates; installation never resets or replenishes them.
