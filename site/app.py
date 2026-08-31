"""Fistream — Persian movie streaming aggregator UI.

FastAPI + Jinja2 server-rendered pages (RTL Persian).
Catalog/search/posters: Stremio Cinemeta (no API key needed).
Playback: multi-provider embeds (vidlink.pro, videasy, vidsrc.su, 2embed).
Subtitles: subf2m pipeline + AvalAI translation fallback, served as VTT.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import ai_subs
import subs

log = logging.getLogger("fistream")
logging.basicConfig(level=logging.INFO)

CINEMETA = "https://v3-cinemeta.strem.io"

app = FastAPI(title="Fistream")
# VidLink's player fetches our .vtt files cross-origin; without this
# the browser silently blocks the subtitle download.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

GENRES_FA = {
    "Action": "اکشن", "Comedy": "کمدی", "Drama": "درام", "Horror": "ترسناک",
    "Romance": "عاشقانه", "Sci-Fi": "علمی‌تخیلی", "Thriller": "هیجانی",
    "Animation": "انیمیشن", "Crime": "جنایی", "Adventure": "ماجراجویی",
    "Fantasy": "فانتزی", "Mystery": "معمایی", "Family": "خانوادگی",
    "Documentary": "مستند", "War": "جنگی",
}


_CAT_TTL = 600            # seconds a catalog response stays fresh
_cat_cache: dict[str, tuple[float, dict]] = {}


async def cinemeta(path: str, use_cache: bool = True) -> dict | None:
    import time as _time
    if use_cache and path in _cat_cache:
        ts, data = _cat_cache[path]
        if _time.time() - ts < _CAT_TTL:
            return data
    try:
        async with httpx.AsyncClient(timeout=15,
                                     follow_redirects=True) as c:
            r = await c.get(f"{CINEMETA}{path}")
            if r.status_code != 200:
                log.warning("cinemeta %s -> %s", path, r.status_code)
                return None
            data = r.json()
            if use_cache and path.startswith("/catalog"):
                _cat_cache[path] = (_time.time(), data)
            return data
    except httpx.HTTPError as e:
        log.warning("cinemeta %s failed: %s", path, e)
        return None


def _row(meta: dict) -> dict:
    year = ""
    rel = meta.get("releaseInfo") or ""
    for part in rel.replace("–", "-").split("-"):
        if part.isdigit() and len(part) == 4:
            year = part
            break
    return {"id": meta.get("imdb_id") or meta["id"],
            "type": meta["type"], "title": meta.get("name"),
            "year": year, "poster": meta.get("poster")}


async def _catalog(catalog_path: str, limit: int = 18) -> list[dict]:
    data = await cinemeta(catalog_path)
    return [_row(m) for m in (data or {}).get("metas", [])[:limit]]


@app.get("/")
async def home(request: Request, q: str | None = None):
    if q:
        items = []
        for typ in ("movie", "series"):
            items += await _catalog(
                f"/catalog/{typ}/top/search={urllib.parse.quote(q)}.json", 16)
        return templates.TemplateResponse(
            request, "home.html",
            {"items": items, "q": q, "nav": "home"})

    trending, movies, shows = await _catalog("/catalog/all/day/trending.json", 20), [], []
    # 'all' trending may fail; fall back per-type
    if not trending:
        trending = await _catalog("/catalog/movie/day/trending.json", 20)
    import asyncio as _aio
    movies, shows, theaters, action, comedy, horror = map(list, await _aio.gather(
        _catalog("/catalog/movie/top.json"),
        _catalog("/catalog/series/top.json"),
        _catalog("/catalog/movie/now_theaters.json", 18),
        _catalog("/catalog/movie/top/genre=Action.json", 18),
        _catalog("/catalog/movie/top/genre=Comedy.json", 18),
        _catalog("/catalog/movie/top/genre=Horror.json", 18)))

    return templates.TemplateResponse(
        request, "home.html",
        {"trending": trending, "movies": movies, "shows": shows,
         "theaters": theaters, "action": action, "comedy": comedy,
         "horror": horror, "genres": GENRES_FA.items(), "q": "",
         "nav": "home"})


@app.get("/browse/{type}")
async def browse(request: Request, type: str,
                 genre: str | None = None, skip: int = 0):
    if type not in ("movie", "series"):
        raise HTTPException(404)
    extra = f"genre={genre}" if genre else ""
    catalog = f"/catalog/{type}/top/{extra}.json" if extra \
        else f"/catalog/{type}/top/skip={skip}.json"
    if extra and skip:
        catalog = f"/catalog/{type}/top/{extra}&skip={skip}.json"
    items = await _catalog(catalog, 36)
    heading = ("فیلم‌ها" if type == "movie" else "سریال‌ها") + \
        (f" — {GENRES_FA.get(genre, genre)}" if genre else "")
    return templates.TemplateResponse(
        request, "browse.html",
        {"items": items, "type": type, "genres": GENRES_FA.items(),
         "cur_genre": genre or "", "heading": heading,
         "has_more": len(items) >= 30, "next_skip": skip + 36,
         "nav": type})


@app.get("/watch/{type}/{tid}")
async def watch(request: Request, type: str, tid: str,
                s: int | None = None, e: int | None = None):
    if type not in ("movie", "series") or not tid.startswith("tt"):
        raise HTTPException(404)
    data = await cinemeta(f"/meta/{type}/{tid}.json")
    meta = (data or {}).get("meta") or {}
    title = meta.get("name") or tid
    rel = meta.get("releaseInfo") or ""
    year = next((p for p in rel.replace("–", "-").split("-")
                 if p.isdigit() and len(p) == 4), "")

    seasons: list[int] = []
    episodes: list[dict] = []
    ep_name = ""
    if type == "series":
        by_season: dict[int, list] = {}
        for v in meta.get("videos") or []:
            sn, en = v.get("season"), v.get("episode")
            if sn and en:
                by_season.setdefault(sn, []).append(
                    {"s": sn, "e": en, "name": v.get("name") or ""})
        seasons = sorted(by_season)
        cur_s = s if s in seasons else (seasons[0] if seasons else 1)
        episodes = sorted(by_season.get(cur_s, []), key=lambda x: x["e"])
        cur_e = e if any(x["e"] == e for x in episodes) \
            else (episodes[0]["e"] if episodes else 1)
        ep_name = next((x["name"] for x in episodes if x["e"] == cur_e), "")
        s, e = cur_s, cur_e

    base = str(request.base_url).rstrip("/")
    sub_url = (f"{base}/subs/{type}/{tid}.vtt"
               f"?title={urllib.parse.quote(title)}&year={year}")
    if type == "series":
        sub_url += f"&s={s}&e={e}"

    backdrop = (meta.get("background") or meta.get("poster") or "")
    poster = meta.get("poster") or ""
    ts = meta.get("trailerStreams") or []
    trailer_yt = ts[0].get("ytId") if ts else None

    similar = await _catalog(f"/catalog/{type}/top/similar={tid}.json", 14)

    return templates.TemplateResponse(
        request, "watch.html",
        {"tid": tid, "type": type, "title": title, "year": year,
         "meta": {"rating": (meta.get("imdbRating") or ""),
                  "runtime": (meta.get("runtime") or ""),
                  "genres": (meta.get("genres") or [])},
         "overview": meta.get("description"),
         "poster": poster, "backdrop": backdrop,
         "sub_url": sub_url, "similar": similar,
         "trailer_yt": trailer_yt,
         "seasons": seasons, "episodes": episodes, "ep_name": ep_name,
         "cur_s": s or 1, "cur_e": e or 1, "q": "", "nav": ""})


def _load_key():
    env = open(os.path.expanduser("~/.hermes/.env")).read()
    m = re.search(
        r'HERMES_CUSTOM_API_AVALAI_IR_API_KEY\s*=\s*"?([^"\s]+)"?', env)
    if m:
        os.environ.setdefault("HERMES_CUSTOM_API_AVALAI_IR_API_KEY",
                              m.group(1))


@app.get("/subs/movie/{tid}.vtt")
@app.get("/subs/series/{tid}.vtt")
# NOTE: deliberately a *sync* def — FastAPI runs it in a threadpool, so a
# slow subf2m scrape / AI translation never blocks the event loop and
# other users keep being served while one translation runs.
def subtitle_file(tid: str,
                        title: str = Query(...),
                        year: str | None = None,
                        s: int | None = None,
                        e: int | None = None,
                        force_ai: int = 0):
    y = int(year) if year and year.isdigit() else None
    episode = (s, e) if (s and e) else None
    res = None if force_ai else \
        subs.get_persian_subtitle(title=title, year=y, episode=episode)
    if not res:
        # ---- AI-translate fallback (English sub -> Persian) ----
        log.info("no fa sub for %s (%s); trying AI translation", title, y)
        _load_key()
        try:
            en_path = ai_subs.fetch_english_srt(title, y, episode)
            if en_path:
                out = re.sub(r"[^a-zA-Z0-9]+", "_",
                             f"{title}_{year or ''}").strip("_")
                if episode:
                    out += f"_S{episode[0]:02d}E{episode[1]:02d}"
                out_path = f"/tmp/fistream-subs/{out}_ai.vtt"
                vtt = ai_subs.translate_srt_to_vtt(
                    open(en_path, encoding="utf-8", errors="replace").read(),
                    out_path)
                if vtt:
                    return FileResponse(vtt, media_type="text/vtt",
                                        filename=f"{tid}.fa.vtt")
        except Exception as exc:
            log.warning("AI fallback failed for %s: %s", title, exc)
        raise HTTPException(404, "no persian subtitle found")
    return FileResponse(res.vtt_path, media_type="text/vtt",
                        filename=f"{tid}.fa.vtt")


@app.get("/desc/{tid}.txt")
async def description(tid: str, title: str = Query(...), lang: str = "fa"):
    """Return movie description in requested language (cached)."""
    import os as _os
    cache_dir = "/tmp/fistream-desc"
    _os.makedirs(cache_dir, exist_ok=True)
    cache = _os.path.join(cache_dir, f"{tid}.{lang}")
    if _os.path.exists(cache):
        return FileResponse(cache, media_type="text/plain")
    data = await cinemeta(f"/meta/movie/{tid}.json")
    desc = ((data or {}).get("meta") or {}).get("description") or ""
    if lang == "fa" and desc:
        fa = ai_subs.translate_text(desc)
        if fa:
            desc = fa
        else:
            raise HTTPException(404)
    with open(cache, "w") as f:
        f.write(desc)
    return FileResponse(cache, media_type="text/plain")


@app.get("/healthz")
async def healthz():
    return {"ok": True}
