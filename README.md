# OppVault Scholarships API

Aggregates scholarship / fellowship listings from:

- [opportunitydesk.org](https://opportunitydesk.org)
- [opportunitiesforyouth.org](https://opportunitiesforyouth.org)
- [scholarpositions.com](https://scholarpositions.com)
- [scholarshipscorner.website](https://scholarshipscorner.website)

via public WordPress REST + RSS. Stores title, source link, excerpt, parsed deadline, and a guessed official apply URL.

## Endpoints

| Method | Path | Notes |
|--------|------|--------|
| GET | `/health` | Count + status |
| GET | `/opportunities` | `upcoming`, `site`, `q`, `limit`, `offset` |
| GET | `/upcoming` | Shortcut for open deadlines |
| POST | `/sync?max_pages=1` | Re-poll sources (keep pages low on Vercel) |
| GET | `/` | Simple UI |

## Local

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Or the CLI poller:

```bash
python -m app.poller --max-pages 3
python -m app.poller --upcoming
```

## Deploy

Vercel (FastAPI). Ephemeral `/tmp` SQLite — first request or `POST /sync` fills the cache.

**Always verify deadlines and eligibility on the official application page.**
