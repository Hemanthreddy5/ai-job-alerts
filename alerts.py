"""Run from the repository root. Preview is the default."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jobs import SETTINGS, match, normalize

LEDGER = Path("delivery.json")


def request(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=data, headers={"User-Agent": "AI-Job-Alert/1.0",
                                           "Content-Type": "application/json"})
    for attempt in range(3):
        delay = 2 ** attempt
        try:
            with urlopen(req, timeout=15) as response:
                body = response.read(40_000_001)
            if len(body) > 40_000_000:
                raise ValueError("Response exceeds size limit")
            return body
        except HTTPError as exc:
            retry = exc.code == 429 or (data is None and 500 <= exc.code <= 599)
            wait = exc.headers.get("Retry-After", "")
            if wait.isdigit():
                delay = max(delay, int(wait))
            if not retry or delay > 30 or attempt == 2:
                raise RuntimeError(f"HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            if data is not None or attempt == 2:
                raise RuntimeError("Network failure") from None
        time.sleep(delay)


def fetch_board(board):
    provider, slug, company = board
    base = {"greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}"}
    try:
        rows = json.loads(request(base[provider]))["jobs"]
        if not isinstance(rows, list):
            raise ValueError("invalid list")
        candidates = []
        for raw in rows:
            job = normalize(board, raw)
            if job and (candidate := match(job)):
                candidates.append(candidate)
        return company, len(rows), candidates, None
    except (RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
        return company, 0, [], str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__


def webhook_url():
    hook = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not re.fullmatch(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+", hook):
        raise RuntimeError("Missing or invalid SLACK_WEBHOOK_URL")
    return hook


def slack(payload):
    if request(webhook_url(), payload).strip() != b"ok":
        raise RuntimeError("Slack did not acknowledge the message")


def job_message(job):
    text = (f"🔎 {job['title'][:230]} · {job['company']}\n"
            f"{job['location'][:200]} · {job['mode']}\n"
            f"Experience: {job['experience'][:200]}\n"
            f"Skills mentioned: {', '.join(job['skills']) or 'Unknown'}\n"
            f"Posted: {job['date']} · Match: {job['score']}/100 · Source: {job['source']}")
    return {"text": "New AI engineering job", "blocks": [
        {"type": "section", "text": {"type": "plain_text", "text": text}},
        {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Apply on employer site"}, "url": job["url"]}]}]}


def checkpoint(state):
    LEDGER.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    for args in (["add", "--", str(LEDGER)], ["commit", "-m", "Update job delivery ledger"],
                 ["push", "origin", "HEAD"]):
        subprocess.run(["git", *args], check=True)


def deliver(jobs, state, save, post):
    count = 0
    for job in jobs:
        key = job["key"]
        print(json.dumps({"event": "reserve", "id": key, "title": job["title"][:230]}), flush=True)
        state[key] = "pending"
        save(state)
        post(job_message(job))
        state[key] = "sent"
        save(state)
        count += 1
        print(json.dumps({"event": "confirmed", "id": key, "delivered_this_run": count}), flush=True)
        time.sleep(1)
    return count


def report(message):
    print(json.dumps({"summary": message}), flush=True)
    if summary_path := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as output:
            print(message + "\n", file=output)


def run(live=False, list_matches=False):
    if live:
        webhook_url()
    state = json.loads(LEDGER.read_text())
    if not isinstance(state, dict) or any(v not in ("sent", "pending") for v in state.values()):
        raise ValueError("Invalid ledger")
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(fetch_board, SETTINGS["boards"]))
    matches = []
    total = 0
    errors = []
    for company, found, candidates, error in results:
        total += found
        matches.extend(candidates)
        report(f"{company} — {error or str(found) + ' postings'}")
        if error:
            errors.append(company)
    unique = {job["key"]: job for job in matches if job["key"] not in state}
    if list_matches:
        for job in matches:
            print(json.dumps({"id": job["key"], "title": job["title"], "url": job["url"]}))
    jobs = sorted(unique.values(), key=lambda job: (-job["score"], job["key"]))
    pending = sum(status == "pending" for status in state.values())
    status = (f"Discovered {total} · Matches {len(matches)} · New {len(jobs)} · "
              f"Duplicates or reserved {len(matches) - len(jobs)} · Pending review {pending}")
    report(status)
    if len(errors) == len(results):
        raise RuntimeError("All sources failed")
    delivered = deliver(jobs, state, checkpoint, slack) if live else 0
    summary = f"{status} · Delivered {delivered}. Unavailable sources: {', '.join(errors) or 'none'}."
    if live:
        slack({"text": summary})
    report(summary)
    if pending:
        raise RuntimeError("Review pending deliveries in delivery.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--list-matches", action="store_true")
    args = parser.parse_args()
    try:
        run(args.send, args.list_matches)
    except Exception as error:
        report(f"Run failed — {type(error).__name__}: {error}")
        if args.send:
            try:
                slack({"text": "⚠ AI job alert run failed. Check the GitHub Actions summary and delivery ledger."})
            except RuntimeError:
                report("Slack failure notice could not be confirmed.")
        raise SystemExit(1)
