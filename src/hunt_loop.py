"""
Hunt fleet — PKP Beacon OJS first.

OpenAlex discovery is OFF. Workers:
  - Validate pending pkp_beacon journals → ready | rejected
  - Harvest ready journals (paginate issues → PDF → email+topic) until dry

Killswitch: HUNT_ENABLED (default true if unset in Actions via workflow env).
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scholarreach.hunt")

OPENALEX_HUNT_ENABLED = False  # hard off


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Scholarreach-Hunt/3.0 (PKP-OJS topic+email)"})
    return s


def claim_pending_pkp(worker_id: str):
    from src import hunt_db as hdb

    now = datetime.now(timezone.utc)
    return hdb.db()[hdb.JOURNALS_COL].find_one_and_update(
        {
            "source": "pkp_beacon",
            "status": "pending",
            "$or": [
                {"claimedBy": {"$exists": False}},
                {"claimedBy": None},
                {"claimExpires": {"$lt": now}},
            ],
        },
        {
            "$set": {
                "status": "sampling",
                "claimedBy": worker_id,
                "claimExpires": now + timedelta(minutes=30),
                "updatedAt": now,
            }
        },
        sort=[("totalRecordCount", -1)],
        return_document=True,
    )


def validate_pkp_journal(worker_id: str, job: dict, sess: requests.Session) -> str:
    """Probe homepage / OAI / sample PDFs. Returns ready|rejected."""
    from src import validate as v
    from src import hunt_db as hdb

    key = job.get("key")
    home = (job.get("homepageUrl") or "").strip()
    oai = (job.get("oaiUrl") or "").strip()
    name = (job.get("displayName") or key or "")[:80]
    alive = 0
    checked = 0
    pdf_hits = 0

    if home:
        checked += 1
        try:
            r = sess.get(home, timeout=25, allow_redirects=True)
            if r.status_code < 400 and len(r.text or "") > 200:
                alive += 1
        except Exception:
            pass

    if oai:
        checked += 1
        try:
            probe = oai if "verb=" in oai.lower() else (oai.rstrip("/") + "?verb=Identify")
            r = sess.get(probe, timeout=25)
            body = r.text or ""
            if r.status_code < 400 and ("OAI-PMH" in body or "Identify" in body or "repositoryName" in body):
                alive += 1
        except Exception:
            pass

    if home:
        try:
            from src.scraper import discover_from_seeds

            papers = discover_from_seeds(listing_urls=[home], pdf_urls=[], sample_paper_urls=[]) or []
            for p in papers[:12]:
                checked += 1
                u = p.get("pdf_url") or ""
                if not u:
                    continue
                try:
                    pr = v.probe_pdf(u, session=sess)
                except Exception:
                    pr = {"alive": False}
                if pr.get("alive"):
                    alive += 1
                    pdf_hits += 1
                time.sleep(0.25)
        except Exception as e:
            logger.debug("%s discover %s: %s", worker_id, key, e)

    # Crawlable if homepage/OAI ok or at least 2 live PDFs
    if alive >= 1 or pdf_hits >= 2:
        status = "ready"
        reason = None
    else:
        status = "rejected"
        reason = f"pkp_sample alive={alive} checked={checked} pdfs={pdf_hits}"

    hdb.db()[hdb.JOURNALS_COL].update_one(
        {"key": key},
        {
            "$set": {
                "status": status,
                "dry": False,
                "sampleChecked": checked,
                "sampleAlive": alive,
                "samplePdfHits": pdf_hits,
                "rejectReason": reason,
                "claimedBy": None,
                "claimExpires": None,
                "lastCheckedAt": datetime.now(timezone.utc),
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )
    logger.info("%s %s → %s (%s)", worker_id, name, status, reason or "ok")
    return status


def harvest_one(worker_id: str, run_id: str, sess: requests.Session, budget: float) -> int:
    """Claim one ready journal and extract emails+topics until budget/dry."""
    from src import hunt_db as hdb
    from src.topic_extract import process_pdf_topic

    journal = hdb.claim_journal_for_run(run_id, worker_id)
    if not journal:
        return 0

    key = journal["key"]
    homepage = (journal.get("homepageUrl") or "").strip()
    if not homepage:
        hdb.mark_journal_dry(key)
        return 0

    start = time.time()
    added = 0
    seen = list(journal.get("seenPdfUrls") or [])
    seen_set = set(seen)

    try:
        from src.scraper import discover_from_seeds

        papers = discover_from_seeds(
            listing_urls=[homepage], pdf_urls=[], sample_paper_urls=[]
        ) or []
    except Exception as e:
        logger.warning("%s discover failed %s: %s", worker_id, key, e)
        papers = []

    new_pdfs = []
    for p in papers:
        u = p.get("pdf_url") or ""
        if u and u not in seen_set:
            new_pdfs.append(p)
            seen_set.add(u)

    if not new_pdfs and not papers:
        # nothing found — mark dry so we move on
        hdb.mark_journal_dry(key)
        logger.info("%s %s dry (no pdfs)", worker_id, key)
        return 0

    hdb.bump_pdf_queued(key, len(new_pdfs))

    # Prefer newest papers first (year in URL/path), then rest
    def _year_key(item):
        import re as _re
        u = (item.get("pdf_url") or "") + " " + (item.get("title") or "")
        ys = [int(x) for x in _re.findall(r"(20[0-2]\d|19[89]\d)", u)]
        return max(ys) if ys else 0
    new_pdfs = sorted(new_pdfs, key=_year_key, reverse=True)

    for p in new_pdfs:  # newest-first
        if budget > 0 and (time.time() - start) >= budget:
            break
        pdf_url = p.get("pdf_url")
        try:
            out = process_pdf_topic(pdf_url)
            if not out:
                continue
            emails = out.get("emails") or []
            if not emails:
                continue
            ok = hdb.save_topic(
                journal_key=key,
                title=out.get("title") or p.get("title") or "",
                topic=out.get("topic") or out.get("title") or "untitled",
                pdf_url=pdf_url,
                doi=p.get("doi") or out.get("doi") or "",
                source_page=homepage,
                author_name=out.get("authorName"),
                emails=emails,
            )
            if ok:
                added += 1
                seen.append(pdf_url)
        except Exception as e:
            logger.debug("%s pdf skip %s: %s", worker_id, (pdf_url or "")[-40:], e)
        time.sleep(0.4)

    # persist seen; if we processed the batch and found nothing more, dry
    crawl = dict(journal.get("crawl") or {})
    crawl["seedDone"] = True
    hdb.save_crawl_progress(key, crawl, seen, email_count_delta=added)

    # If discover returned a small finite set and we walked it, mark dry
    if added == 0 and len(new_pdfs) < 3:
        hdb.mark_journal_dry(key)
        logger.info("%s %s marked dry (exhausted small set)", worker_id, key)
    elif added:
        logger.info("%s %s +%d emails", worker_id, key, added)

    return added


def run_loop(max_runtime_seconds: int = 5 * 3600 + 1800, idle_sleep: int = 5):
    from src.config import HUNT_ENABLED

    # Allow empty secret: treat missing as enabled when Actions sets HUNT_ENABLED=true
    enabled = HUNT_ENABLED
    if (os.getenv("HUNT_ENABLED") or "").strip() == "":
        enabled = True  # workflow should pass true; empty secret used to kill the fleet

    if not enabled:
        logger.warning("HUNT_ENABLED is FALSE — hunt idle")
        return 0

    from src import hunt_db as hdb

    try:
        hdb.ensure_hunt_indexes()
    except Exception as e:
        logger.warning("indexes: %s", e)

    try:
        hdb.db()["systemsettings"].update_one(
            {"key": "hunt"},
            {
                "$set": {
                    "enabled": True,
                    "openalexEnabled": False,
                    "primarySource": "pkp_beacon",
                    "preferSourceOrder": ["pkp_beacon"],
                    "updatedAt": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
    except Exception:
        pass

    num = int(os.getenv("HUNT_WORKER_NUM") or "1")
    run_id = os.getenv("GITHUB_RUN_ID") or "local"
    worker_id = f"hunt-{run_id}-{num}-{uuid.uuid4().hex[:6]}"
    logger.info(
        "Hunt worker %s start (PKP-OJS, openalex=OFF) max=%ss",
        worker_id,
        max_runtime_seconds,
    )

    sess = _session()
    start = time.time()
    validated = 0
    harvested = 0

    while time.time() - start < max_runtime_seconds:
        remaining = max_runtime_seconds - (time.time() - start)
        if remaining < 30:
            break

        # Discover only until ready pool hits ~200–250; then all workers harvest to dry
        try:
            do_validate = hdb.should_discover()
            ready = hdb.ready_count()
        except Exception:
            do_validate, ready = True, 0
        if do_validate:
            logger.info("%s discover mode (ready=%s)", worker_id, ready)
        else:
            logger.info("%s harvest-only mode (ready=%s >= cap) — no new journal discovery", worker_id, ready)

        if do_validate:
            job = claim_pending_pkp(worker_id)
            if job:
                try:
                    st = validate_pkp_journal(worker_id, job, sess)
                    if st == "ready":
                        validated += 1
                except Exception as e:
                    logger.exception("%s validate fail: %s", worker_id, e)
                    try:
                        hdb.db()[hdb.JOURNALS_COL].update_one(
                            {"key": job.get("key")},
                            {"$set": {"status": "pending", "claimedBy": None, "claimExpires": None}},
                        )
                    except Exception:
                        pass
                continue

        # Harvest
        try:
            n = harvest_one(worker_id, run_id, sess, min(remaining, 1200))
            if n:
                harvested += n
            else:
                time.sleep(idle_sleep)
        except Exception as e:
            logger.exception("%s harvest fail: %s", worker_id, e)
            time.sleep(idle_sleep)

    logger.info(
        "Hunt worker %s done validated_ready=%d emails=%d",
        worker_id,
        validated,
        harvested,
    )
    try:
        logger.info("Hunt stats: %s", hdb.hunt_stats())
    except Exception:
        pass
    return validated + harvested
