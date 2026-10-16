#!/usr/bin/env python3
"""Poll opportunity sites via WP REST (fallback RSS) into SQLite."""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse

SITES = {
    "opportunitydesk": "https://opportunitydesk.org",
    "opportunitiesforyouth": "https://opportunitiesforyouth.org",
    "scholarpositions": "https://scholarpositions.com",
    "scholarshipscorner": "https://scholarshipscorner.website",
}
DB_PATH = os.environ.get("OPP_DB", "/tmp/opportunities.db")
UA = "OppPoller/1.0 (OppVault; contact: barntech7427@gmail.com)"
DELAY = 1.5
FIELDS = "id,link,title,date,modified,excerpt,content"

MONTH = r"[A-Za-z]{3,9}\.?"
DATE_PAT = (
    rf"(?:{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH},?\s+\d{{4}}"
    r"|\d{{4}}-\d{{2}}-\d{{2}})"
)
DEADLINE_RE = re.compile(
    r"(?:deadline|closing date|closes? on|apply by|apply before|last date(?: to apply)?"
    r"|applications? (?:close|are due|due)(?: on)?)\s*[:\-–—]?\s*(?:is\s+|on\s+)?"
    rf"({DATE_PAT})",
    re.I,
)
BAD_HOSTS = (
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "t.me", "whatsapp.com", "youtube.com", "opd.to", "wordpress.org", "gravatar.com", "wp.com",
)
STRONG = re.compile(
    r"apply (now|here|online|today|for this)|application (link|portal|page|form)"
    r"|official (website|link|page|site|call)|click here to apply|apply at", re.I)
WEAK = re.compile(r"\bapply\b|application|official|register|submit", re.I)
PATH_HINT = re.compile(r"apply|application|scholarship|fellowship|call", re.I)


def clean_text(html):
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def extract_deadline(text):
    for m in DEADLINE_RE.finditer(text or ""):
        try:
            return dtparse.parse(m.group(1), fuzzy=True).date().isoformat()
        except (ValueError, OverflowError):
            continue
    return None


def extract_apply_url(html, own_host):
    soup = BeautifulSoup(html or "", "html.parser")
    best, best_score = None, 0
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(("http://", "https://")):
            continue
        host = urlparse(href).netloc.lower().removeprefix("www.")
        if host == own_host or any(b in host for b in BAD_HOSTS):
            continue
        text = a.get_text(" ", strip=True)
        score = 0
        if STRONG.search(text):
            score = 3
        elif WEAK.search(text):
            score = 2
        elif PATH_HINT.search(urlparse(href).path):
            score = 1
        if score > best_score:
            best, best_score = href, score
    if best_score >= 2:
        return best, "high"
    if best_score == 1:
        return best, "low"
    return None, None


def get(session, url, params=None):
    for _ in range(3):
        r = session.get(url, params=params, timeout=25)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 20)))
            continue
        return r
    return r


def fetch_rest(session, base, since, max_pages):
    params = {"per_page": 50, "_fields": FIELDS, "orderby": "modified", "order": "desc"}
    if since:
        params["modified_after"] = since
    for page in range(1, max_pages + 1):
        params["page"] = page
        r = get(session, f"{base}/wp-json/wp/v2/posts", params)
        if r.status_code == 400 and page > 1:
            return
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            raise RuntimeError(f"REST API unavailable (HTTP {r.status_code})")
        posts = r.json()
        for p in posts:
            yield {
                "link": p["link"],
                "title": clean_text(p["title"]["rendered"]),
                "published": p["date"],
                "modified": p.get("modified"),
                "excerpt": clean_text(p["excerpt"]["rendered"]),
                "html": p["content"]["rendered"],
            }
        total_pages = int(r.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            return
        time.sleep(DELAY)


def fetch_feed(session, base):
    r = get(session, f"{base}/feed/")
    if r.status_code != 200:
        raise RuntimeError(f"feed unavailable (HTTP {r.status_code})")
    for e in feedparser.parse(r.content).entries:
        html = e.content[0].value if e.get("content") else e.get("summary", "")
        yield {
            "link": e.link,
            "title": clean_text(e.title),
            "published": e.get("published", ""),
            "modified": None,
            "excerpt": clean_text(e.get("summary", ""))[:500],
            "html": html,
        }


def init_db(path=None):
    path = path or DB_PATH
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE IF NOT EXISTS opportunities (
            link TEXT PRIMARY KEY, site TEXT NOT NULL, title TEXT,
            published TEXT, modified TEXT, excerpt TEXT,
            deadline TEXT, apply_url TEXT, apply_conf TEXT, first_seen TEXT)"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_deadline ON opportunities(deadline)")
    return db


def upsert(db, site, base, post):
    host = urlparse(base).netloc.lower().removeprefix("www.")
    text = clean_text(post["html"])
    deadline = extract_deadline(text)
    apply_url, conf = extract_apply_url(post["html"], host)
    db.execute(
        """INSERT INTO opportunities
           (link, site, title, published, modified, excerpt, deadline, apply_url, apply_conf, first_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(link) DO UPDATE SET
             title=excluded.title, modified=excluded.modified, excerpt=excluded.excerpt,
             deadline=excluded.deadline, apply_url=excluded.apply_url, apply_conf=excluded.apply_conf""",
        (post["link"], site, post["title"], post["published"], post["modified"],
         (post["excerpt"] or "")[:600], deadline, apply_url, conf,
         datetime.now(timezone.utc).isoformat()),
    )


def poll(db, max_pages=1):
    session = requests.Session()
    session.headers["User-Agent"] = UA
    for site, base in SITES.items():
        row = db.execute("SELECT MAX(modified) FROM opportunities WHERE site=?", (site,)).fetchone()
        since = row[0]
        try:
            posts = list(fetch_rest(session, base, since, max_pages))
            mode = "rest"
        except Exception as exc:
            print(f"[{site}] {exc}; falling back to feed", file=sys.stderr)
            try:
                posts = list(fetch_feed(session, base))
                mode = "feed"
            except Exception as exc2:
                print(f"[{site}] skipped: {exc2}", file=sys.stderr)
                continue
        for p in posts:
            upsert(db, site, base, p)
        db.commit()
        print(f"[{site}] {len(posts)} posts via {mode}")
        time.sleep(DELAY)


def show_upcoming(db):
    today = date.today().isoformat()
    rows = db.execute(
        """SELECT deadline, site, title, COALESCE(apply_url, link), apply_conf
           FROM opportunities WHERE deadline >= ? ORDER BY deadline LIMIT 50""",
        (today,),
    )
    for deadline, site, title, url, conf in rows:
        tag = "" if conf == "high" else " (check)"
        print(f"{deadline}  [{site}] {title}\n    {url}{tag}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--upcoming", action="store_true")
    args = ap.parse_args()
    conn = init_db(args.db)
    if args.upcoming:
        show_upcoming(conn)
    else:
        poll(conn, args.max_pages)
