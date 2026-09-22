"""
Auto-hunt fleet with exclusive per-run journal claims.

Phases:
  DISCOVER — OpenAlex find + validate journals while ready < ~150–200
  HARVEST  — one worker owns one journal for the whole run; paginate until dry;
             next run may continue the same journal.

No two workers in the same GitHub run process the same journal.
"""
import logging
import os
import time
import uuid
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scholarreach.hunt")


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Scholarreach-Hunt/2.1 (claim+paginate topic+email)"})
    return s


def discover_one(worker_id: str, src: dict, sess: requests.Session) -> bool:
    """Validate OpenAlex source and insert as ready if crawlable. Returns True if added."""
    from src import openalex as oa
    from src import validate as v
    from src import hunt_db as hdb

    name = src.get("display_name") or "Untitled journal"
    homepage = (src.get("homepage_url") or "").strip()
    openalex_id = src.get("id") or ""
    key = "openalex:" + (
        openalex_id.split("/")[-1] if "/" in openalex_id else openalex_id or name[:40]
    )

    existing = hdb.db()[hdb.JOURNALS_COL].find_one({"key": key})
    if existing:
        return False

    try:
        works = oa.works_with_pdfs(openalex_id, per_page=10)
    except Exception as e:
        logger.debug("%s openalex works failed %s: %s", worker_id, name[:40], e)
        return False
    if len(works) < 5:
        return False

    alive = 0
    with_title = 0
    for w in works[:10]:
        try:
            p = v.probe_pdf(w["pdf_url"], session=sess)
        except Exception:
            p = {"alive": False}
        if p.get("alive"):
            alive += 1
            if (w.get("title") or "").strip():
                with_title += 1
        time.sleep(0.3)

    if alive < 7 or with_title < 5:
        hdb.upsert_journal(
            {
                "key": key,
                "displayName": str(name)[:160],
                "homepageUrl": str(homepage)[:500],
                "openalexId": str(openalex_id)[:120],
                "issn": src.get("issn") or [],
                "status": "rejected",
                "sampleChecked": min(10, len(works)),
                "alivePdfs": alive,
            }
        )
        return False

    hdb.upsert_journal(
        {
            "key": key,
            "displayName": str(name)[:160],
            "homepageUrl": str(homepage)[:500],
            "openalexId": str(openalex_id)[:120],
            "issn": src.get("issn") or [],
            "status": "ready",
            "dry": False,
            "emailCount": 0,
            "topicCount": 0,
            "pdfQueued": 0,
            "sampleChecked": min(10, len(works)),
            "alivePdfs": alive,
        }
    )
    logger.info("%s discovered ready journal %s", worker_id, name[:50])
    return True


def harvest_journal(worker_id: str, journal: dict, sess: requests.Session, max_seconds: float = 0) -> int:
    """
    Continue pagination on one claimed journal until dry or time budget.
    Never re-process seen PDF URLs.
    """
    from src import hunt_db as hdb
    from src.topic_extract import process_pdf_topic
    from src import scraper

    key = journal["key"]
    homepage = (journal.get("homepageUrl") or "").strip()
    if not homepage:
        hdb.mark_journal_dry(key)
        return 0

    crawl = dict(journal.get("crawl") or {})
    seen = list(journal.get("seenPdfUrls") or [])
    seen_set = set(seen)

    issue_queue = list(crawl.get("issueQueue") or [])
    archive_queue = list(crawl.get("archiveQueue") or [])
    pages_done = set(crawl.get("pagesDone") or [])
    seed_done = bool(crawl.get("seedDone"))

    start = time.time()
    added = 0

    def time_up():
        return max_seconds > 0 and (time.time() - start) >= max_seconds

    def fetch(url: str) -> str:
        if hasattr(scraper, "_get"):
            return scraper._get(url, session=sess)
        r = sess.get(url, timeout=40)
        r.raise_for_status()
        return r.text

    def collect_pdfs_from_html(html: str, page_url: str):
        links = scraper.extract_links_from_html(html, page_url) or []
        out = []
        for L in links:
            if L.get("kind") == "pdf" and L.get("url"):
                out.append(L["url"])
            elif L.get("kind") in ("article", "listing") and L.get("url"):
                # follow article pages for real PDF hrefs (no guessed paths)
                try:
                    ah = fetch(L["url"])
                    for pu in scraper.extract_pdfs_from_article_page(ah, L["url"]) or []:
                        out.append(pu)
                    time.sleep(0.25)
                except Exception:
                    pass
        return list(dict.fromkeys(out))

    def try_pdf(url: str, source_page: str) -> bool:
        nonlocal added
        if not url or url in seen_set:
            return False
        seen_set.add(url)
        seen.append(url)
        try:
            row = process_pdf_topic(url)
            if row and hdb.save_topic(
                key,
                row.get("title") or "",
                row.get("topic") or row.get("title") or "",
                url,
                doi=row.get("doi") or "",
                source_page=source_page,
                emails=row.get("emails") or [],
            ):
                added += 1
                return True
        except Exception as e:
            logger.debug("%s pdf skip %s: %s", worker_id, url[-40:], e)
        time.sleep(0.4)
        return False

    if not seed_done:
        try:
            html = fetch(homepage)
            nav = scraper.expand_ojs_archive_issues(html, homepage) or {}
            for u in nav.get("issues") or []:
                if u not in issue_queue:
                    issue_queue.append(u)
            for u in nav.get("archive_pages") or []:
                if u not in archive_queue:
                    archive_queue.append(u)
            for L in scraper.extract_links_from_html(html, homepage) or []:
                u = L.get("url")
                if not u:
                    continue
                if L.get("kind") == "listing" and u not in issue_queue and u not in archive_queue:
                    if "archive" in u.lower():
                        archive_queue.append(u)
                    else:
                        issue_queue.append(u)
            for pu in collect_pdfs_from_html(html, homepage):
                if time_up():
                    break
                try_pdf(pu, homepage)
            seed_done = True
        except Exception as e:
            logger.warning("%s seed failed %s: %s", worker_id, key, e)
            seed_done = True

    def process_listing(page_url: str):
        if page_url in pages_done:
            return 0
        try:
            html = fetch(page_url)
        except Exception as e:
            logger.debug("%s listing %s: %s", worker_id, page_url[-50:], e)
            pages_done.add(page_url)
            return 0
        for pu in collect_pdfs_from_html(html, page_url):
            if time_up():
                break
            try_pdf(pu, page_url)
        for m in scraper.find_pagination_links(html, page_url) or []:
            if m and m not in issue_queue and m not in archive_queue and m not in pages_done:
                issue_queue.append(m)
        nav = scraper.expand_ojs_archive_issues(html, page_url) or {}
        for u in nav.get("issues") or []:
            if u not in issue_queue and u not in pages_done:
                issue_queue.append(u)
        pages_done.add(page_url)
        return 1

    # Walk archive pages then issues
    while not time_up() and archive_queue:
        u = archive_queue.pop(0)
        process_listing(u)
        hdb.save_crawl_progress(
            key,
            {
                "seedDone": seed_done,
                "issueQueue": issue_queue[:500],
                "archiveQueue": archive_queue[:200],
                "pagesDone": list(pages_done)[-2000:],
            },
            list(seen_set),
            email_count_delta=0,
        )

    while not time_up() and issue_queue:
        u = issue_queue.pop(0)
        process_listing(u)
        if added and added % 5 == 0:
            hdb.save_crawl_progress(
                key,
                {
                    "seedDone": seed_done,
                    "issueQueue": issue_queue[:500],
                    "archiveQueue": archive_queue[:200],
                    "pagesDone": list(pages_done)[-2000:],
                },
                list(seen_set),
                email_count_delta=0,
            )

    dry = seed_done and not issue_queue and not archive_queue
    hdb.save_crawl_progress(
        key,
        {
            "seedDone": seed_done,
            "issueQueue": issue_queue[:500],
            "archiveQueue": archive_queue[:200],
            "pagesDone": list(pages_done)[-2000:],
        },
        list(seen_set),
        email_count_delta=added,
    )
    if dry:
        logger.info("%s journal %s DRY — pagination finished (+%d emails this session)", worker_id, key, added)
        hdb.mark_journal_dry(key)
    else:
        logger.info(
            "%s journal %s pause queues issue=%d archive=%d seen_pdfs=%d +%d emails",
            worker_id,
            key,
            len(issue_queue),
            len(archive_queue),
            len(seen_set),
            added,
        )
    return added


def run_loop(max_runtime_seconds: int = 5 * 3600 + 1800, idle_sleep: int = 5):
    from src.config import HUNT_ENABLED
    from src import hunt_db as hdb
    from src import openalex as oa

    if not HUNT_ENABLED:
        logger.warning("HUNT_ENABLED is FALSE — hunt idle.")
        time.sleep(min(30, max_runtime_seconds))
        return 0

    try:
        hdb.ensure_hunt_indexes()
    except Exception as e:
        logger.warning("hunt indexes: %s", e)

    worker_num = int(os.getenv("HUNT_WORKER_NUM") or "1")
    run_id = str(os.getenv("GITHUB_RUN_ID") or f"local-{uuid.uuid4().hex[:8]}")
    worker_id = f"hunt-{run_id}-w{worker_num}-{uuid.uuid4().hex[:6]}"
    logger.info(
        "Worker %s starting run=%s max=%ss discover_cap=%s",
        worker_id,
        run_id,
        max_runtime_seconds,
        hdb.DISCOVER_CAP,
    )

    sess = _session()
    start = time.time()
    total = 0
    journals_touched = 0
    last_status_log = 0
    skip_base = (worker_num - 1) * 3

    while True:
        elapsed = time.time() - start
        if elapsed >= max_runtime_seconds:
            logger.info("Max runtime reached rows=%d journals=%d", total, journals_touched)
            break

        if elapsed - last_status_log >= 60:
            try:
                st = hdb.hunt_stats()
            except Exception:
                st = {}
            logger.info(
                "heartbeat elapsed=%.0fs remaining=%.0fs rows=%d stats=%s",
                elapsed,
                max_runtime_seconds - elapsed,
                total,
                st,
            )
            last_status_log = elapsed

        remaining = max_runtime_seconds - elapsed

        # --- HARVEST first when catalog is full enough ---
        if not hdb.should_discover() or hdb.active_harvest_count() > 0:
            journal = hdb.claim_journal_for_run(run_id, worker_id)
            if journal:
                journals_touched += 1
                budget = min(remaining - 30, 45 * 60)  # up to 45 min on one journal per slice
                if budget < 60:
                    budget = max(30, remaining - 10)
                try:
                    n = harvest_journal(worker_id, journal, sess, max_seconds=budget)
                    total += n
                except Exception as e:
                    logger.exception("%s harvest failed %s: %s", worker_id, journal.get("key"), e)
                # keep claim until dry or run ends — release only if not dry so next run continues
                j2 = hdb.db()[hdb.JOURNALS_COL].find_one({"key": journal["key"]})
                if j2 and j2.get("dry"):
                    pass  # already cleared claim in mark_journal_dry
                # if still not dry, leave claimedByRun=this run so same-run peers skip it;
                # next run_id will reclaim
                continue
            # No journal to harvest
            if not hdb.should_discover():
                logger.info("%s no harvest targets; idle", worker_id)
                time.sleep(idle_sleep * 4)
                continue

        # --- DISCOVER ---
        if hdb.should_discover():
            batch = 0
            try:
                for i, src in enumerate(oa.iter_oa_sources(per_page=25, max_pages=15)):
                    if time.time() - start >= max_runtime_seconds:
                        break
                    if not hdb.should_discover():
                        logger.info("%s discover cap reached — switch to harvest", worker_id)
                        break
                    if i < skip_base:
                        continue
                    try:
                        if discover_one(worker_id, src, sess):
                            batch += 1
                    except Exception as e:
                        logger.exception("%s discover fail: %s", worker_id, e)
                    time.sleep(idle_sleep)
            except Exception as e:
                logger.exception("%s openalex iter: %s", worker_id, e)
                time.sleep(idle_sleep * 3)
            if batch == 0:
                time.sleep(idle_sleep * 3)
            skip_base = (skip_base + worker_num) % 40
        else:
            time.sleep(idle_sleep)

    try:
        logger.info("%s done total_rows=%d stats=%s", worker_id, total, hdb.hunt_stats())
    except Exception:
        logger.info("%s done total_rows=%d", worker_id, total)
    return total
