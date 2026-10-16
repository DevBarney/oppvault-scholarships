"""OppVault Scholarships / Fellowships API.

Polls WordPress opportunity sites (Opportunity Desk, OFY, ScholarPositions,
Scholarships Corner), caches title/link/deadline/apply URL in SQLite, and
serves JSON for the OppVault frontend.

Always verify deadlines and eligibility on the official application page.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from app import poller

DB_PATH = os.environ.get("OPP_DB", "/tmp/opportunities.db")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@contextmanager
def connect():
    poller.init_db(DB_PATH)  # ensures schema
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def ensure_data(max_pages: int = 1) -> dict[str, Any]:
    with connect() as db:
        n = db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    if n > 0:
        return {"cached": n, "synced": False}
    with connect() as db:
        poller.poll(db, max_pages=max_pages)
        n = db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    return {"cached": n, "synced": True}


app = FastAPI(
    title="OppVault Scholarships",
    description="Aggregated scholarships/fellowships from public WordPress feeds. Verify on official sites.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/")
def home():
    page = FRONTEND / "index.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse("<h1>OppVault Scholarships API</h1><p>See /docs · /health · /opportunities</p>")


@app.get("/health")
def health():
    with connect() as db:
        n = db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    return {"ok": True, "count": n, "db": DB_PATH}


@app.post("/sync")
def sync(max_pages: int = Query(1, ge=1, le=5)):
    with connect() as db:
        poller.poll(db, max_pages=max_pages)
        n = db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    return {"ok": True, "count": n, "max_pages": max_pages}


@app.get("/opportunities")
def list_opportunities(
    upcoming: bool = Query(False, description="Only items with deadline >= today"),
    site: str | None = Query(None),
    q: str | None = Query(None, description="Search title/excerpt"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    ensure_data(max_pages=1)
    clauses: list[str] = []
    args: list[Any] = []
    if upcoming:
        clauses.append("deadline IS NOT NULL AND deadline >= ?")
        args.append(date.today().isoformat())
    if site:
        clauses.append("site = ?")
        args.append(site)
    if q:
        clauses.append("(title LIKE ? OR excerpt LIKE ?)")
        args.extend([f"%{q}%", f"%{q}%"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order = "ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline ASC, published DESC"
    sql = f"SELECT * FROM opportunities{where} {order} LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    with connect() as db:
        rows = db.execute(sql, args).fetchall()
        total = db.execute(f"SELECT COUNT(*) FROM opportunities{where}", args[:-2]).fetchone()[0]
    items = []
    for r in rows:
        items.append({
            "title": r["title"],
            "site": r["site"],
            "source_link": r["link"],
            "apply_url": r["apply_url"],
            "apply_confidence": r["apply_conf"],
            "deadline": r["deadline"],
            "published": r["published"],
            "excerpt": r["excerpt"],
            "primary_url": r["apply_url"] or r["link"],
        })
    return {"total": total, "count": len(items), "items": items}


@app.get("/upcoming")
def upcoming(limit: int = Query(50, ge=1, le=100)):
    return list_opportunities(upcoming=True, limit=limit)
