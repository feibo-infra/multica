# Programmatic agent usage baseline

Python 3.9+ and an authenticated `multica` CLI are the only dependencies. The
collector runs read-only CLI commands. It does not invoke models, retry failed
business work, post comments, modify agents, or install a schedule.

Keep state, observations and reports **outside the repository**. They contain
workspace identifiers and belong in issue attachments or the existing statistics
pipeline, rather than Git. Use a dedicated state directory; do not share a
takeover controller's state files.

## Collection and daily integration

From the repository root, seed a fixed run-creation window once:

```sh
python3 scripts/agent-usage/agent_usage.py collect \
  --state /path/to/private-state --since 2026-10-12T02:35:00Z
```

Subsequent calls with the same `--since` are incremental. They refresh the last
48 hours and any older unfinished runs. A newly discovered agent is backfilled
to the baseline start. Include archived agents. Every page's exact CLI cursor
(including its fractional timestamp and UUID) is kept in `collection.json`.
Runs have a unique SQLite primary key; refreshing replaces their entire
provider/model usage snapshot instead of accumulating it again. Inventories
persist only IDs, names, runtime IDs, model names and archive dates. Run records
exclude prompts, results, account emails, credentials and raw error messages.

To collect and write yesterday's report in one foreground command:

```sh
python3 scripts/agent-usage/agent_usage.py daily \
  --state /path/to/private-state --since 2026-10-12T02:35:00Z \
  --output-dir /path/to/private-reports \
  --observations /path/to/observations.jsonl --mapping /path/to/mapping.json
```

`--day YYYY-MM-DD` selects another completed day. Default timezone is
`Asia/Shanghai`; `--timezone` is explicit in every report. Without observation
or mapping files, omit those flags and their fields remain unknown. A report of
an arbitrary collected window does not contact the server:

```sh
python3 scripts/agent-usage/agent_usage.py report \
  --state /path/to/private-state \
  --since 2026-10-12T02:35:00Z --until 2026-10-19T02:35:00Z \
  --expect-no-change 26 \
  --output /path/to/baseline.json
```

The existing daily statistics job can call `daily` and consume the dated JSON.
Its owner must approve the installation location, execution time and retention
policy before enabling it. No extra model-based daily report is needed. The
same invocation returns nonzero on failure; the existing job should surface
that failure using its own notification channel.

## Completeness and failure behavior

- A collection commits only after **every** agent finishes its pages. A CLI
  error, timeout, malformed response, repeated cursor or page limit fails the
  collection and keeps its watermark. A full terminal page without a cursor is
  valid: the current CLI exposes the server's authoritative next-cursor header.
- Calls have a timeout (60 seconds by default), a page cap (500 per agent),
  bounded concurrency (4, maximum 8) and no automatic retries. stdout JSON and
  stderr pagination metadata are parsed separately. Raw error output is not
  saved. `last-attempt.json` records success/failure and whether state advanced.
- Successful per-agent reads are staged separately during a failed collection.
  Rerun with the same fixed `--until` and script version to reuse them and fill
  only missing agents. Staged reads retain their original observation times;
  they do not advance the committed watermark. A successful commit cleans them.
- `collection.lock` prevents concurrent writers and is released by the OS on
  exit. Do not delete it to break a live process's lock. An interrupted run can
  be rerun with the same state. State commits are atomic SQLite transactions;
  `collection.json` is an exported copy of metadata stored in the database.
  The dedicated state directory is restricted to its owner (0700).
- A cutoff cannot be in the future. Pagination is not a transactionally
  consistent server snapshot; statuses and usage are observed at collection
  time, while membership in the window is by run creation time.
- Final usage can arrive more than 48 hours late. For historical reconciliation,
  run `collect --refresh-since <timestamp>` in addition to the usual flags.
  Final missing-usage rows remain unknown until such a refresh supplies usage.
  An indefinitely unfinished run keeps its agent's lookback open and can
  eventually hit the page cap; this remains a visible collection failure,
  rather than silently abandoning the run.
- Reports outside the successful collection window are refused. All IDs and
  coverage remain available for review; the collector cannot see inaccessible
  or permanently deleted agents and does not claim whole-provider billing
  coverage.

## Report interpretation

`groups` retain date, runtime, explicit machine/account mapping, agent, issue,
provider and model. An absent usage report is its own `unknown` model group;
the current agent model is never used to reconstruct historical consumption.
`usage_missing_runs` and `usage_missing_rate` are per group, with a separate
global missing-run count. Each of the four token buckets has `reported_sum`,
`reported_rows` and `missing_rows`. All-unobserved sums are `null`; an integer
zero from the API stays zero. The database defaults absent provider counters to
zero, so this API cannot distinguish a real zero from an unreported cache
counter. Buckets are not added into a price or total token equivalent.
Recent adapters normalize input to exclude both cache buckets, but the
normalization/backfill deployment time for historical rows is not established.
The collector preserves the API counters, separates every provider/model in
both daily and unit-delivery output, and does not re-add reasoning output.
Daily `coverage` contains run counts and missing rates only; `model_usage`
contains each provider/model's separate token buckets.

The daily output includes completed/accepted/rework task counts only from
explicit observations. `completed` **run** status is never treated as a
completed or accepted **issue**. Empty delivery and fallback ledgers are marked
`not_collected` with `null` counts. A nonempty ledger gives observed counts,
not a claim of complete workspace coverage. No keyword is used to infer an
acceptance, no-change poll or successful fallback.

Repeated starts on the same agent/issue/day and closed execution intervals
that overlap are diagnostic candidates. They do not prove repeated business
work. Long-quota repetition requires a previous quota failure to have ended,
an explicitly parsed reset at least 30 minutes later, and another start before
that reset on the same runtime. Only runs with an actual `started_at` qualify;
missing start times appear separately in `missing_started_at_run_ids`.
A completed run with reported usage clears the
runtime interval. Cross-runtime shared-account history is not inferred.
`quota_unknown_reset_run_ids` retains parse gaps. These are not counted as
known timed intervals.

`audited_no_change` links reviewed run IDs to evidence.
`unmatched_no_change_run_ids` lists samples absent from the database or outside
the report window; `--expect-no-change N` fails explicitly on a count mismatch.
`fallback.events`
retain explicit success/failure/pending/unknown, continuation delay and known
or unknown duplicate execution. `unit_deliveries` count consumption only for
explicitly linked, deduplicated run IDs, including links created before the
report day. They retain missing links, task type, stage and before/after labels
for each recorded optimization. Compare matching task types and stages, and
keep usage coverage visible. They do not claim all unlinked cost belongs to
that delivery. Linked consumption also remains separated by provider/model.
`optimizations` records observed effective times, not planned
rollout dates.

## Observation and mapping contracts

Observations are JSONL from the existing statistics/acceptance/control process.
Every row needs a stable `id`, `kind` and `evidence_ref`. Duplicate identical IDs
are ignored; conflicting IDs fail. Examples below are illustrative, **not
collected facts**:

```jsonl
{"id":"audit-1","kind":"no_change","run_id":"RUN_UUID","evidence_ref":"reviewed execution log"}
{"id":"delivery-1","kind":"delivery","issue_id":"ISSUE_UUID","at":"2026-10-18T08:00:00Z","verdict":"accepted","task_type":"bugfix","stage":"acceptance","run_ids":["RUN_UUID"],"evidence_ref":"review decision"}
{"id":"switch-1","kind":"fallback","triggered_at":"2026-10-18T08:00:00Z","started_at":"2026-10-18T08:02:00Z","result":"success","duplicate_execution":null,"source_run_id":"SOURCE_UUID","target_run_id":"TARGET_UUID","evidence_ref":"takeover ledger"}
{"id":"optimization-1","kind":"optimization","effective_at":"2026-10-18T08:00:00Z","evidence_ref":"confirmed installation record"}
```

Delivery verdicts: `completed`, `accepted`, `rework`. Fallback results:
`success`, `failure`, `pending`, `unknown`. Missing fallback fields stay
uncollected until the controlling implementation exports them. This collector
does not modify that implementation or its live control files.

Mapping JSON is keyed by the **historical run runtime ID**, not the current
agent. It stores pseudonyms only:

```json
{"RUNTIME_UUID":{"machine":"machine-b","account_alias":"unknown","evidence_level":"runtime metadata only","evidence_ref":"inventory snapshot"}}
```

A machine/runtime mapping is not verified account attribution. Do not put
credentials, login emails or subscription account identifiers in these files.
If account binding changed over time, leave the account alias unknown instead
of applying a current account to the entire baseline.

## Prior diagnostic archive verification

The optional `verify-prior` command accepts an extracted archive containing
`SHA256SUMS.json`, `summary.json`, `claude-run-usage.csv`, `failed-runs.csv`, and
`nochange-evidence.json`. It validates hashes, run/model uniqueness, failure
IDs, group totals, and each audited sample's usage. It can export the reviewed
sample IDs as observations, avoiding another model-based analysis:

```sh
python3 scripts/agent-usage/agent_usage.py verify-prior \
  --prior /path/to/extracted-prior --output /path/to/prior-check.json \
  --observations /path/to/prior-observations.jsonl \
  --evidence-ref 'https://example.com/original-diagnostic'
```

The verifier does not pretend aggregate-only prior evidence contains all run
IDs. Compare a fresh collection's creation-window count and common IDs, while
allowing documented final-state/late-usage drift.
The platform hides some unstarted fallback/deferred/cancelled tasks from agent
run listings. Counts are visible listed runs, not every internal task record.
Failure classifications are keyword heuristics, not independently verified
root causes.

## Validation and rollback

```sh
python3 -m unittest discover -s scripts/agent-usage -p 'test_*.py' -v
git diff --check
```

Rollback: remove the invocation from the existing statistics job if installed;
keep its last successful output until replacement is verified. Revert the
script commit to withdraw the tool. The script has no service, launch agent,
external write, provider call, or product deployment to undo. Back up the private
state directory before deliberately resetting the baseline.
