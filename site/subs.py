"""Fetch Persian subtitles for a movie/series from subf2m.co.

Pipeline: search by TMDB title+year -> pick Farsi/Persian entry ->
download newest zip -> extract SRT -> convert to clean VTT.

All network calls are bounded; the module never raises on failure,
it returns None so callers can fall back (e.g. AI translation).
"""
from __future__ import annotations

import io
import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass

import httpx
import pysubs2

log = logging.getLogger(__name__)

BASE = "https://subf2m.co"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CACHE_DIR = os.environ.get("SUBS_CACHE_DIR", "/tmp/fistream-subs")


@dataclass
class SubResult:
    vtt_path: str
    source_url: str


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE,
        timeout=httpx.Timeout(20),
        follow_redirects=True,
        headers={"User-Agent": UA},
    )


def search_title(client: httpx.Client, title: str, year: int | None):
    """Return list of {href, text} subtitle-page candidates."""
    r = client.get("/subtitles/searchbytitle",
                   params={"query": title, "l": ""})
    r.raise_for_status()
    pattern = re.compile(
        r'<a href="(/subtitles/[a-z0-9-]+)"[^>]*>\s*'
        r'([^<]+?)\s*\((\d{4})\)')

    # The listing page embeds results as plain HTML links.
    found = []
    for href, name, yr in pattern.findall(r.text):
        found.append({"href": href, "name": name.strip(), "year": int(yr)})
    return found


def pick_candidate(cands, title: str, year: int | None):
    """Score candidates by title similarity and exact year match."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    want = norm(title)
    best, best_score = None, 0.0
    for c in cands:
        got = norm(c["name"])
        if not got:
            continue
        score = 1.0 if got == want else 0.0
        if not score:
            # containment heuristic
            inter = len(set(got) & set(want))
            score = inter / max(len(set(want)), 1) * 0.5
        if year and c["year"] == year:
            score += 0.5
        elif year and c["year"] != year:
            score -= 0.25
        if score > best_score:
            best, best_score = c, score
    return best if best and best_score >= 0.6 else None


def persian_sub_links(client: httpx.Client, page_href: str,
                      limit: int = 5) -> list:
    """Newest-first list of Farsi/Persian subtitle detail links."""
    # NOTE: subf2m mixes single/double quotes in attributes — accept both.
    detail_re = re.compile(
        r"""['"](/subtitles/[a-z0-9-]+/farsi_persian/(\d+))['"]""")
    r = client.get(page_href)
    r.raise_for_status()
    matches = detail_re.findall(r.text)
    if not matches:
        # fall back to the language index page
        m2 = re.search(
            r"""['"](/subtitles/[a-z0-9-]+/farsi_persian)['"]""", r.text)
        if not m2:
            return []
        r2 = client.get(m2.group(1))
        r2.raise_for_status()
        matches = detail_re.findall(r2.text)
        if not matches:
            return []
    # Highest id == most recently uploaded subtitle
    seen, ordered = set(), []
    for href, sid in sorted(matches, key=lambda m: -int(m[1])):
        if href not in seen:
            seen.add(href)
            ordered.append(href)
        if len(ordered) >= limit:
            break
    return ordered


def download_zip(client: httpx.Client, detail_href: str) -> bytes | None:
    r = client.get(detail_href + "/download", headers={
        "Referer": BASE + detail_href})
    r.raise_for_status()
    if r.content[:2] != b"PK":
        log.warning("unexpected payload at %s (%dB)", detail_href,
                    len(r.content))
        return None
    return r.content


def zip_to_vtt(zbytes: bytes, out_path: str,
               episode: tuple | None = None) -> str | None:
    """Convert an SRT zip to VTT. For TV, episode=(season, ep) picks the
    matching SxxEyy file from a season pack."""
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    srts = [n for n in zf.namelist() if n.lower().endswith(".srt")]

    if episode:
        s, e = episode
        pat = re.compile(rf"s{s:02d}\s*e{e:02d}|s0?{s}e0?{e}",
                         re.IGNORECASE)
        matched = [n for n in srts if pat.search(n.replace(" ", ""))]
        log.info("episode filter S%02dE%02d: %d/%d files match", s, e,
                 len(matched), len(srts))
        srts = matched or srts  # fall back to whole pack if naming differs

    if not srts:
        return None
    # prefer largest (usually full sync, not sample)
    srts.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
    for name in srts:                      # try each until one is Persian
        raw = zf.read(name)
        text = raw.decode("utf-8", errors="replace")
        try:
            subs = pysubs2.SSAFile.from_string(text, format_="srt")
        except Exception:
            continue
        new = pysubs2.SSAFile()
        for ev in subs:
            t = ev.plaintext.strip()
            if t:
                new.events.append(pysubs2.SSAEvent(
                    start=ev.start, end=ev.end,
                    text=t.replace("\n", "\\N")))
        if not new.events:
            continue
        # Reject mislabeled uploads: require real Persian letters.
        sample = " ".join(ev.plaintext for ev in new.events[:80])
        fa = len(re.findall(r"[\u0600-\u06FF]", sample))
        latin = len(re.findall(r"[A-Za-z]", sample))
        if fa < 40 or latin > fa * 0.6:
            log.info("rejecting %s: looks non-Persian (fa=%d latin=%d)",
                     name, fa, latin)
            continue
        new.save(out_path, format_="vtt")
        return out_path
    return None


def get_persian_subtitle(title: str, year: int | None = None,
                         tmdb_id: int | None = None,
                         episode: tuple | None = None) -> SubResult | None:
    """Main entry. For TV pass episode=(season, episode)."""
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", f"{title}_{year or ''}").strip("_")
    if episode:
        safe += f"_S{episode[0]:02d}E{episode[1]:02d}"
    cache = os.path.join(CACHE_DIR, f"{safe}.vtt")
    if os.path.exists(cache) and os.path.getsize(cache) > 200 \
            and time.time() - os.path.getmtime(cache) < 86400 * 30:
        return SubResult(vtt_path=cache, source_url="(cached)")

    try:
        with _client() as client:
            cands = search_title(client, title, year)
            cand = pick_candidate(cands, title, year)
            if not cand:
                log.info("no subf2m candidate for %s (%s)", title, year)
                return None
            # Try several Persian uploads — some are mislabeled English.
            for detail in persian_sub_links(client, cand["href"], limit=5):
                zbytes = download_zip(client, detail)
                if not zbytes:
                    continue
                os.makedirs(CACHE_DIR, exist_ok=True)
                out = cache
                if zip_to_vtt(zbytes, out, episode=episode):
                    return SubResult(vtt_path=out, source_url=BASE + detail)
            return None
    except Exception as e:
        log.warning("sub pipeline failed for %s: %s", title, e)
        return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    t = sys.argv[1] if len(sys.argv) > 1 else "Fight Club"
    y = int(sys.argv[2]) if len(sys.argv) > 2 else None
    res = get_persian_subtitle(t, y)
    print(res)
