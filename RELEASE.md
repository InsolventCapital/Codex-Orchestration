# Release process

This process is for the InsolventCapital-maintained fork, not an official Cjbuilds
upstream release. Canonical repository: `InsolventCapital/Codex-Orchestration`;
HTTPS source: `https://github.com/InsolventCapital/Codex-Orchestration.git`,
ref `main`; marketplace: `insolvent-capital-codex-orchestration`; plugin:
`codex-orchestration@insolvent-capital-codex-orchestration`.
The current source candidate is `0.9.5 — Unreleased`, future tag `v0.9.5`.
Publication, merge, tag signing and live qualification require fork owner authority.
This source task authorizes none of those actions or any live model call.
Keep the original author credit, MIT license and copyright unchanged.

1. Replace `Unreleased` in `CHANGELOG.md` with the release date.
2. Confirm `.codex-plugin/plugin.json`, the changelog, the installed package, and lifecycle fixture all use the same new semantic version. Never publish different plugin behavior under a version already used by another bundle; fix forward with a new version so Codex cannot reuse the old cache identity.
3. Run the source-of-truth local release gate:

   ```bash
   python3 scripts/preflight.py full
   ```

   Local results are PARTIAL; required hosted checks are authoritative. Behavior fixes require an exact regression test, and every plugin payload change requires a strictly greater semantic version. Security or state changes additionally require a threat model, malformed and negative tests, and a fresh final-tree review attested against the exact head SHA. Any later head change invalidates that review.

4. From a new Desktop task, verify one direct same-provider child route. Record `route accepted`; record `used and confirmed` only if the client exposes effective child model/provider/effort metadata.
5. If Claude Fable 5 is included in the release, verify both supported seat paths from a first-party Claude login: Fable Planner `create_plan`/`revise_plan` with a different Advisor, and Fable Advisor `review_plan` with root planning. Confirm the pinned primary model, exact allowlisted helper set reported by runtime metadata, effort, status, the bounded approval loop, and disable/restore. An unknown helper model is a release failure, not an implicit allowlist expansion.
6. If Claude Opus 5 is included in the release, separately qualify both live seat paths from a first-party Claude login. Run Opus Planner `create_plan` and force a reviewed `PLAN_REVISE` round through `revise_plan` with a distinct Advisor; then run Opus Advisor `review_plan` with root planning. For both paths confirm the pinned `claude-opus-5` primary, the primary-only allowed-runtime-model policy, the configured effort, no tools and no session persistence, `status --require-effective`, the eight-review approval bound, and disable/restore. Any helper or other runtime model is a release failure. This is a manual release qualification; local preflight must never invoke Opus.
7. Merge only after every protected check passes.
8. Create a signed annotated tag named `v<manifest-version>` at the reviewed merge commit.
9. Re-run `python3 scripts/preflight.py full` on the tagged tree, then run `python3 scripts/release_check.py --require-tag` and publish a GitHub release from that tag using the matching changelog section.
10. Qualify upgrade from the previous version in a clean Codex home, reinstall the fork plugin, and verify the installed version and skill contents changed before starting a new task. Verify setup, `status --require-effective`, disable, and restore. Require exactly one enabled fork plugin with `marketplaceSource.sourceType` equal to `git`, the exact HTTPS source above and registered requested ref `main`, before and after steady-state native upgrade/add. Missing provenance, duplicates, local/unexpected sources and source/ref drift fail closed. The synthetic fork-namespace 0.5.0 lifecycle fixture is not a published fork release or an actual upstream-to-fork migration.

## Separate transition qualification

Measure the registered requested ref through native AppServer `config/read`
with `includeLayers: true`: the user-layer and effective
`marketplaces.<name>` values must agree on `source_type`, `source`, and `ref`.
The expected source and `ref = "main"` must be explicit; inventory URL alone
or the arguments passed to marketplace add are not readback evidence.
The optional `last_revision` field can be null after native add/upgrade.
Never use it as a substitute for the independently measured cache HEAD,
origin, and complete package inventory/byte comparison; if populated, require
it to agree with that exact expected commit. Preserve the filtered registration
readback and any mismatch in the qualification report.
The disposable probe exercises this measurement for both synthetic distributions
and re-add transitions; its result remains `LOCAL_FIXTURE`, not HTTPS or Desktop
qualification.

Upstream-to-fork transition requires separate owner authorization, prior
source/policy hashes, a qualified native CAS recipe, and tested rollback in a clean
profile. Preserve personal Verifier paragraphs, receipt and registration, saved
routing state and hint bytes, and unowned fields. Retain the inactive old package
installed but disabled: native remove erases unowned plugin-local settings. Routing
disable/setup loses personal Verifier extensions and is not a transition recipe.
Use native AppServer versioned CAS only on exact old/new plugin enabled and
Fable-MCP enabled fields, rejecting stale versions and unrelated drift. Never add a
generic migration bypass or rewrite policy fields. LOCAL_FIXTURE probe results do
not qualify real HTTPS or a live profile migration; root must separately qualify
those gates before authorizing live action. No source update automatically migrates
the current live user.

The standalone personal Opus Verifier is separate from bundled seat qualification;
never add a native Verifier field. Preserve policy markers, default route choices,
model/runtime allowlists and all existing review gates. Preserve each user's
applicable task-local review budgets and owner authorization gates; installation
never resets or replenishes them.
Source changes invalidate earlier review/verification; obtain a fresh final-tree
review bound to the exact head SHA and all required hosted checks before handoff.

Never move a published release tag. If a release is bad, fix forward with a new version and retain the old tag as provenance.

Before downgrading to a release that predates Planner/state-schema-3 support, run `disable` with the current release. Older versions must fail closed on the unknown state schema rather than guessing how to restore it.
