"""AI subtitle translation — multi-provider free-tier rotation.

Chains free providers; when one hits quota/limit, falls through to the next.
Combined with disk cache (translated once = free forever), this gives
effectively unlimited free translation for users.

Providers (all free tier):
  - OpenRouter free models (50 req/day across several models)
  - DeepSeek (if balance available)
  - Groq (free tier)
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx
import pysubs2

log = logging.getLogger(__name__)

BATCH = 60  # cues per request — bigger batches = fewer API calls

# ── Provider chain ──────────────────────────────────────────────
# Each provider: name, url, headers, payload builder, model list
def _env():
    return open(os.path.expanduser("~/.hermes/.env")).read()

def _key(name):
    # 1) Runtime env (Render injects these)
    v = os.environ.get(name)
    if v:
        return v.strip().strip('"')
    # 2) Fall back to local Hermes .env (dev laptop)
    m = re.search(rf'^{name}\s*=\s*"?([^"\s]+)"?',
                  open(os.path.expanduser("~/.hermes/.env")).read(),
                  re.MULTILINE)
    return m.group(1) if m else ""


def _or_payload(model, messages):
    return {"model": model, "temperature": 0.2, "messages": messages}


def _or_headers():
    return {"Authorization": f"Bearer {_key('OPENROUTER_API_KEY')}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://fistream.local",
            "X-Title": "Fistream"}


PROVIDERS = [
    {  # OpenRouter — primary (free models)
        "name": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "headers": _or_headers,
        "models": ["minimax/minimax-m3:free",
                   "google/gemma-4-26b-a4b-it:free",
                   "meta-llama/llama-3.1-8b-instruct:free",
                   "mistralai/mistral-7b-instruct:free"],
        "payload": _or_payload,
    },
]


PROMPT = (
    "Translate English subtitles to natural conversational Persian (Farsi). "
    "Input: JSON array of strings (may include \\n line breaks and speaker dashes). "
    "Output ONLY a JSON array of the same size with translations, keeping \\n "
    "breaks and leading dashes. Keep proper names transliterated. "
    "Output valid JSON only, no markdown fences."
)


def _translate_one(client, provider, model, texts):
    """Try one provider+model. Returns list[str] or raises."""
    r = client.post(provider["url"],
                    headers=provider["headers"](),
                    json=provider["payload"](model, [
                        {"role": "system", "content": PROMPT},
                        {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
                    ]),
                    timeout=120)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    out = json.loads(content)
    if len(out) != len(texts):
        raise ValueError(f"size mismatch: got {len(out)} want {len(texts)}")
    return out


def _translate_batch(client, texts):
    """Try providers in order until one works."""
    for provider in PROVIDERS:
        for model in provider["models"]:
            try:
                out = _translate_one(client, provider, model, texts)
                log.info("%s/%s: ok (%d cues)", provider["name"], model, len(out))
                return out
            except Exception as e:
                log.warning("%s/%s failed: %s", provider["name"], model, str(e)[:80])
                continue
    raise RuntimeError("all providers exhausted")


def translate_srt_to_vtt(srt_text: str, out_path: str) -> str | None:
    """Translate an SRT string to Persian VTT. Returns out_path or None."""
    try:
        subs = pysubs2.SSAFile.from_string(srt_text, format_="srt")
    except Exception:
        return None

    texts = [ev.plaintext.strip() for ev in subs.events]
    if not texts:
        return None

    translated = []
    with httpx.Client(timeout=120) as client:
        for i in range(0, len(texts), BATCH):
            batch = texts[i:i + BATCH]
            for attempt in range(2):
                try:
                    translated.extend(_translate_batch(client, batch))
                    break
                except Exception as e:
                    log.warning("batch %d attempt %d: %s", i // BATCH, attempt + 1, str(e)[:80])
                    if attempt == 1:
                        translated.extend(batch)  # keep original on total failure

    new = pysubs2.SSAFile()
    for ev, tr in zip(subs.events, translated):
        t = (tr or ev.plaintext).strip()
        if t:
            new.events.append(pysubs2.SSAEvent(
                start=ev.start, end=ev.end,
                text=t.replace("\n", "\\N")))
    new.save(out_path, format_="vtt")
    return out_path


def fetch_english_srt(title: str, year: int | None,
                      episode: tuple | None = None) -> str | None:
    """Find an English SRT for the title (subf2m), return extracted path."""
    import io as _io
    import zipfile as _zipfile
    import subs as _subs

    try:
        with _subs._client() as client:
            cands = _subs.search_title(client, title, year)
            cand = _subs.pick_candidate(cands, title, year)
            if not cand:
                return None
            r = client.get(cand["href"])
            r.raise_for_status()
            m = re.findall(
                r"""['"](/subtitles/[a-z0-9-]+/english/(\d+))['"]""",
                r.text)
            if not m:
                return None
            detail = max(m, key=lambda x: int(x[1]))[0]
            zbytes = _subs.download_zip(client, detail)
            if not zbytes:
                return None
            zf = _zipfile.ZipFile(_io.BytesIO(zbytes))
            srts = [n for n in zf.namelist() if n.lower().endswith(".srt")]
            if episode:
                s, e = episode
                pat = re.compile(rf"s0?{s}\s*e0?{e}", re.IGNORECASE)
                matched = [n for n in srts if pat.search(n.replace(" ", ""))]
                srts = matched or srts
            if not srts:
                return None
            srts.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
            path = os.path.join("/tmp", "fistream-en.srt")
            with open(path, "wb") as f:
                f.write(zf.read(srts[0]))
            return path
    except Exception as e:
        log.warning("english srt fetch failed for %s: %s", title, e)
        return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    t = sys.argv[1] if len(sys.argv) > 1 else "Fight Club"
    y = int(sys.argv[2]) if len(sys.argv) > 2 else None
    en = fetch_english_srt(t, y)
    if en:
        out = f"/tmp/{t.replace(' ', '_')}_fa.vtt"
        r = translate_srt_to_vtt(open(en, encoding="utf-8").read(), out)
        print(r)
