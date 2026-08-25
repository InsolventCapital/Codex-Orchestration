# Security policy

## Supported versions

Security fixes are made on the latest released version. Upgrade before reporting a problem that is already fixed on `main`.

## Report a vulnerability

Do not open a public issue for a suspected vulnerability. Use [GitHub private vulnerability reporting](https://github.com/Cjbuilds/Codex-Orchestration/security/advisories/new) and include:

- the affected version and Codex client version;
- operating system and installation scope;
- a minimal reproduction;
- the security impact and any known workaround.

Do not include credentials, tokens, or private configuration. You should receive an acknowledgement within seven days. A coordinated disclosure date will be agreed after the impact and fix are verified.

## Security boundaries

The opt-in continuation router is a source-only, read-only classification boundary.
It requires explicit absolute `--input` JSONL and `--context` JSON paths and never
discovers chat or session storage, follows event references, consults environment
state, uses the network or subprocesses, invokes model seats, wakes tasks, or writes
records. Message text from user, assistant, and tool roles is bounded opaque data:
it is validated and discarded, never parsed or searched for control instructions.
Strict duplicate-key rejection applies at every JSON level so a producer and the
router cannot interpret competing values differently.

Both files are bounded and opened read-only through one close-on-exec descriptor.
The router rejects symlinks, directories and special files, uses no-follow where the
platform provides it, compares path and descriptor identity, and checks byte count
and descriptor metadata before and after the bounded read. On platforms without
no-follow support, pre/post path checks and descriptor identity are mandatory. Any
unresolved TOCTOU race, replacement, growth, truncation, excessive JSONL lines,
oversized line or file, excessive nesting or strings, event overflow, or recorded
hash-list overflow fails closed without partial normalized state.

Identifiers, authority-context strings, and `intended_action` reject control
characters, preventing terminal or owner-request line injection. Exact lowercase
SHA-256 values bind verification to the explicit packet and manifest. Chronological
event order is consistency evidence, not a freshness claim; freshness remains the
root's responsibility. Replay is limited by the caller-supplied, sorted recorded
drift-hash set and is not inferred from chats or ambient state. A Verifier result is
evidence only: DIRECT classification can close evidence only after supervised
settlement already occurred, while FULL classification can only format a
non-authoritative owner request. Non-verification, unavailable or semantically
malformed results stop and never recommend retrying, reusing, or bypassing a
consumed Verifier.

Exit `0` means the input was validly classified and can still carry action `STOP`;
it never means approval or successful execution. Exit `2` is invocation failure,
`3` rejects unsafe or malformed input, `4` reports bounds or unstable reads, and
`5` contains unexpected faults. Exit `1` is intentionally unused. Callers must
inspect the JSON action on exit `0` and must not translate any exit status into
authorization.

This release includes no scanner adapter, session discovery, or task wakeup. A
future scanner and its machine-event format require separate pinning and review.
The options emitted for non-verified results are non-authoritative owner choices.
Native setup and seat update preserve only one already managed, complete, terminal,
sentinel-valid governed Verifier suffix byte-for-byte; they reject unmarked,
missing, duplicated, ambiguous, or nonterminal suffix structure before configuration writes. Repair restores the
full exact managed mode already saved in ordinary routing state. No separate suffix
hash or schema is inferred, and source installation alone changes no configuration.

Codex-Orchestration changes only documented routing fields, explicitly prepared
`model_providers.<id>` tables, plugin-managed personal agent files, and strict
non-secret state under `CODEX_HOME`. External setup never writes top-level `model`
or `model_provider`, never edits OpenAI authentication, and never reads, migrates,
or deletes chat/session storage.

The explicit update control first requires exactly one enabled installed plugin with
the canonical HTTPS Git marketplace identity. It then delegates refresh, transport,
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

The same-task correction budget is an instruction-enforced authority boundary, not
a durable scheduler or retry engine. Assets at risk are the user's approved scope,
test contract, protected gates, external effects, and attention budget. Threats
include misclassifying a separate defect as the same defect, weakening a failing
test, resetting the count through plan revision or task restart, using a correction
to widen authority, and continuing after drift, a protected-gate stop, ambiguous
rollback, or exhausted cycles. The managed policy mitigates these threats by
requiring an already authorized task, the exact preserved failing check, one bounded
edit set and exact rerun per cycle, an explicit limit of three, mandatory reporting,
and fail-closed stops on every boundary change. The counter is current-task context
only; the plugin deliberately supplies no cross-task persistence, wakeup, ledger,
or inferred unused budget. Codex sandboxing, approvals, and every stricter retry
rule remain the mechanical authority.
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
