import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_token_meter as meter


def write_rows(path, rows, trailing_newline=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + ("\n" if trailing_newline else ""))


def claude_row(mid, stamp, read=0):
    return {"type": "assistant", "sessionId": "sample-session", "timestamp": stamp,
            "message": {"id": mid, "model": "sample-model", "usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": read, "output_tokens": 5}}}


def codex_row(stamp, full, cached):
    return {"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count",
            "info": {"last_token_usage": {"input_tokens": full,
            "cached_input_tokens": cached, "output_tokens": 4}}}}


class MeterTest(unittest.TestCase):
    def test_provider_months_and_cached_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stamp = "2026-10-20T12:00:00Z"
            write_rows(root / "claude" / "sample-project" / "one.jsonl",
                       [claude_row("first", stamp, 30), claude_row("first", stamp, 30)])
            write_rows(root / "codex" / "2026" / "10" / "rollout.jsonl", [
                {"type": "session_meta", "payload": {"id": "other-session", "cwd": "/sample/project"}},
                codex_row(stamp, 1000, 800),
                {"type": "event_msg", "timestamp": stamp,
                 "payload": {"type": "token_count", "info": {"last_token_usage": None}}},
            ])
            claude, diagnostics = meter.collect(root / "claude")
            codex, invalid = meter.collect_codex(root / "codex")
            self.assertEqual(diagnostics["duplicate_rows"], 1)
            self.assertEqual(invalid, 0)
            self.assertEqual(codex[0]["input_tokens"], 200)
            self.assertEqual(codex[0]["cache_read_input_tokens"], 800)
            self.assertEqual(codex[0]["context_tokens"], 1000)
            ratio = meter.load_pricing(meter.DEFAULT_PRICING_FILE)
            monthly = meter.summarize_providers(claude + codex, "month", ratio)
            self.assertEqual([row["provider"] for row in monthly], ["claude", "codex"])
            self.assertEqual(monthly[1]["full_price_equivalent"], 300)

    def test_incremental_cache_and_latest_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stamp = "2026-10-20T12:00:00Z"
            later = "2026-10-20T12:01:00Z"
            path = root / "claude" / "sample-project" / "one.jsonl"
            write_rows(path, [claude_row("first", stamp), claude_row("first", stamp)])
            codex_path = root / "codex" / "2026" / "rollout.jsonl"
            write_rows(codex_path, [
                {"type": "session_meta", "payload": {"id": "other-session", "cwd": "/sample/project"}},
                codex_row(stamp, 1000, 800), codex_row(later, 1200, 900)])
            state = meter.update_cache(root / "claude", {})
            meter.update_codex_cache(root / "codex", state)
            self.assertEqual(sum(row[0] for row in state["agg"].values()), 3)
            self.assertEqual(state["last"]["other-session"]["ctx"], 1200)
            self.assertEqual(state["first"]["other-session"]["ctx"], 1000)
            with path.open("a") as stream:
                stream.write(json.dumps(claude_row("second", later, 40)))
            meter.update_cache(root / "claude", state)
            self.assertEqual(sum(row[0] for row in state["agg"].values()), 3)
            with path.open("a") as stream:
                stream.write("\n")
            meter.update_cache(root / "claude", state)
            self.assertEqual(sum(row[0] for row in state["agg"].values()), 4)
            meter.update_codex_cache(root / "codex", state)
            self.assertEqual(sum(row[0] for row in state["agg"].values()), 4)
            summary = meter.build_summary(state, meter.load_pricing(meter.DEFAULT_PRICING_FILE),
                                          now=datetime.fromisoformat(later.replace("Z", "+00:00")).timestamp() + 30)
            self.assertEqual(summary["month_by_provider"]["codex"]["api_calls"], 2)
            self.assertEqual(next(row for row in summary["live"] if row["provider"] == "codex")["ctx"], 1200)

    def test_cli_paths_state_and_pricing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stamp = "2026-10-20T12:00:00Z"
            write_rows(root / "claude" / "sample-project" / "one.jsonl", [claude_row("first", stamp)])
            pricing = root / "weights.json"
            pricing.write_text(json.dumps({"input": 1, "cache_write": 2,
                                           "cache_read": 0, "output": 1}))
            args = ["--data-dir", str(root / "claude"), "--codex-data-dir", str(root / "empty"),
                    "--state-dir", str(root / "state"), "--pricing-file", str(pricing), "--json"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(meter.main(args), 0)
            self.assertEqual(json.loads(output.getvalue())["rows"][0]["full_price_equivalent"], 55)
            with contextlib.redirect_stdout(output := io.StringIO()):
                self.assertEqual(meter.main(["--cache"] + args), 0)
            self.assertTrue((root / "state" / "state.json").exists())
            self.assertTrue((root / "state" / "summary.json").exists())
            self.assertIn("month_by_provider", json.loads(output.getvalue()))

    def test_xdg_state_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("XDG_STATE_HOME")
            try:
                os.environ["XDG_STATE_HOME"] = tmp
                self.assertEqual(meter.default_state_dir(), Path(tmp) / "agent-token-meter")
            finally:
                if previous is None:
                    os.environ.pop("XDG_STATE_HOME", None)
                else:
                    os.environ["XDG_STATE_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
