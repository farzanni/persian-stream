"""AI subtitle translation via AvalAI (OpenAI-compatible).

Fallback when subf2m has no genuine Persian upload: take the English
SRT, batch-translate cue texts to Persian, re-tim onto original cues.

Cost control: cheap model, large batches, exact JSON in/out.
A 2,000-cue movie costs roughly 0.5-1.5 cents of credit.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx
import pysubs2

log = logging.getLogger(__name__)

BASE_URL = "https://api.avalai.ir/v1"
MODEL = os.environ.get("FISTRANS_MODEL", "gpt-4o-mini")
BATCH = 40          # cues per request


def _key() -> str:
    for name in ("HERMES_CUSTOM_API_AVALAI_IR_API_KEY", "AVALAI_API_KEY"):
        v = os.environ.get(name)
        if v:
            return v.strip().strip('"')
    # last resort: parse Hermes .env directly
    env = os.path.expanduser("~/.hermes/.env")
    m = re.search(r'HERMES_CUSTOM_API_AVALAI_IR_API_KEY\s*=\s*"?([^"\s]+)"?',
                  open(env).read())
    return m.group(1) if m else ""


PROMPT = (
    "You translate English subtitles to natural conversational Persian "
    "(Farsi). Input: JSON array of strings (may include \\n line breaks "
    "and speaker dashes). Output ONLY a JSON array of the same size with "
    "translations, keeping \\n breaks and leading dashes. Keep proper "
    "names transliterated.")


def _translate_batch(client: httpx.Client, texts: list[str]) -> list[str]:
    r = client.post(f"{BASE_URL}/chat/completions", headers={
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
    }, json={
        "model": MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
        ],
    })
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    # strip accidental markdown fences
    content = re.sub(r"^```(json)?|```$", "", content,
                     flags=re.MULTILINE).strip()
    out = json.loads(content)
    assert isinstance(out, list) and len(out) == len(texts), \
        f"batch size mismatch: {len(out)} != {len(texts)}"
    return [str(x).strip() or t for x, t in zip(out, texts)]


def translate_srt_to_vtt(srt_text: str, out_path: str,
                         max_cues: int | None = None) -> str | None:
    """Translate English SRT text -> Persian VTT file. Returns path."""
    subs = pysubs2.SSAFile.from_string(srt_text, format_="srt")
    events = [ev for ev in subs if ev.plaintext.strip()]
    if max_cues:
        events = events[:max_cues]
    if not events:
        return None

    texts = [ev.plaintext.replace("\n", "\n") for ev in events]
    translated: list[str] = []
    with httpx.Client(timeout=httpx.Timeout(120)) as client:
        for i in range(0, len(texts), BATCH):
            chunk = texts[i:i + BATCH]
            for attempt in range(3):
                try:
                    translated.extend(_translate_batch(client, chunk))
                    break
                except Exception as e:
                    log.warning("batch %d attempt %d failed: %s",
                                i // BATCH + 1, attempt + 1, e)
            else:
                translated.extend(chunk)   # keep English on hard failure
            log.info("translated %d/%d cues", min(i + BATCH, len(texts)),
                     len(texts))

    new = pysubs2.SSAFile()
    for ev, fa in zip(events, translated):
        new.events.append(pysubs2.SSAEvent(
            start=ev.start, end=ev.end,
            text=fa.replace("\n", "\\N")))
    new.save(out_path, format_="vtt")
    return out_path


def fetch_english_srt(title: str, year: int | None,
                      episode: tuple | None = None) -> str | None:
    """Find an English SRT for the title (subf2m), return extracted text
    file path. Mirrors subs.py's Persian flow but for the english tag."""
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
            srts = [n for n in zf.namelist()
                    if n.lower().endswith(".srt")]
            if episode:
                s, e = episode
                pat = re.compile(rf"s0?{s}\s*e0?{e}", re.IGNORECASE)
                matched = [n for n in srts
                           if pat.search(n.replace(" ", ""))]
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
    src, dst = sys.argv[1], sys.argv[2]
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
    print("wrote:", translate_srt_to_vtt(open(src, encoding="utf-8").read(),
                                         dst, max_cues=limit))
