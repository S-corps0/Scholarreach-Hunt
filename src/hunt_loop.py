"""
Auto-hunt fleet: each worker runs the FULL pipeline independently.

  1) Find journal (OpenAlex)
  2) Confirm crawlable (probe sample PDFs)
  3) Extract paper/PDF links
  4) Extract topic + author email (skip if no email)

No hunter/crawler/topicker split — avoids handoff errors.
Killswitch: HUNT_ENABLED=true|false
"""
import logging
import os
import time
import uuid
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("scholarreach.hunt")


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Scholarreach-Hunt/2.0 (independent topic+email)"})
    return s


def process_one_journal(worker_id: str, src: dict, sess: requests.Session) -> int:
    """
    Full pipeline for one OpenAlex source.
    Returns number of topic+email rows saved.
    """
    from src import openalex as oa
    from src import validate as v
    from src import hunt_db as hdb
    from src.topic_extract import process_pdf_topic
    from src import scraper

    name = src.get("display_name") or "Untitled journal"
    homepage = (src.get("homepage_url") or "").strip()
    openalex_id = src.get("id") or ""
    key = "openalex:" + (openalex_id.split("/")[-1] if "/" in openalex_id else openalex_id or name[:40])

    # --- 1+2: sample works, confirm crawlable ---
    try:
        works = oa.works_with_pdfs(openalex_id, per_page=10)
    except Exception as e:
        logger.debug("%s openalex works failed %s: %s", worker_id, name[:40], e)
        return 0
    if len(works) < 5:
        return 0

    alive = 0
    with_title = 0
    sample_pdfs = []
    for w in works[:10]:
        try:
            p = v.probe_pdf(w["pdf_url"], session=sess)
        except Exception:
            p = {"alive": False}
        if p.get("alive"):
            alive += 1
            sample_pdfs.append({
                "pdf_url": w["pdf_url"],
                "title": w.get("title") or "",
                "doi": w.get("doi") or "",
            })
            if (w.get("title") or "").strip():
                with_title += 1
        time.sleep(0.35)

    if alive < 7 or with_title < 5:
        hdb.upsert_journal({
            "key": key,
            "displayName": str(name)[:160],
            "homepageUrl": str(homepage)[:500],
            "openalexId": str(openalex_id)[:120],
            "issn": src.get("issn") or [],
            "status": "rejected",
            "sampleChecked": min(10, len(works)),
            "sampleAlive": alive,
            "sampleWithTopic": with_title,
            "rejectReason": f"sample alive={alive}/10 titles={with_title}/10",
            "pdfQueued": 0,
            "topicCount": 0,
            "emailCount": 0,
        })
        logger.info("%s rejected %s alive=%d titles=%d", worker_id, name[:40], alive, with_title)
        return 0

    hdb.upsert_journal({
        "key": key,
        "displayName": str(name)[:160],
        "homepageUrl": str(homepage)[:500],
        "openalexId": str(openalex_id)[:120],
        "issn": src.get("issn") or [],
        "status": "ready",
        "sampleChecked": min(10, len(works)),
        "sampleAlive": alive,
        "sampleWithTopic": with_title,
        "rejectReason": None,
        "pdfQueued": 0,
        "topicCount": 0,
        "emailCount": 0,
    })
    logger.info("%s crawlable %s — extracting links+emails", worker_id, name[:50])

    # --- 3: extract more PDF links (homepage / OJS patterns via scraper) ---
    pdf_urls = []
    seen = set()
    for m in sample_pdfs:
        u = m.get("pdf_url")
        if u and u not in seen:
            seen.add(u)
            pdf_urls.append(m)

    if homepage and homepage.startswith("http"):
        try:
            # generic_discover(seed_urls, max_pages=..., max_pdfs=...)
            discovered = scraper.generic_discover([homepage], max_pages=12, max_pdfs=80)
            for item in discovered or []:
                if isinstance(item, str) and item.startswith("http") and item not in seen:
                    seen.add(item)
                    pdf_urls.append({"pdf_url": item, "title": "", "doi": ""})
                elif isinstance(item, dict):
                    pu = item.get("pdf_url") or item.get("url") or item.get("href")
                    if pu and str(pu).startswith("http") and pu not in seen:
                        seen.add(pu)
                        pdf_urls.append({
                            "pdf_url": pu,
                            "title": item.get("title") or "",
                            "doi": item.get("doi") or "",
                        })
        except Exception as e:
            logger.debug("%s link extract %s: %s", worker_id, homepage[-40:], e)

        # more OA works as backup links
        try:
            more = oa.works_with_pdfs(openalex_id, per_page=50)
            for w in more:
                u = w.get("pdf_url")
                if u and u not in seen:
                    seen.add(u)
                    pdf_urls.append({
                        "pdf_url": u,
                        "title": w.get("title") or "",
                        "doi": w.get("doi") or "",
                    })
        except Exception:
            pass

    hdb.bump_counts(key, pdf_delta=len(pdf_urls))

    # --- 4: extract topic + email from each PDF (strict: email required) ---
    added = 0
    for m in pdf_urls[:60]:
        pdf_url = m.get("pdf_url")
        if not pdf_url:
            continue
        try:
            out = process_pdf_topic(pdf_url)
            ok = hdb.save_topic(
                key,
                out.get("title") or m.get("title") or "Untitled paper",
                out.get("topic") or m.get("title") or "Untitled paper",
                pdf_url,
                doi=m.get("doi") or out.get("doi") or "",
                source_page=homepage,
                author_name=out.get("authorName"),
                emails=out.get("emails") or [],
            )
            if ok:
                added += 1
        except Exception as e:
            logger.debug("%s pdf skip %s: %s", worker_id, pdf_url[-50:], str(e)[:80])
        time.sleep(0.5)

    if added:
        hdb.bump_counts(key, topic_delta=added, email_delta=added)
        logger.info("%s %s +%d topic+email rows (from %d pdfs)", worker_id, key, added, len(pdf_urls))
    return added


def run_loop(max_runtime_seconds: int = 5 * 3600 + 1800, idle_sleep: int = 5):
    from src.config import HUNT_ENABLED

    if not HUNT_ENABLED:
        logger.warning(
            "HUNT_ENABLED is FALSE — hunt idle. Set secret HUNT_ENABLED=true to run."
        )
        return 0

    from src import hunt_db as hdb
    from src import openalex as oa

    try:
        hdb.ensure_hunt_indexes()
    except Exception as e:
        logger.warning("hunt indexes: %s", e)

    worker_num = int(os.getenv("HUNT_WORKER_NUM") or "1")
    worker_id = (
        f"hunt-{os.getenv('GITHUB_RUN_ID', 'local')}-"
        f"w{worker_num}-{uuid.uuid4().hex[:6]}"
    )
    logger.info("Independent hunt worker %s starting (full pipeline)", worker_id)

    sess = _session()
    start = time.time()
    total = 0
    # Stagger OpenAlex page start so 40 workers don't all hit page 1
    # Stagger so workers don't all process the same first journals
    skip = (worker_num - 1) * 3

    try:
        for i, src in enumerate(oa.iter_oa_sources(per_page=25, max_pages=50)):
            if time.time() - start >= max_runtime_seconds:
                break
            if i < skip:
                continue
            try:
                total += process_one_journal(worker_id, src, sess)
            except Exception as e:
                logger.exception("%s journal failed: %s", worker_id, e)
            time.sleep(idle_sleep)
    except Exception as e:
        logger.exception("%s source iter failed: %s", worker_id, e)

    try:
        logger.info("%s done total_rows=%d stats=%s", worker_id, total, hdb.hunt_stats())
    except Exception:
        logger.info("%s done total_rows=%d", worker_id, total)
    return total
