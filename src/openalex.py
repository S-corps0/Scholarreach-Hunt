"""
OpenAlex client for the hunt fleet (topic-only).

OpenAlex allows bots; be polite anyway:
- descriptive User-Agent with contact
- mailto param if OPENALEX_MAILTO set
- small pages, short sleeps between calls
"""
import logging
import time
import urllib.parse
import urllib.request
import json
import os

logger = logging.getLogger(__name__)

BASE = "https://api.openalex.org"
CONTACT = os.getenv("OPENALEX_MAILTO", "").strip()
UA = "Scholarreach-Hunt/1.0 (topic+email independent hunt; research bot)"
if CONTACT:
    UA += f" (mailto:{CONTACT})"


def _get_json(url: str, timeout: int = 25) -> dict:
    if CONTACT and "mailto=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}mailto={urllib.parse.quote(CONTACT)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def iter_oa_sources(per_page: int = 50, max_pages: int = 20, cursor: str = "*"):
    """Yield OpenAlex OA journal sources (cursor pagination)."""
    cur = cursor
    for _ in range(max_pages):
        url = (
            f"{BASE}/sources?filter=is_oa:true,type:journal"
            f"&per-page={per_page}&cursor={urllib.parse.quote(cur)}"
            "&select=id,display_name,host_organization_name,issn,homepage_url,works_count"
        )
        data = _get_json(url)
        for s in data.get("results", []):
            yield s
        meta = data.get("meta", {})
        cur = meta.get("next_cursor")
        if not cur:
            break
        time.sleep(1.0)


def works_with_pdfs(source_id: str, per_page: int = 10):
    """Newest OA works for a source with direct pdf_url (best_oa then primary)."""
    sid = source_id.split("/")[-1] if "/" in source_id else source_id
    url = (
        f"{BASE}/works?filter=primary_location.source.id:{sid},is_oa:true"
        f"&per-page={per_page}&sort=publication_date:desc"
        "&select=id,display_name,doi,best_oa_location,primary_location"
    )
    data = _get_json(url)
    out = []
    for w in data.get("results", []):
        best = (w.get("best_oa_location") or {}).get("pdf_url")
        prim = (w.get("primary_location") or {}).get("pdf_url")
        pdf = best or prim
        if not pdf:
            continue
        # OpenAlex sometimes returns http; prefer https for crawlability
        if pdf.startswith("http://"):
            pdf = "https://" + pdf[len("http://"):]
        out.append({
            "openalex_id": w.get("id", ""),
            "title": (w.get("display_name") or "Untitled").strip()[:300],
            "doi": (w.get("doi") or "").strip()[:200],
            "pdf_url": pdf,
        })
    return out
