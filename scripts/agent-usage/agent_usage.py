#!/usr/bin/env python3
"""Read-only, model-free Multica run collection and evidence-based daily reports."""

import argparse
import concurrent.futures
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

VERSION = "1.1.0"
TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
TERMINAL = {"completed", "failed", "cancelled"}
CURSOR = re.compile(r'More runs available; use --before "([^"\n]+)"')


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CLI:
    def __init__(self, executable="multica", timeout=60):
        self.executable, self.timeout = executable, timeout

    def read(self, *args):
        try:
            p = subprocess.run([self.executable, *args, "--output", "json"],
                               capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"CLI timeout after {self.timeout}s: {' '.join(args[:3])}") from exc
        if p.returncode:
            # Do not persist stderr: authentication errors can contain sensitive context.
            raise RuntimeError(f"CLI failed (exit {p.returncode}, {failure(p.stderr)}): {' '.join(args[:3])}")
        return json.loads(p.stdout), p.stderr


def failure(error):
    error = (error or "").lower()
    if "limit" in error and "hit your" in error:
        return "Claude quota"
    for label, patterns in [
        ("Model capacity", ("at capacity",)),
        ("Model/config", ("not supported", "invalid model", "not found")),
        ("Auth/access", ("unauthorized", "not logged", "authenticate", "403", "different owners",
                         "401", "token", "credentials", "permission")),
        ("Network/routing/timeout", ("connection", "network", "routing discovery", "tls", "timed out", "timeout")),
        ("Runtime/process", ("daemon", "task cancelled", "process", "thread/start", "signal", "stdin", "pipe", "exited")),
    ]:
        if any(p in error for p in patterns):
            return label
    return "Other/unknown"


def quota_reset(error, created):
    """Parse only explicit provider reset times; unknown is never an inferred date."""
    m = re.search(r"resets\s+(?:(\w{3})\s+(\d{1,2})\s+at\s+)?(\d{1,2})(?::(\d{2}))?(am|pm)\s+\(([^)]+)\)",
                  error or "", re.I)
    if not m:
        return None
    month, day, hour, minute, ampm, zone = m.groups()
    try:
        local = stamp(created).astimezone(ZoneInfo(zone))
        hour = int(hour)
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
        result = local.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
        if month:
            month_number = datetime.strptime(month.title(), "%b").month
            result = result.replace(month=month_number, day=int(day))
            if result < local - timedelta(days=1):
                result = result.replace(year=result.year + 1)
        elif result <= local:
            result += timedelta(days=1)
        return iso(result)
    except (ValueError, KeyError):
        return None


def normalize(task):
    for key in ("id", "agent_id", "created_at", "status"):
        if not task.get(key):
            raise ValueError(f"run missing {key}")
    stamp(task["created_at"])
    out = {k: task.get(k) for k in ("id", "agent_id", "runtime_id", "issue_id", "created_at",
            "started_at", "completed_at", "status", "wakeup_id", "attempt", "kind")}
    out["failure_category"] = failure(task.get("error")) if task["status"] == "failed" else None
    out["quota_reset_at"] = quota_reset(task.get("error"), task["created_at"]) if out["failure_category"] == "Claude quota" else None
    out["quota_kind"] = ("weekly" if "weekly limit" in (task.get("error") or "").lower() else "session") if out["failure_category"] == "Claude quota" else None
    usages = {}
    for u in task.get("usage") or []:
        key = (u.get("provider") or "unknown", u.get("model") or "unknown")
        row = {"provider": key[0], "model": key[1]}
        for token in TOKENS:
            value = u.get(token)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"invalid {token} in run {out['id']}")
            row[token] = value
        if key in usages and usages[key] != row:
            raise ValueError(f"conflicting usage rows in run {out['id']}")
        usages[key] = row
    out["usage"] = list(usages.values())
    return out


def page_agent(cli, agent_id, lower, upper, max_pages):
    runs, cursor, cursors, pages, duplicate_rows = {}, None, set(), [], 0
    previous_oldest = None
    for _ in range(max_pages):
        args = ["agent", "tasks", agent_id, "--limit", "200"]
        if cursor:
            args += ["--before", cursor]
        rows, stderr = cli.read(*args)
        if not isinstance(rows, list):
            raise ValueError(f"agent {agent_id}: expected a JSON array")
        times = [stamp(t["created_at"]) for t in rows]
        if times != sorted(times, reverse=True) or (times and previous_oldest and times[0] > previous_oldest):
            raise ValueError(f"agent {agent_id}: pages not ordered by creation time")
        for task in rows:
            if task.get("agent_id") != agent_id:
                raise ValueError("agent-scoped response contains another agent's run")
            at = stamp(task["created_at"])
            if lower <= at < upper:
                normalized = normalize(task)
                if task["id"] in runs:
                    duplicate_rows += 1
                runs[task["id"]] = normalized
        match = CURSOR.search(stderr)
        next_cursor = match.group(1) if match else None
        pages.append({"before": cursor, "rows": len(rows), "next_cursor": next_cursor,
                      "oldest": iso(min(times)) if times else None})
        crossed = bool(times and min(times) < lower)
        if crossed or not next_cursor:
            return list(runs.values()), {"agent_id": agent_id, "lower": iso(lower), "upper": iso(upper),
                "pages": pages, "unique_runs": len(runs), "duplicate_rows": duplicate_rows,
                "complete": True, "stop": "crossed_lower_bound" if crossed else "exhausted"}
        if next_cursor in cursors or not rows:
            raise ValueError("pagination cursor did not advance")
        cursors.add(next_cursor)
        cursor, previous_oldest = next_cursor, min(times)
    raise ValueError(f"agent {agent_id}: exceeded max pages ({max_pages}); state not advanced")


def database(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    db = sqlite3.connect(state / "runs.sqlite")
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
    return db


def meta(db, key, default=None):
    row = db.execute("SELECT body FROM metadata WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, json.dumps(value)))


@contextmanager
def state_lock(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    with (state / "collection.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another collector owns this state directory") from exc
        yield


def collect(args):
    with state_lock(args.state):
        db = database(args.state)
        committed = False
        try:
            code_sha = fingerprint(__file__)
            old = meta(db, "collection")
            since, until = stamp(args.since), stamp(args.until)
            if since >= until:
                raise ValueError("since must precede until")
            if until > datetime.now(timezone.utc):
                raise ValueError("collection cutoff must not be in the future")
            if old and old["since"] != iso(since):
                raise ValueError("changing the baseline start requires a separate state directory")
            if old and until < stamp(old["until"]):
                raise ValueError("collection cutoff cannot go backwards")
            cli = CLI(args.cli, args.timeout)
            agents, _ = cli.read("agent", "list", "--include-archived")
            runtimes, _ = cli.read("runtime", "list")
            if not isinstance(agents, list) or not agents:
                raise ValueError("empty or malformed agent inventory")
            known_agents = {a["id"] for a in meta(db, "agents", [])}
            # Preserve archived/deleted agents still visible when this state was seeded.
            agent_ids = {a["id"] for a in agents} | known_agents
            pending = {}
            for (body,) in db.execute("SELECT body FROM runs"):
                run = json.loads(body)
                if run["status"] not in TERMINAL:
                    aid = run["agent_id"]
                    pending[aid] = min(pending.get(aid, until), stamp(run["created_at"]))
            lower = max(since, stamp(old["until"]) - timedelta(hours=args.lookback_hours)) if old else since
            if args.refresh_since:
                lower = min(lower, max(since, stamp(args.refresh_since)))
            results, manifests = [], []

            def fetch(aid):
                start = min(lower, pending.get(aid, lower)) if aid in known_agents else since
                identity = json.dumps([aid, iso(start), iso(until), code_sha])
                cache = args.state / "staging" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
                if cache.exists():
                    saved = json.loads(cache.read_text())
                    return saved["runs"], saved["manifest"]
                rows, manifest = page_agent(cli, aid, start, until, args.max_pages)
                manifest["observed_at"] = iso(datetime.now(timezone.utc))
                write_json(cache, {"runs": rows, "manifest": manifest})
                return rows, manifest

            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(fetch, aid) for aid in sorted(agent_ids)]
                for f in concurrent.futures.as_completed(futures):
                    rows, manifest = f.result()
                    results.extend(rows)
                    manifests.append(manifest)
            if len({r["id"] for r in results}) != len(results):
                raise ValueError("a run appeared under multiple agents")
            completed = iso(datetime.now(timezone.utc))
            manifest = {"version": VERSION, "source_sha256": code_sha, "since": iso(since),
                "until": iso(until), "collected_at": completed, "complete": True,
                "agent_count": len(agent_ids), "fetched_unique_runs": len(results),
                "agents": sorted(manifests, key=lambda m: m["agent_id"])}
            with db:
                for run in results:
                    db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)", (run["id"], json.dumps(run)))
                set_meta(db, "agents", [{k: a.get(k) for k in ("id", "name", "runtime_id", "model", "archived_at")} for a in agents])
                set_meta(db, "runtimes", [{k: r.get(k) for k in ("id", "name")} for r in runtimes])
                set_meta(db, "collection", manifest)
                write_json(args.state / "collection.json", manifest)
                write_json(args.state / "last-attempt.json", {"ok": True, "at": completed, "until": iso(until)})
            committed = True
            try:
                for cache in (args.state / "staging").glob("*.json"):
                    cache.unlink(missing_ok=True)
            except OSError:
                print("WARNING: committed successfully; staging cleanup deferred", file=sys.stderr)
            print(json.dumps({"ok": True, "fetched_unique_runs": len(results),
                              "stored_unique_runs": db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                              "agents": len(agent_ids), "until": iso(until)}))
        except Exception as exc:
            write_json(args.state / "last-attempt.json", {"ok": False, "at": iso(datetime.now(timezone.utc)),
                       "error": str(exc), "watermark_advanced": committed})
            raise
        finally:
            db.close()


def load_events(path):
    events = {}
    if not path:
        return []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not event.get("id") or not event.get("evidence_ref") or not event.get("kind"):
            raise ValueError("each observation needs a stable id and evidence_ref")
        if event["kind"] not in {"no_change", "fallback", "delivery", "optimization"}:
            raise ValueError("unknown observation kind")
        if event["kind"] == "no_change" and not event.get("run_id"):
            raise ValueError("no_change observation needs run_id")
        if event["kind"] == "delivery":
            if event.get("verdict") not in {"completed", "accepted", "rework"} or not event.get("issue_id"):
                raise ValueError("delivery needs issue_id and explicit verdict")
            if not event.get("at"):
                raise ValueError("delivery needs at")
            stamp(event["at"])
            if not isinstance(event.get("run_ids", []), list):
                raise ValueError("delivery run_ids must be an array")
        if event["kind"] == "optimization":
            if not event.get("effective_at"):
                raise ValueError("optimization needs effective_at")
            stamp(event["effective_at"])
        if event["kind"] == "fallback":
            if event.get("result") not in {"success", "failure", "pending", "unknown"}:
                raise ValueError("fallback needs an explicit result")
            if not event.get("triggered_at"):
                raise ValueError("fallback needs triggered_at")
            stamp(event["triggered_at"])
            if event.get("duplicate_execution") is not None and type(event["duplicate_execution"]) is not bool:
                raise ValueError("fallback duplicate_execution must be boolean or null")
            if event.get("started_at") and stamp(event["started_at"]) < stamp(event["triggered_at"]):
                raise ValueError("fallback cannot start before it is triggered")
        if event["id"] in events and events[event["id"]] != event:
            raise ValueError("conflicting observation id")
        events[event["id"]] = event
    return list(events.values())


def bucket():
    return {"runs": 0, "usage_missing_runs": 0, "usage_missing_rate": None,
            **{k: {"reported_sum": None, "reported_rows": 0, "missing_rows": 0} for k in TOKENS}}


def add_usage(group, usages):
    group["runs"] += 1
    if not usages:
        group["usage_missing_runs"] += 1
        for token in TOKENS:
            group[token]["missing_rows"] += 1
    for u in usages:
        for token in TOKENS:
            if u[token] is None:
                group[token]["missing_rows"] += 1
            else:
                group[token]["reported_sum"] = (group[token]["reported_sum"] or 0) + u[token]
                group[token]["reported_rows"] += 1
    group["usage_missing_rate"] = group["usage_missing_runs"] / group["runs"]


def model_usage(runs):
    groups = {}
    for run in runs:
        usages = {}
        for row in run["usage"]:
            usages.setdefault((row["provider"], row["model"]), []).append(row)
        if not usages:
            usages[("unknown", "unknown")] = []
        for key, rows in usages.items():
            add_usage(groups.setdefault(key, bucket()), rows)
    return [{"provider": k[0], "model": k[1], **v} for k, v in sorted(groups.items())]


def run_coverage(runs):
    missing = sum(not r["usage"] for r in runs)
    return {"runs": len(runs), "usage_missing_runs": missing,
            "usage_missing_rate": missing / len(runs) if runs else None}


def quota_repeats(runs):
    known, repeated, unknown = {}, [], []
    # Knowledge starts when the failing run ends, not when it was created.
    actions = []
    for r in runs:
        if r.get("started_at"):
            actions.append((stamp(r["started_at"]), 0, r))
        if r["failure_category"] == "Claude quota" and r.get("completed_at"):
            actions.append((stamp(r["completed_at"]), 1, r))
        elif r["status"] == "completed" and r["usage"] and r.get("completed_at"):
            actions.append((stamp(r["completed_at"]), 2, r))
    for at, action, r in sorted(actions, key=lambda a: (a[0], a[1])):
        scope = r.get("runtime_id")
        if not scope:
            continue
        if action == 0:
            k = known.get(scope)
            if k and at < k["reset"]:
                repeated.append({"run_id": r["id"], "known_from_run_id": k["id"], "scope": scope,
                                 "reset_at": iso(k["reset"])})
        elif action == 1:
            reset = stamp(r["quota_reset_at"]) if r["quota_reset_at"] else None
            if reset and reset - at >= timedelta(minutes=30):
                known[scope] = {"reset": reset, "id": r["id"]}
            elif not reset:
                # An unknown reset is diagnostic evidence, never a timed protection interval.
                unknown.append(r["id"])
        else:
            known.pop(scope, None)
    return repeated, unknown


def overlaps(runs):
    groups, found = {}, []
    for r in runs:
        if r.get("issue_id") and r.get("started_at") and r.get("completed_at"):
            groups.setdefault((r["agent_id"], r["issue_id"]), []).append(r)
    for group in groups.values():
        active = []
        for r in sorted(group, key=lambda r: stamp(r["started_at"])):
            start = stamp(r["started_at"])
            active = [p for p in active if stamp(p["completed_at"]) > start]
            for p in active:
                found.append([p["id"], r["id"]])
            active.append(r)
    return found


def summarize(all_runs, since, until, zone, events, mapping):
    selected = [r for r in all_runs if since <= stamp(r["created_at"]) < until]
    selected.sort(key=lambda r: (stamp(r["created_at"]), r["id"]))
    by_id = {r["id"]: r for r in all_runs}
    groups, statuses, failures, daily = {}, {}, {}, {}
    date = since.astimezone(zone).date()
    last_date = (until - timedelta(microseconds=1)).astimezone(zone).date()
    while date <= last_date:
        daily[date.isoformat()] = {"same_agent_issue_repeat_starts": 0}
        date += timedelta(days=1)
    annotations = {e["run_id"]: e for e in events if e["kind"] == "no_change"}
    no_change = []
    quota_all, unknown_resets = quota_repeats(all_runs)
    selected_ids = {r["id"] for r in selected}
    repeated = [r for r in quota_all if r["run_id"] in selected_ids]
    for r in selected:
        day = stamp(r["created_at"]).astimezone(zone).date().isoformat()
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        if r["failure_category"]:
            failures[r["failure_category"]] = failures.get(r["failure_category"], 0) + 1
        if r["id"] in annotations:
            no_change.append({"run_id": r["id"], "day": day, "usage": r["usage"],
                              "evidence_ref": annotations[r["id"]]["evidence_ref"]})
        by_model = {}
        for u in r["usage"]:
            by_model.setdefault((u["provider"], u["model"]), []).append(u)
        if not by_model:
            by_model[("unknown", "unknown")] = []
        for (provider, model), usages in by_model.items():
            account = mapping.get(r.get("runtime_id"), {})
            # Runtime is an observed run field. Machine/account are explicit, evidence-qualified mappings.
            key = (day, r.get("runtime_id"), account.get("machine", "unknown"), r["agent_id"],
                   r.get("issue_id"), provider, model, account.get("account_alias", "unknown"),
                   account.get("evidence_level", "unavailable"))
            add_usage(groups.setdefault(key, bucket()), usages)
    repeat_keys = {}
    for r in selected:
        if r.get("issue_id") and r.get("started_at"):
            day = stamp(r["created_at"]).astimezone(zone).date().isoformat()
            k = (day, r["agent_id"], r["issue_id"])
            if k in repeat_keys:
                daily[day]["same_agent_issue_repeat_starts"] += 1
            repeat_keys[k] = True
    deliveries, fallback = [], []
    for e in events:
        if e["kind"] == "delivery" and since <= stamp(e["at"]) < until:
            deliveries.append(e)
        if e["kind"] == "fallback" and since <= stamp(e["triggered_at"]) < until:
            item = dict(e)
            item["continuation_seconds"] = ((stamp(e["started_at"]) - stamp(e["triggered_at"])).total_seconds()
                                               if e.get("started_at") else None)
            fallback.append(item)
    # A blank ledger means uncollected, not zero accepted/reworked tasks.
    for day in {stamp(e["at"]).astimezone(zone).date().isoformat() for e in deliveries} | set(daily):
        d = daily.setdefault(day, {"same_agent_issue_repeat_starts": 0})
        day_runs = [r for r in selected if stamp(r["created_at"]).astimezone(zone).date().isoformat() == day]
        d["coverage"] = run_coverage(day_runs)
        d["model_usage"] = model_usage(day_runs)
        de = [e for e in deliveries if stamp(e["at"]).astimezone(zone).date().isoformat() == day]
        d["delivery_coverage"] = "explicit_observations_only" if de else "not_collected"
        for verdict in ("completed", "accepted", "rework"):
            d[verdict + "_tasks"] = len({e["issue_id"] for e in de if e["verdict"] == verdict}) if de else None
    units = []
    for e in deliveries:
        linked = [by_id[rid] for rid in dict.fromkeys(e.get("run_ids", [])) if rid in by_id]
        units.append({"observation_id": e["id"], "issue_id": e["issue_id"], "verdict": e["verdict"],
                      "task_type": e.get("task_type", "unknown"), "stage": e.get("stage", "unknown"),
                      "day": stamp(e["at"]).astimezone(zone).date().isoformat(),
                      "optimization_phases": {o["id"]: "after" if stamp(e["at"]) >= stamp(o["effective_at"]) else "before"
                                              for o in events if o["kind"] == "optimization"},
                      "linked_run_coverage": run_coverage(linked), "model_usage": model_usage(linked),
                      "missing_linked_run_ids": sorted(set(e.get("run_ids", [])) - set(by_id)),
                      "attribution": "explicit run links only; unlinked consumption excluded"})
    fields = ("day", "runtime_id", "machine", "agent_id", "issue_id", "provider", "model", "account_alias", "account_evidence_level")
    return {"window": {"since": iso(since), "until": iso(until), "timezone": str(zone)},
        "run_count": len(selected), "statuses": statuses, "failure_categories": failures,
        "usage_missing_runs": sum(not r["usage"] for r in selected),
        "groups": [{**dict(zip(fields, k)), **v} for k, v in groups.items()], "daily": daily,
        "audited_no_change": no_change, "no_change_coverage": "audited observations only; unannotated runs unknown",
        "known_long_quota_repeat_starts": repeated,
        "missing_started_at_run_ids": [r["id"] for r in selected if not r.get("started_at")],
        "unmatched_no_change_run_ids": sorted(set(annotations) - selected_ids),
        "quota_unknown_reset_run_ids": [rid for rid in unknown_resets if rid in selected_ids],
        "same_agent_issue_overlaps": overlaps(selected), "overlap_interpretation": "candidates, not proven duplicate work; only closed intervals",
        "fallback": {"coverage": "explicit_observations_only" if fallback else "not_collected",
                     "trigger_count": len(fallback) if fallback else None,
                     "success_count": sum(e["result"] == "success" for e in fallback) if fallback else None,
                     "failure_count": sum(e["result"] == "failure" for e in fallback) if fallback else None,
                     "proven_duplicate_count": sum(e.get("duplicate_execution") is True for e in fallback) if fallback else None,
                     "duplicate_unknown_count": sum(e.get("duplicate_execution") is None for e in fallback) if fallback else None,
                     "events": fallback}, "unit_deliveries": units,
        "optimizations": [e for e in events if e["kind"] == "optimization"],
        "limitations": ["Run completion is not task completion or acceptance.",
            "Token buckets remain separate; no inferred monetary cost.",
            "Missing model usage cannot be attributed using the agent's current model.",
            "Quota repetition is runtime-scoped; historical shared-account identity is unknown.",
            "Failure categories are keyword heuristics, not verified root causes.",
            "Historical input-token normalization is not established; provider/model rows remain separate.",
            "Stored zero cannot distinguish a reported zero from a provider field defaulted to zero.",
            "The platform hides some unstarted fallback/deferred/cancelled runs from this listing.",
            "A paginated collection is not a transactionally consistent server snapshot.",
            "Terminal runs may report usage later; refresh the historical range when reconciling."]}


def report(args):
    db = database(args.state)
    try:
        collection = meta(db, "collection")
        if not collection:
            raise ValueError("no successful collection")
        since, until = stamp(args.since), stamp(args.until)
        if since < stamp(collection["since"]) or until > stamp(collection["until"]) or since >= until:
            raise ValueError("report window must be inside the successfully collected window")
        runs = [json.loads(row[0]) for row in db.execute("SELECT body FROM runs")]
        mapping = json.loads(args.mapping.read_text()) if args.mapping else {}
        result = summarize(runs, since, until, ZoneInfo(args.timezone), load_events(args.observations), mapping)
        expected = getattr(args, "expect_no_change", None)
        if expected is not None and len(result["audited_no_change"]) != expected:
            raise ValueError(f"expected {expected} audited no-change runs, found {len(result['audited_no_change'])}; check unmatched_no_change_run_ids")
        result["provenance"] = {"version": VERSION, "source_sha256": fingerprint(__file__),
            "collected_at": collection["collected_at"],
            "collection_manifest_sha256": hashlib.sha256((json.dumps(collection, ensure_ascii=False, indent=2) + "\n").encode()).hexdigest(),
            "observations_sha256": fingerprint(args.observations) if args.observations else None,
            "mapping_sha256": fingerprint(args.mapping) if args.mapping else None}
        write_json(args.output, result)
        print(json.dumps({"report": str(args.output), "runs": result["run_count"],
                          "usage_missing_runs": result["usage_missing_runs"],
                          "audited_no_change": len(result["audited_no_change"])}))
    finally:
        db.close()


def daily(args):
    zone = ZoneInfo(args.timezone)
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day else
           datetime.now(zone).date() - timedelta(days=1))
    start = datetime.combine(day, datetime.min.time(), zone)
    end = start + timedelta(days=1)
    if end.astimezone(timezone.utc) > stamp(args.until):
        raise ValueError("daily reporting requires a complete day before the collection cutoff")
    load_events(args.observations)
    if args.mapping:
        json.loads(args.mapping.read_text())
    collect(args)
    args.output = args.output_dir / (day.isoformat() + ".json")
    args.since, args.until = iso(start), iso(end)
    report(args)


def verify_prior(args):
    root = args.prior
    checks = json.loads((root / "SHA256SUMS.json").read_text())
    for name, digest in checks.items():
        if fingerprint(root / name) != digest:
            raise ValueError(f"prior evidence hash mismatch: {name}")
    summary = json.loads((root / "summary.json").read_text())
    rows = list(csv.DictReader((root / "claude-run-usage.csv").open()))
    keys = [(r["run_id"], r["model"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate run/model in prior usage")
    failed = list(csv.DictReader((root / "failed-runs.csv").open()))
    if len({r["id"] for r in failed}) != len(failed):
        raise ValueError("duplicate failed run IDs")
    totals = {k: sum(int(r[k]) for r in rows) for k in TOKENS}
    expected = {k: sum(a[k] for a in summary["agents"]) for k in TOKENS}
    samples = json.loads((root / "nochange-evidence.json").read_text())
    samples_by_id = {e["run_id"]: e for e in samples}
    if len(samples_by_id) != len(samples):
        raise ValueError("duplicate no-change sample IDs")
    usage_by_id = {}
    for r in rows:
        usage_by_id.setdefault(r["run_id"], []).append(r)
    for rid, sample in samples_by_id.items():
        for k in TOKENS:
            if sum(int(r[k]) for r in usage_by_id.get(rid, [])) != sample["usage"][k]:
                raise ValueError(f"sample usage mismatch {rid}: {k}")
    if totals != expected or len(failed) != summary["windows"]["7days"]["statuses"]["failed"]:
        raise ValueError("prior aggregates do not reconcile")
    facts = {"verified": True, "prior_window": summary["windows"], "claude_reported_unique_runs": len(usage_by_id),
        "claude_usage_rows": len(rows), "claude_totals": totals, "failed_unique_runs": len(failed),
        "audited_no_change_count": len(samples),
        "audited_no_change_tokens": {k: sum(e["usage"][k] for e in samples) for k in TOKENS},
        "sample_run_ids": sorted(samples_by_id),
        "coverage": "Prior archive contains aggregates, not every original run; full-ID pagination requires collection manifest."}
    write_json(args.output, facts)
    if args.observations:
        args.observations.write_text("".join(json.dumps({"id": "prior-nochange-" + e["run_id"],
            "kind": "no_change", "run_id": e["run_id"],
            "evidence_ref": args.evidence_ref}, ensure_ascii=False) + "\n" for e in samples))
    print(json.dumps(facts, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name, func in (("collect", collect), ("daily", daily)):
        c = sub.add_parser(name)
        c.add_argument("--state", type=Path, required=True)
        c.add_argument("--since", required=True)
        c.add_argument("--until", default=iso(datetime.now(timezone.utc).replace(microsecond=0)))
        c.add_argument("--refresh-since")
        c.add_argument("--lookback-hours", type=int, default=48)
        c.add_argument("--workers", type=int, choices=range(1, 9), default=4)
        c.add_argument("--timeout", type=int, default=60)
        c.add_argument("--max-pages", type=int, default=500)
        c.add_argument("--cli", default="multica")
        if name == "daily":
            c.add_argument("--day", help="YYYY-MM-DD; defaults to yesterday in --timezone")
            c.add_argument("--timezone", default="Asia/Shanghai")
            c.add_argument("--observations", type=Path)
            c.add_argument("--mapping", type=Path)
            c.add_argument("--output-dir", type=Path, required=True)
            c.add_argument("--expect-no-change", type=int)
        c.set_defaults(func=func)
    r = sub.add_parser("report")
    for name in ("state", "observations", "mapping", "output"):
        r.add_argument("--" + name, type=Path, required=name in {"state", "output"})
    r.add_argument("--since", required=True)
    r.add_argument("--until", required=True)
    r.add_argument("--timezone", default="Asia/Shanghai")
    r.add_argument("--expect-no-change", type=int)
    r.set_defaults(func=report)
    v = sub.add_parser("verify-prior")
    v.add_argument("--prior", type=Path, required=True)
    v.add_argument("--output", type=Path, required=True)
    v.add_argument("--observations", type=Path)
    v.add_argument("--evidence-ref", default="prior archive: nochange-evidence.json")
    v.set_defaults(func=verify_prior)
    args = p.parse_args()
    try:
        if getattr(args, "timeout", 1) <= 0 or getattr(args, "lookback_hours", 1) <= 0:
            raise ValueError("timeout and lookback-hours must be positive")
        args.func(args)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
