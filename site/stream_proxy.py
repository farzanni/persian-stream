"""Stream proxy — fetches video from provider CDNs server-side so Iranian
clients never connect to blocked domains directly."""
import re
import httpx
from fastapi.responses import StreamingResponse

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


async def proxy_stream(url: str, referer: str = ""):
    """Fetch a video/stream from a provider CDN and relay to client."""
    headers = {**_HEADERS}
    if referer:
        headers["Referer"] = referer
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return None
            content_type = r.headers.get("content-type", "")
            if "m3u8" in content_type or url.endswith(".m3u8"):
                body = _rewrite_hls_segments(r.text, url)
                return StreamingResponse(
                    iter([body.encode()]),
                    media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-cache"},
                )
            return StreamingResponse(
                r.iter_bytes(65536),
                media_type=content_type,
                headers={"Cache-Control": "no-cache"},
            )
    except httpx.HTTPError:
        return None


def _rewrite_hls_segments(playlist: str, base_url: str) -> str:
    def _fix(line: str) -> str:
        line = line.strip()
        if not line or line.startswith("#"):
            return line
        if line.startswith("http"):
            return f"/stream?url={line}"
        from urllib.parse import urljoin
        absolute = urljoin(base_url, line)
        return f"/stream?url={absolute}"
    return "\n".join(_fix(l) for l in playlist.split("\n"))


async def resolve_stream(provider_key: str, tid: str, type: str = "movie",
                         s: int = 0, e: int = 0) -> str | None:
    """Try to extract a direct stream URL from a known provider."""
    patterns = {
        "2embed": f"https://www.2embed.cc/embed/{tid}" + (f"~{s}~{e}" if type == "series" else ""),
        "vidlink": f"https://vidlink.pro/{type}/{tid}" + (f"?s={s}&e={e}" if type == "series" else ""),
        "vidsrcsu": f"https://vidsrc.su/embed/{type}/{tid}" + (f"&s={s}&e={e}" if type == "series" else ""),
        "videasy": f"https://player.videasy.net/{type}/{tid}" + (f"/{s}-{e}" if type == "series" else ""),
    }
    embed_url = patterns.get(provider_key, "")
    if not embed_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(embed_url, headers=_HEADERS)
            if r.status_code != 200:
                return None
            html = r.text
    except httpx.HTTPError:
        return None
    for ext in (".m3u8", ".mp4"):
        urls = re.findall(rf'(https?://[^\s"\'<>]+\.{ext.lstrip(".")}[^\s"\'<>]*)', html)
        if urls:
            return urls[0]
    api_patterns = [
        r'(https?://[^"\'<>]{10,}/api/[^\s"\'<>]+)',
        r'(https?://[^"\'<>]{10,}/stream/[^\s"\'<>]+)',
        r'(https?://[^"\'<>]{10,}/watch/[^\s"\'<>]+)',
    ]
    for pat in api_patterns:
        urls = re.findall(pat, html)
        if urls:
            return urls[0]
    return None
