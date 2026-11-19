import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import alerts
from jobs import match, normalize

BOARD = ("ashby", "example", "Example")
RAW = dict(title="ML Engineer", id="a", location="Bengaluru", descriptionPlain="0–2 years of experience. Python and SQL.",
           applyUrl="https://jobs.example.org/open/a", publishedAt="2026-11-18T12:00:00Z")
NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def candidate(**changes):
    return match(normalize(BOARD, dict(RAW, **changes)), NOW)


class Matching(unittest.TestCase):
    def test_experience_range(self):
        self.assertEqual(candidate()["experience"], "0–2 years")

    def test_reject_senior(self):
        self.assertIsNone(candidate(title="Staff AI Engineer"))
        self.assertIsNone(candidate(descriptionPlain="4+ years building models"))

    def test_reject_unrelated(self):
        self.assertIsNone(candidate(title="AI Product Manager"))
        self.assertIsNone(candidate(title="Backend Engineer"))

    def test_remote(self):
        self.assertEqual(candidate(location="India", workplaceType="Remote")["mode"], "Remote")
        self.assertIsNone(candidate(location="Remote, US only"))
        self.assertIsNone(candidate(location="Remote"))

    def test_hybrid(self):
        self.assertEqual(candidate(location="Bangalore", workplaceType="Hybrid")["mode"], "Hybrid")

    def test_intern_conversion(self):
        self.assertIsNone(candidate(title="AI Intern"))
        self.assertIsNotNone(candidate(title="AI Intern", descriptionPlain="PPO available"))

    def test_unknowns(self):
        result = candidate(title="Graduate LLM Engineer", descriptionPlain="", publishedAt="bad")
        self.assertIn("Unknown", result["experience"])
        self.assertEqual(result["date"], "Unknown")

    def test_ranking(self):
        remote = candidate(location="Remote India")
        self.assertEqual(remote["score"], 94)
        self.assertGreater(remote["score"], candidate()["score"])
        self.assertGreater(candidate()["score"], candidate(publishedAt="2025-01-01T00:00:00Z")["score"])

    def test_malformed(self):
        for row in (None, [], {}, dict(RAW, applyUrl="javascript:alert(1)"), dict(RAW, isListed=False)):
            self.assertIsNone(normalize(BOARD, row))

    def test_greenhouse(self):
        raw = dict(title="AI Engineer", id=14, location={"name":"Bangalore"}, content="&lt;p&gt;1 year of experience&lt;/p&gt;",
                   updated_at="2026-11-19", absolute_url="https://example.org/jobs/14")
        result = match(normalize(("greenhouse", "x", "X"), raw))
        self.assertEqual(result["date"], "Unknown")
        self.assertEqual(result["experience"], "1 year")


class Delivery(unittest.TestCase):
    def test_plain_text(self):
        result = alerts.job_message(candidate(title="Junior AI Engineer <!channel>"))
        self.assertEqual(result["blocks"][0]["text"]["type"], "plain_text")
        self.assertEqual(result["blocks"][1]["elements"][0]["url"], RAW["applyUrl"])

    def test_failed_source(self):
        with patch("alerts.request", side_effect=RuntimeError("HTTP 503")):
            self.assertEqual(alerts.fetch_board(BOARD)[-1], "HTTP 503")

    def test_acknowledgement(self):
        with patch.dict("os.environ", SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T/B/test"), patch("alerts.request", return_value=b"error"):
            with self.assertRaises(RuntimeError):
                alerts.slack({"text": "unit test"})

    def test_interrupted_post(self):
        state = {}
        with self.assertRaises(RuntimeError):
            alerts.deliver([candidate()], state, lambda _: None, self.fail_delivery)
        self.assertEqual(list(state.values()), ["pending"])

    @staticmethod
    def fail_delivery(_):
        raise RuntimeError("unconfirmed")

    def test_two_runs_partial_outage(self):
        state = {}
        good = ("Example", 2, [candidate(), candidate()], None)
        bad = ("Other", 0, [], "HTTP 503")
        with patch("alerts.fetch_board", side_effect=[good, bad, good, bad]), patch.dict(alerts.SETTINGS, boards=[BOARD, BOARD]), \
             patch("alerts.webhook_url"), patch("alerts.slack") as post, patch("alerts.time.sleep"), \
             patch.object(Path, "read_text", side_effect=lambda: json.dumps(state)), patch("alerts.checkpoint", side_effect=lambda value: state.update(copy.deepcopy(value))), patch("alerts.report"):
            alerts.run(True)
            alerts.run(True)
        self.assertEqual(sum("blocks" in call.args[0] for call in post.call_args_list), 1)
        self.assertEqual(list(state.values()), ["sent"])

    def test_workflow(self):
        workflow = Path(".github/workflows/job-alerts.yml").read_text()
        for setting in ("30 3 * * *", "workflow_dispatch:", "cancel-in-progress: false", "timeout-minutes: 10"):
            self.assertIn(setting, workflow)
