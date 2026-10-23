import argparse
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

spec = importlib.util.spec_from_file_location("agent_usage", Path(__file__).with_name("agent_usage.py"))
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)

START = "2026-10-12T00:00:00Z"
END = "2026-10-19T00:00:00Z"


def task(id="r1", created="2026-10-18T00:00:00Z", **kwargs):
    return {"id": id, "agent_id": "a", "runtime_id": "runtime-a", "issue_id": "issue-a",
            "created_at": created, "status": "completed", **kwargs}


def usage(**kwargs):
    return {"provider": "claude", "model": "opus", "input_tokens": 6, "output_tokens": 10,
            "cache_read_tokens": 100, "cache_write_tokens": 20, **kwargs}


class FakeCLI:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def read(self, *args):
        self.calls.append(args)
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class CollectorTests(unittest.TestCase):
    def test_precise_cursor_and_id_deduplication(self):
        cursor = "2026-10-18T00:00:00.123456Z|r1"
        cli = FakeCLI([([task()], f'More runs available; use --before "{cursor}" to fetch the next page.'),
                       ([task(), task("old", START)], "")])
        rows, manifest = u.page_agent(cli, "a", u.stamp(START), u.stamp(END), 5)
        self.assertEqual({r["id"] for r in rows}, {"r1", "old"})
        self.assertEqual(cli.calls[1][-1], cursor)
        self.assertEqual(manifest["duplicate_rows"], 1)

    def test_cutoff_excludes_future_and_stops_after_crossing_lower_bound(self):
        cli = FakeCLI([([task("future", END), task(), task("old", "2026-10-11T23:59:59Z")],
                        'More runs available; use --before "cursor"')])
        rows, manifest = u.page_agent(cli, "a", u.stamp(START), u.stamp(END), 1)
        self.assertEqual([r["id"] for r in rows], ["r1"])
        self.assertEqual(manifest["stop"], "crossed_lower_bound")

    def test_repeated_cursor_fails_and_exact_full_terminal_page_is_valid(self):
        with self.assertRaises(ValueError):
            u.page_agent(FakeCLI([([task()], 'More runs available; use --before "same"')] * 2),
                         "a", u.stamp(START), u.stamp(END), 3)
        rows, manifest = u.page_agent(FakeCLI([([task(str(i)) for i in range(200)], "")]),
                                     "a", u.stamp(START), u.stamp(END), 3)
        self.assertEqual(len(rows), 200)
        self.assertTrue(manifest["complete"])

    def test_cli_failure_does_not_advance_state_and_rerun_replaces_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            args = argparse.Namespace(state=state, since=START, until=END, cli="multica",
                timeout=1, workers=1, max_pages=5, lookback_hours=48, refresh_since=None)
            def client(rows):
                return FakeCLI([([{"id": "a", "name": "agent"}], ""), ([], ""), rows])
            with patch.object(u, "CLI", return_value=client(([task(usage=[usage()])], ""))):
                u.collect(args)
            manifest = (state / "collection.json").read_bytes()
            with patch.object(u, "CLI", return_value=client(RuntimeError("offline"))):
                with self.assertRaises(RuntimeError):
                    u.collect(args)
            self.assertEqual(manifest, (state / "collection.json").read_bytes())
            self.assertFalse(json.loads((state / "last-attempt.json").read_text())["ok"])
            for _ in range(2):
                with patch.object(u, "CLI", return_value=client(([task(usage=[usage(output_tokens=12)])], ""))):
                    u.collect(args)
            db = u.database(state)
            rows = [json.loads(r[0]) for r in db.execute("SELECT body FROM runs")]
            db.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["usage"][0]["output_tokens"], 12)

    def test_unfinished_old_run_is_refetched(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            args = argparse.Namespace(state=state, since=START, until=END, cli="multica",
                timeout=1, workers=1, max_pages=5, lookback_hours=48, refresh_since=None)
            cli = FakeCLI([([{"id": "a"}], ""), ([], ""),
                           ([task("pending", START, status="running")], "")])
            with patch.object(u, "CLI", return_value=cli):
                u.collect(args)
            with patch.object(u, "CLI", return_value=FakeCLI([([{"id": "a"}], ""), ([], "")])):
                with patch.object(u, "page_agent", return_value=([], {"agent_id": "a"})) as paged:
                    u.collect(args)
                    self.assertEqual(paged.call_args.args[2], u.stamp(START))

    def test_failed_agent_resumes_cached_success_without_advancing_watermark(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            args = argparse.Namespace(state=state, since=START, until=END, cli="multica",
                timeout=1, workers=1, max_pages=5, lookback_hours=48, refresh_since=None)
            inventory = ([{"id": "a"}, {"id": "b"}], "")
            first = FakeCLI([inventory, ([], ""), ([task()], ""), RuntimeError("offline")])
            with patch.object(u, "CLI", return_value=first):
                with self.assertRaises(RuntimeError):
                    u.collect(args)
            db = u.database(state)
            self.assertIsNone(u.meta(db, "collection"))
            db.close()
            self.assertEqual(len(list((state / "staging").glob("*.json"))), 1)
            retry = FakeCLI([inventory, ([], ""), ([task("r2", agent_id="b")], "")])
            with patch.object(u, "CLI", return_value=retry):
                u.collect(args)
            self.assertEqual(len(retry.calls), 3)
            self.assertEqual(retry.calls[-1][2], "b")

    def test_future_cutoff_is_refused_before_cli_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(state=Path(temp), since=START,
                until=u.iso(u.datetime.now(u.timezone.utc) + timedelta(days=1)))
            with patch.object(u, "CLI") as cli:
                with self.assertRaisesRegex(ValueError, "future"):
                    u.collect(args)
                cli.assert_not_called()

    def test_cli_wrapper_separates_streams_handles_exit_and_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "fake-multica"
            executable.write_text('#!/usr/bin/env python3\nimport sys,time\n'
                'if "slow" in sys.argv: time.sleep(2)\n'
                'if "fail" in sys.argv: sys.exit(9)\n'
                'print("[]")\nprint("cursor metadata",file=sys.stderr)\n')
            executable.chmod(0o700)
            value, stderr = u.CLI(str(executable), 2).read("ok")
            self.assertEqual(value, [])
            self.assertIn("cursor metadata", stderr)
            with self.assertRaisesRegex(RuntimeError, "exit 9"):
                u.CLI(str(executable), 2).read("fail")
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                u.CLI(str(executable), 0.1).read("slow")


class ReportTests(unittest.TestCase):
    def report(self, rows, events=None):
        return u.summarize([u.normalize(r) for r in rows], u.stamp(START), u.stamp(END),
                           ZoneInfo("UTC"), events or [], {})

    def test_missing_usage_and_partial_fields_are_unknown_not_zero(self):
        result = self.report([task(), task("r2", usage=[usage(input_tokens=None, output_tokens=0)])])
        self.assertEqual(result["usage_missing_runs"], 1)
        known, unknown = sorted(result["groups"], key=lambda g: g["model"])
        self.assertIsNone(known["input_tokens"]["reported_sum"])
        self.assertEqual(known["output_tokens"]["reported_sum"], 0)
        self.assertIsNone(unknown["output_tokens"]["reported_sum"])
        self.assertEqual(unknown["usage_missing_rate"], 1)
        self.assertEqual(result["daily"]["2026-10-18"]["accepted_tasks"], None)

    def test_no_keyword_acceptance_and_only_audited_no_change(self):
        r = task(result="accepted passed completed; no change", usage=[usage()])
        self.assertEqual(self.report([r])["audited_no_change"], [])
        event = {"id": "audit", "kind": "no_change", "run_id": "r1", "evidence_ref": "review"}
        self.assertEqual(len(self.report([r], [event])["audited_no_change"]), 1)

    def test_quota_knowledge_starts_at_failure_completion_and_clears_on_success(self):
        err = "You've hit your weekly limit · resets Oct 20 at 11am (America/Los_Angeles)"
        rows = [task("quota", "2026-10-17T00:00:00Z", status="failed", error=err,
                     started_at="2026-10-17T00:00:00Z",
                     completed_at="2026-10-17T00:02:00Z"),
                task("before-known", "2026-10-17T00:01:00Z", started_at="2026-10-17T00:01:00Z"),
                task("repeat", "2026-10-17T00:03:00Z", started_at="2026-10-17T00:03:00Z",
                     usage=[usage()], completed_at="2026-10-17T00:04:00Z"),
                task("queued-cancelled", "2026-10-17T00:03:00Z", status="cancelled"),
                task("after-success", "2026-10-17T00:05:00Z", started_at="2026-10-17T00:05:00Z"),
                task("other-runtime", "2026-10-17T00:03:00Z", runtime_id="other")]
        result = self.report(rows)
        self.assertEqual([r["run_id"] for r in result["known_long_quota_repeat_starts"]], ["repeat"])
        self.assertIn("queued-cancelled", result["missing_started_at_run_ids"])
        self.assertEqual(u.quota_reset(err, "2026-10-17T00:00:00Z"), "2026-10-20T18:00:00Z")
        self.assertIsNone(u.quota_reset("limit unknown", START))

    def test_multiple_models_remain_separate_and_usage_duplicates_not_summed(self):
        result = self.report([task(usage=[usage(), usage(), usage(model="haiku", output_tokens=2)])])
        self.assertEqual(result["run_count"], 1)
        self.assertEqual(len(result["groups"]), 2)
        self.assertNotIn("usage", result["daily"]["2026-10-18"])
        self.assertEqual({r["model"]: r["output_tokens"]["reported_sum"] for r in
                          result["daily"]["2026-10-18"]["model_usage"]}, {"opus": 10, "haiku": 2})
        with self.assertRaises(ValueError):
            u.normalize(task(usage=[usage(), usage(output_tokens=999)]))

    def test_overlap_is_candidate_and_delivery_cost_uses_explicit_deduped_links(self):
        rows = [task(started_at="2026-10-18T00:00:00Z", completed_at="2026-10-18T00:03:00Z", usage=[usage()]),
                task("r2", started_at="2026-10-18T00:01:00Z", completed_at="2026-10-18T00:02:00Z")]
        events = [{"kind": "delivery", "id": "del", "issue_id": "issue-a", "verdict": "accepted",
                   "at": "2026-10-18T00:04:00Z", "run_ids": ["r1", "r1", "unknown"], "evidence_ref": "review"}]
        result = self.report(rows, events)
        self.assertEqual(result["same_agent_issue_overlaps"], [["r1", "r2"]])
        self.assertEqual(result["daily"]["2026-10-18"]["accepted_tasks"], 1)
        self.assertEqual(result["unit_deliveries"][0]["linked_run_coverage"]["runs"], 1)
        self.assertEqual(result["unit_deliveries"][0]["missing_linked_run_ids"], ["unknown"])
        self.assertIsNone(result["fallback"]["trigger_count"])

    def test_fallback_and_optimization_have_explicit_evidence(self):
        events = [{"kind": "fallback", "id": "switch", "result": "success",
                   "triggered_at": "2026-10-18T00:00:00Z", "started_at": "2026-10-18T00:02:00Z",
                   "duplicate_execution": None, "evidence_ref": "control ledger"},
                  {"kind": "optimization", "id": "change", "effective_at": "2026-10-17T00:00:00Z", "evidence_ref": "rollout"}]
        result = self.report([], events)
        self.assertEqual(result["fallback"]["success_count"], 1)
        self.assertEqual(result["fallback"]["duplicate_unknown_count"], 1)
        self.assertEqual(result["fallback"]["events"][0]["continuation_seconds"], 120)

    def test_daily_uses_previous_local_day_and_refuses_partial_day(self):
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(day="2026-10-18", timezone="Asia/Shanghai", until=END,
                                      since=START, output_dir=Path(temp), observations=None, mapping=None)
            with patch.object(u, "collect") as collect, patch.object(u, "report") as report:
                u.daily(args)
                collect.assert_called_once()
                report.assert_called_once()
                self.assertEqual(args.since, "2026-10-17T16:00:00Z")
                self.assertEqual(args.until, "2026-10-18T16:00:00Z")
            args.until = "2026-10-18T15:00:00Z"
            with patch.object(u, "collect") as collect:
                with self.assertRaises(ValueError):
                    u.daily(args)
                collect.assert_not_called()

    def test_observation_conflicts_and_malformed_boolean_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "observations.jsonl"
            event = {"kind": "fallback", "id": "switch", "result": "unknown",
                     "triggered_at": "2026-10-18T00:00:00Z", "duplicate_execution": "false", "evidence_ref": "ledger"}
            path.write_text(json.dumps(event))
            with self.assertRaisesRegex(ValueError, "boolean"):
                u.load_events(path)
            event["duplicate_execution"] = None
            changed = {**event, "result": "success"}
            path.write_text(json.dumps(event) + "\n" + json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "conflicting"):
                u.load_events(path)

    def test_daily_provider_models_and_unmatched_samples_remain_separate(self):
        rows = [task(usage=[usage(), usage(provider="openai", model="gpt", input_tokens=1000)])]
        events = [{"kind": "no_change", "id": "missing", "run_id": "missing-run", "evidence_ref": "audit"}]
        result = self.report(rows, events)
        self.assertEqual(result["unmatched_no_change_run_ids"], ["missing-run"])
        groups = result["daily"]["2026-10-18"]["model_usage"]
        self.assertEqual([(g["provider"],g["input_tokens"]["reported_sum"]) for g in groups],
                         [("claude",6),("openai",1000)])
        self.assertNotIn("input_tokens", result["daily"]["2026-10-18"]["coverage"])

    def test_expected_sample_count_fails_loudly(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            db = u.database(state)
            with db:
                u.set_meta(db, "collection", {"since": START, "until": END})
            db.close()
            args = argparse.Namespace(state=state, since=START, until=END, timezone="UTC",
                mapping=None, observations=None, expect_no_change=26, output=state/"report.json")
            with self.assertRaisesRegex(ValueError, "expected 26"):
                u.report(args)


if __name__ == "__main__":
    unittest.main()
