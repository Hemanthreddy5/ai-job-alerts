"""Normalize public employer postings and rank early-career engineering roles."""
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SETTINGS = json.loads(Path(__file__).with_name("settings.json").read_text())
SENIOR = r"\b(?:senior|sr|lead|principal|staff|manager|director|head|architect|iii|iv)\b"
ENTRY = r"\b(?:junior|jr|graduate|entry.level|fresher|associate|trainee|new grad)\b"
YEARS = re.compile(r"\b(\d{1,2})(?:\s*(?:[-–—]|to)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b", re.I)


def string(value):
    return value.strip() if isinstance(value, str) else ""


def normalize(board, raw):
    provider, slug, company = board
    if not isinstance(raw, dict) or raw.get("isListed") is False:
        return
    title = string(raw.get("title"))
    url = string(raw.get("applyUrl") or raw.get("absolute_url"))
    if not title or not re.fullmatch(r"https://[a-z0-9.-]+/[^\s<>|\\]+", url, re.I):
        return
    if provider == "ashby":
        locations = [string(raw.get("location"))]
        address = raw.get("address") or {}
        if isinstance(address, dict) and isinstance(address.get("postalAddress"), dict):
            locations.append(string(address["postalAddress"].get("addressCountry")))
        other_locations = raw.get("secondaryLocations")
        for item in other_locations if isinstance(other_locations, list) else []:
            if isinstance(item, dict):
                locations.append(string(item.get("location")))
        location = ", ".join(dict.fromkeys(x for x in locations if x))
        mode = string(raw.get("workplaceType")) or ("Remote" if raw.get("isRemote") else "")
        description = string(raw.get("descriptionPlain"))
        posted = string(raw.get("publishedAt"))
    else:
        loc = raw.get("location")
        location = string(loc.get("name")) if isinstance(loc, dict) else ""
        description = re.sub(r"<[^>]+>", "\n", html.unescape(string(raw.get("content"))))
        mode = ""
        posted = string(raw.get("first_published"))
    job_id = raw.get("id") or url
    key = hashlib.sha256(f"{provider}/{slug}/{job_id}".encode()).hexdigest()
    return dict(key=key, title=title, url=url, company=company, source=provider,
                location=location, description=description, mode=mode, posted=posted)


def match(job, now=None):
    title = job["title"].lower()
    body = job["description"].lower()
    topics = "|".join(re.escape(word.lower()) for word in SETTINGS["topics"])
    if re.search(SENIOR, title) or not re.search(r"\b(?:" + topics + r")\b", title):
        return
    internship = bool(re.search(r"\bintern(?:ship)?\b", title))
    if internship and not re.search(r"\b(?:ppo|pre.placement offer|convert to full.time|conversion to full.time|intern.to.hire)\b", body):
        return
    if not internship and not re.search(r"\b(?:engineer(?:ing)?|developer)\b", title):
        return
    location = job["location"].lower()
    city = bool(re.search(r"\b(?:bengaluru|bangalore)\b", location))
    mode = job["mode"] or next((name for name, pattern in [
        ("Hybrid", r"\bhybrid\b"), ("OnSite", r"\bon.?site\b"), ("Remote", r"\bremote\b|\bwfh\b")
    ] if re.search(pattern, location)), "Unknown")
    remote = mode.lower() == "remote"
    eligible = city or re.search(r"\b(?:india|worldwide|anywhere in the world)\b", location)
    if not (city or remote and eligible):
        return
    evidence = []
    for line in re.split(r"[\n;.!?]", title + "\n" + body):
        if re.search(r"\b(preferred|bonus|nice.to.have|founded|company history)\b", line):
            continue
        if not re.search(r"experience|years?\s+(?:of|in|with|building|developing|working)|yrs?\b|required|minimum|at least|^\s*[-•]?\s*\d", line) and line != title:
            continue
        evidence.extend(YEARS.finditer(line))
    if any(int(m[1]) > SETTINGS["max_required_years"] for m in evidence):
        return
    entry = internship or re.search(ENTRY, title) or re.search(r"freshers? (?:welcome|can apply)|new graduates? welcome|no (?:prior )?experience required", body)
    if not entry and not evidence:
        return
    experience = ", ".join(dict.fromkeys(m[0] for m in evidence)) or "Unknown (early-career wording)"
    skills = [s for s in SETTINGS["skills"] if re.search(r"\b" + re.escape(s.lower()) + r"\b", body)]
    fresh = 0
    date = "Unknown"
    try:
        published = datetime.fromisoformat(job["posted"].replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - published).total_seconds() / 3600
        if age >= 0:
            date = published.date().isoformat()
            fresh = 10 if age <= 72 else 5 if age <= 168 else 0
    except (ValueError, TypeError):
        pass
    score = 30 + (25 if remote else 20) + (20 if evidence else 14) + min(10, 2 * len(skills)) + fresh + 5
    return dict(job, experience=experience, skills=skills, mode=mode, date=date, score=score)
