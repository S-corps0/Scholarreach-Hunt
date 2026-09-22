"""
Mongo helpers for the hunt fleet.

- huntedjournals : catalog + per-journal crawl cursor / claim
- papertopics    : sold/unsold email+topic pool

Claim rules:
  One journal is owned by at most one worker per GitHub run_id.
  A later run may claim the same journal and continue pagination.
"""
import os
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING, ReturnDocument

_client = None

APP_DB = os.getenv("MONGODB_DB") or os.getenv("MONGO_DB") or "test"
JOURNALS_COL = "huntedjournals"
TOPICS_COL = "papertopics"

# Stop OpenAlex discovery once we have this many ready journals; harvest only.
DISCOVER_CAP = int(os.getenv("HUNT_DISCOVER_CAP", "200"))
# Resume discovery only when ready-and-not-dry falls below this
DISCOVER_RESUME_BELOW = int(os.getenv("HUNT_DISCOVER_RESUME_BELOW", "150"))


def _client_or_connect():
    global _client
    if _client is None:
        uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI") or (
            "mongodb://localhost:27017"
        )
        _client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        _client.admin.command("ping")
    return _client


def db():
    return _client_or_connect()[APP_DB]


def ensure_hunt_indexes():
    j = db()[JOURNALS_COL]
    j.create_index([("status", ASCENDING), ("updatedAt", ASCENDING)])
    j.create_index([("status", ASCENDING), ("dry", ASCENDING), ("claimedByRun", ASCENDING)])
    j.create_index([("key", ASCENDING)], unique=True)
    t = db()[TOPICS_COL]
    t.create_index([("journalKey", ASCENDING), ("extractedAt", ASCENDING)])
    try:
        t.create_index("pdfUrl", unique=True, sparse=True)
    except Exception:
        pass


def ready_count() -> int:
    return db()[JOURNALS_COL].count_documents({"status": "ready"})


def active_harvest_count() -> int:
    """Ready journals not yet marked dry (still have crawl work)."""
    return db()[JOURNALS_COL].count_documents(
        {"status": "ready", "dry": {"$ne": True}}
    )


def should_discover() -> bool:
    """
    Discover new journals only while catalog is thin.
    Once we hold ~150–200 ready journals, focus on harvesting emails.
    When all are dry (or active harvest count is low), discover again.
    """
    ready = ready_count()
    active = active_harvest_count()
    if ready < DISCOVER_RESUME_BELOW:
        return True
    if ready >= DISCOVER_CAP and active > 0:
        return False
    if ready >= DISCOVER_RESUME_BELOW and active > 0:
        return False
    # All scraped dry (or almost) — open discovery again
    if active == 0:
        return True
    return ready < DISCOVER_CAP


def upsert_journal(doc: dict):
    now = datetime.now(timezone.utc)
    key = doc["key"]
    set_doc = {**doc, "updatedAt": now}
    set_doc.pop("key", None)
    db()[JOURNALS_COL].update_one(
        {"key": key},
        {
            "$set": set_doc,
            "$setOnInsert": {
                "createdAt": now,
                "dry": False,
                "seenPdfUrls": [],
                "crawl": {},
                "claimedByRun": None,
                "claimedByWorker": None,
            },
        },
        upsert=True,
    )


def claim_journal_for_run(run_id: str, worker_id: str):
    """
    Atomically claim one non-dry ready journal for this workflow run.
    Journals claimed by another worker in the SAME run are skipped.
    Journals claimed only by a PREVIOUS run are free to continue.
    """
    now = datetime.now(timezone.utc)
    # Prefer journals already partially crawled (have crawl state / seen urls)
    filt = {
        "status": "ready",
        "dry": {"$ne": True},
        "$or": [
            {"claimedByRun": None},
            {"claimedByRun": {"$exists": False}},
            {"claimedByRun": {"$ne": run_id}},
        ],
    }
    doc = db()[JOURNALS_COL].find_one_and_update(
        filt,
        {
            "$set": {
                "claimedByRun": run_id,
                "claimedByWorker": worker_id,
                "claimedAt": now,
                "updatedAt": now,
            }
        },
        sort=[("emailCount", ASCENDING), ("updatedAt", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )
    return doc


def release_journal_claim(key: str, run_id: str):
    db()[JOURNALS_COL].update_one(
        {"key": key, "claimedByRun": run_id},
        {
            "$set": {
                "claimedByRun": None,
                "claimedByWorker": None,
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )


def mark_journal_dry(key: str):
    db()[JOURNALS_COL].update_one(
        {"key": key},
        {
            "$set": {
                "dry": True,
                "status": "ready",
                "claimedByRun": None,
                "claimedByWorker": None,
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )


def save_crawl_progress(key: str, crawl: dict, seen_pdf_urls: list, email_count_delta: int = 0):
    """Persist pagination cursor + seen PDF set (capped)."""
    now = datetime.now(timezone.utc)
    # Cap seen list size in memory store
    seen = list(dict.fromkeys(seen_pdf_urls or []))[-8000:]
    upd = {
        "$set": {
            "crawl": crawl or {},
            "seenPdfUrls": seen,
            "updatedAt": now,
        }
    }
    if email_count_delta:
        upd["$inc"] = {
            "emailCount": max(0, email_count_delta),
            "topicCount": max(0, email_count_delta),
        }
    db()[JOURNALS_COL].update_one({"key": key}, upd)


def bump_pdf_queued(key: str, n: int):
    if n:
        db()[JOURNALS_COL].update_one(
            {"key": key},
            {"$inc": {"pdfQueued": n}, "$set": {"updatedAt": datetime.now(timezone.utc)}},
        )


def save_topic(
    journal_key: str,
    title: str,
    topic: str,
    pdf_url: str,
    doi: str = "",
    source_page: str = "",
    author_name=None,
    emails=None,
):
    emails = [str(e).strip().lower() for e in (emails or []) if e and "@" in str(e)]
    emails = list(dict.fromkeys(emails))
    if not emails:
        return False
    title = (title or topic or "Untitled paper")[:300]
    topic = (topic or title)[:300]
    now = datetime.now(timezone.utc)
    try:
        res = db()[TOPICS_COL].update_one(
            {"pdfUrl": pdf_url},
            {
                "$set": {
                    "journalKey": journal_key,
                    "title": title,
                    "topic": topic,
                    "email": emails[0],
                    "emails": emails,
                    "doi": (doi or "")[:200],
                    "sourcePage": (source_page or "")[:500],
                    "authorName": author_name,
                    "extractedAt": now,
                },
                "$setOnInsert": {"soldAt": None},
            },
            upsert=True,
        )
        return bool(res.upserted_id or res.modified_count)
    except Exception:
        return False


def hunt_stats() -> dict:
    j = db()[JOURNALS_COL]
    t = db()[TOPICS_COL]
    return {
        "ready": j.count_documents({"status": "ready"}),
        "dry": j.count_documents({"status": "ready", "dry": True}),
        "active": active_harvest_count(),
        "rejected": j.count_documents({"status": "rejected"}),
        "topics": t.estimated_document_count(),
        "unsold": t.count_documents({"soldAt": None}),
        "shouldDiscover": should_discover(),
    }
