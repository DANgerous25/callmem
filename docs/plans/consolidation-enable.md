# Consolidation Enable Checklist

Consolidation (LLM-routed ADD/UPDATE/NOOP entity merging, `EntityConsolidator`)
shipped in phase 1 with `enabled = false` because it archives user memories on
LLM judgment and four known issues gated the switch. This plan closes those
four, adds the shadow-mode tooling needed to calibrate the threshold on real
data, and ends with a canary enablement.

Motivating evidence: audio-converter carries ~62% duplicate entity mass (one
title x46); ellma's 18 restored failures include `test_reconnect_loop` x7,
gateway-outage x4, Sakana-403 x3. This is exactly what consolidation collapses.

## Global Constraints

- TDD with RED/GREEN evidence. Surgical diffs. Type hints. SQL in
  repository.py. No broad except-Exception that swallows errors.
- Conventional commits. **NO AI attribution** anywhere (overrides any default).
- Production interpreter is Python 3.10: run covering tests under
  `uv run --python /usr/bin/python3 --extra dev pytest <files>` for anything
  touching datetime/stdlib behaviour.
- Full suite green before committing. Do NOT push.
- **Fail-open is inviolable**: every change must preserve the property that a
  malformed, absent, or hostile judge response results in ADD-everything (no
  archival, no supersession), loudly logged. Consolidation must never destroy
  data on a bad LLM day. Any test that would weaken this is wrong.
- `CLOSED_ENTITY_STATUSES` (repository.py) is the single source of truth for
  closed statuses — never re-enumerate them (a regression test enforces this).

## Task 1: dedupe / consolidation precedence

**Finding as originally filed:** dedupe.py (title-similarity, threshold 0.82)
and consolidation (LLM judge, threshold 0.55) both write `superseded_by` +
`stale`, with no defined precedence.

**Correction from code survey (verify this yourself before designing):**
`dedupe` is invoked ONLY from `cli.py:3288` (`find_clusters`/`apply_clusters`)
— it is a manual command, not part of compaction or any daemon path. So there
is no runtime race; the real risk is a human running the cruder heuristic
against a corpus consolidation is already curating, producing supersession
chains from two different rulesets.

**Required behaviour:**
1. Confirm the survey (grep every caller of `find_clusters`/`apply_clusters`).
   If a non-CLI caller exists, STOP and report NEEDS_CONTEXT — the design
   changes materially.
2. Make the CLI command consolidation-aware: when `[consolidation] enabled` is
   true for the project, the dedupe command warns clearly that consolidation
   is the evidence-based curator and title-similarity dedupe may create
   supersession chains from a cruder rule, and requires an explicit
   `--force` (or equivalent already-conventional flag) to proceed. Dry-run
   behaviour unchanged and still available without the flag.
3. Document precedence in `docs/` where dedupe/consolidation are described:
   consolidation is authoritative when enabled; dedupe is the offline tool for
   corpora that predate it (e.g. audio-converter's existing 62% duplicate mass)
   or when no LLM backend is configured.

**Tests:** consolidation-enabled project + dedupe without force → refuses with
the warning, writes nothing; with force → proceeds; consolidation-disabled →
unchanged behaviour; dry-run never blocked.

## Task 2: NOOP-redirect order-dependence + duplicate-existing_id overcount

Two defects in `EntityConsolidator._apply` (consolidation.py ~389-415), both
found by review, neither yet fixed because the feature ships off.

**2a — redirect is iteration-order dependent.** `superseded_this_run` maps an
existing entity to the new entity that superseded it, so a later NOOP whose
target was just superseded redirects its `touch_entity` to the survivor. But
that only works if the UPDATE is applied BEFORE the NOOP in `entities` order.
In the reverse order the NOOP touches an entity that is marked stale one
iteration later — the "survivor gets bumped" signal is silently lost.

**2b — duplicate `existing_id` across two UPDATE decisions is unguarded.**
`_parse` enforces distinct `new_id` but nothing forbids two different new
entities naming the SAME `existing_id`. The second `mark_stale` is a no-op in
the DB (guarded `WHERE ... stale = 0`), but the code ignores the return value:
`stats.updated` increments twice and `superseded_this_run[existing_id]` is
overwritten with the second entity, so a later NOOP redirects to an entity that
is NOT what the DB records as the supersessor.

**Required behaviour:** make `_apply` order-independent and return-value-aware.
Suggested shape (choose your own if better, and justify): resolve all verdicts
into a plan before mutating — apply UPDATE/CONTRADICTS supersessions first
(recording only those `mark_stale` actually acted on), then apply NOOPs against
the resulting survivor map. Reject or collapse duplicate `existing_id` claims
deterministically (first-wins by pair index, remainder recorded as unchanged —
NOT counted as updated). `stats` must reflect what the DB actually did.

**Tests:** both iteration orders of an UPDATE+NOOP pair on the same target
produce identical DB state and identical stats; two UPDATEs claiming the same
`existing_id` → exactly one supersession, `stats.updated == 1`, redirect target
matches the DB's `superseded_by`; existing consolidation tests unchanged.

## Task 3: citation transfer on NOOP-archive

When a NOOP archives a newly-created duplicate, any citation credit that later
lands on it is stranded, and — more importantly — the survivor does not inherit
the duplicate's existing `cited_count`. Phase-1 T3 made `cited_count` a real
input to briefing ranking, so losing it demotes the survivor.

**Required behaviour:** on NOOP archival, transfer the archived entity's
`cited_count` onto the survivor (additive) and carry `last_cited_at` forward if
it is newer. Repository method; SQL there. Same for the entity superseded by an
UPDATE (the old entity's citations belong to its replacement). Must be
idempotent-safe: re-running consolidation must not double-count.

**Tests:** NOOP with a cited duplicate → survivor's count increases by exactly
the duplicate's, duplicate's own count zeroed or left but not double-counted on
a second run; UPDATE path likewise; entity with zero citations is a no-op.

## Task 4: shadow mode + calibration harness

Threshold `0.55` is uncalibrated — it was picked from a small probe, and the
whole point of the gate is that nobody has seen what consolidation would
actually do to a real corpus.

**Required behaviour:**
1. `callmem consolidate --dry-run` (new CLI command, or extend an existing one
   if a natural host exists): runs the FULL consolidation path over a project's
   existing entities — candidate lookup, threshold gate, batched judge — and
   reports what it WOULD do, writing nothing. Per-decision output: new entity,
   matched existing entity, similarity, verdict, and the judge's reason.
   Summary counts by verdict. Honour the fail-open contract (a judge failure
   reports as ADD/unknown, never as a would-archive).
2. `--threshold FLOAT` override so the same corpus can be swept at several
   thresholds without editing config.
3. `--limit N` to bound cost on large corpora, and print what was skipped.
4. Because this is the tool that will decide whether real memories get
   archived, its own correctness matters: the dry-run must reuse the SAME
   candidate-selection and judging code as the live path (no parallel
   reimplementation) — assert this structurally in a test.

**Tests:** dry-run writes nothing (DB byte-identical before/after — assert on
entity/archived counts and `superseded_by` set); `--threshold` changes the
candidate set; `--limit` bounds judge calls and reports the skip; judge failure
in dry-run reports fail-open, never a would-archive; the shared-code assertion.

## Out of scope (controller runs these after merge)

Calibration sweep on real corpora, threshold selection, canary enablement on a
single project, and post-enable verification. Do not enable consolidation in
any config as part of these tasks.
