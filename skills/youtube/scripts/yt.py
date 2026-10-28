#!/usr/bin/env python3
"""Fetch YouTube transcripts, comments and video details into a local library.

Run it through the `yt` wrapper next to this file, which builds and uses a private
Python environment. yt-dlp finds videos and reads details/comments; youtube-transcript-api
reads captions (yt-dlp's own caption download needs a PO token YouTube now demands).

All state lives in $YT_HOME (default ~/.youtube-reader): the environment, the library
and the watchlists. Nothing is written next to the code, so plugin updates keep the library.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import html
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, urlparse

from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    YouTubeTranscriptApi,
)

YT_HOME = Path(os.environ.get("YT_HOME", "~/.youtube-reader")).expanduser()
LIBRARY = Path(os.environ.get("YT_LIBRARY", YT_HOME / "library")).expanduser()
WATCHLISTS = YT_HOME / "watchlists.json"
REQUIREMENTS = Path(__file__).with_name("requirements.txt")


def proxy_settings() -> tuple[object | None, str | None]:
    """Optional proxy from the environment: (youtube-transcript-api config, yt-dlp proxy URL).

    Only needed away from a home connection, because YouTube blocks cloud IPs.
    YT_WEBSHARE_USER + YT_WEBSHARE_PASS use Webshare rotating residential proxies;
    YT_PROXY=http://user:pass@host:port uses any other proxy for both tools.
    """
    user, password = os.environ.get("YT_WEBSHARE_USER"), os.environ.get("YT_WEBSHARE_PASS")
    if user and password:
        from youtube_transcript_api.proxies import WebshareProxyConfig

        rotating = user if user.endswith("-rotate") else f"{user}-rotate"
        url = f"http://{quote(rotating, safe='')}:{quote(password, safe='')}@p.webshare.io:80"
        return WebshareProxyConfig(user, password), url
    url = os.environ.get("YT_PROXY")
    if url:
        from youtube_transcript_api.proxies import GenericProxyConfig

        return GenericProxyConfig(http_url=url, https_url=url), url
    return None, None


PROXY_CONFIG, PROXY_URL = proxy_settings()
# --ignore-config: a user's global yt-dlp config (output templates, cookies, proxies) must not change what we parse.
YTDLP = [sys.executable, "-m", "yt_dlp", "--ignore-config"]
# The proxy reaches yt-dlp through its environment, not argv, so credentials never show up in `ps`.
YTDLP_ENV = (
    {**os.environ, "HTTP_PROXY": PROXY_URL, "HTTPS_PROXY": PROXY_URL, "http_proxy": PROXY_URL, "https_proxy": PROXY_URL}
    if PROXY_URL
    else None
)


def redact(text: str) -> str:
    """Hide user:password@ in any URL before it is printed or stored."""
    return CREDENTIAL_RE.sub("://***@", text)


def utc_today() -> date:
    """YouTube's upload dates are UTC, so date cutoffs are too."""
    return datetime.now(timezone.utc).date()


PREFERRED_LANGS = ["en", "en-US", "en-GB", "en-CA", "en-AU"]
PARAGRAPH_SECONDS = 30
# Pacing, to stay under YouTube's rate limits. Pauses are randomized so requests don't arrive on a beat.
PAUSE_RANGE = (2.5, 5.0)  # seconds between videos
BLOCK_WAIT = 60  # seconds to wait before one retry when YouTube pushes back
DEFAULT_COMMENTS = 100
TOKENS_PER_WORD = 1.35

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID_RE = re.compile(r"^(?:PL|UU|LL|FL|RD|OL)[A-Za-z0-9_-]{10,}$")
CREDENTIAL_RE = re.compile(r"://[^/@\s]+@")
NOISE_RE = re.compile(r"\[(?:music|applause|laughter|__)\]|♪", re.IGNORECASE)
YTDLP_ERROR_RE = re.compile(r"ERROR: \[youtube\] ([A-Za-z0-9_-]{11}): (.*)")


class Blocked(Exception):
    """YouTube is refusing requests from this connection; stop instead of hammering it.

    `partial` carries the results collected before the block so they can still be reported.
    """

    def __init__(self, msg: str, partial: list | None = None):
        super().__init__(msg)
        self.partial = partial or []


@dataclass
class Result:
    id: str
    status: str  # new | cached | comments-added | no-captions | error
    meta: dict | None = None
    note: str = ""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- parsing input


def video_id_from(s: str) -> str | None:
    s = s.strip()
    if ID_RE.match(s):
        return s
    u = urlparse(s if "://" in s else "https://" + s)
    host = (u.hostname or "").lower()
    if host.endswith("youtu.be"):
        cand = u.path.lstrip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        if u.path == "/watch":
            cand = parse_qs(u.query).get("v", [""])[0]
        else:
            m = re.match(r"^/(?:shorts|live|embed|v)/([^/?#]+)", u.path)
            cand = m.group(1) if m else ""
    else:
        return None
    return cand if ID_RE.match(cand or "") else None


def classify(source: str) -> tuple[str, str]:
    source = source.strip()
    if source.startswith("search:"):
        return "search", source[len("search:") :].strip()
    if source.startswith("playlist:"):
        return "playlist", source[len("playlist:") :].strip()
    if PLAYLIST_ID_RE.match(source):
        return "playlist", source
    if video_id_from(source):
        return "video", source
    if "list=" in source:
        return "playlist", source
    return "channel", source


def channel_url(s: str) -> str:
    s = s.strip()
    if re.match(r"^UC[A-Za-z0-9_-]{22}$", s):
        base = f"https://www.youtube.com/channel/{s}"
    elif "/" not in s:  # a handle, with or without its @
        base = "https://www.youtube.com/" + (s if s.startswith("@") else "@" + s)
    else:
        base = (s if "://" in s else "https://" + s).split("?")[0]
    base = re.sub(r"/(featured|about|community|playlists|posts|live)/?$", "", base.rstrip("/"))
    if not re.search(r"/(videos|streams|shorts|podcasts)$", base):
        base += "/videos"
    return base


def search_url(query: str, sort: str, days: int | None) -> str:
    # `sp` is YouTube's base64 protobuf for search options: field 1 = sort,
    # field 2 = filters (1: upload-date window, 2: result type; type 1 = videos only).
    sp = bytearray()
    if sort == "date":
        sp += b"\x08\x02"
    elif sort == "views":
        sp += b"\x08\x03"
    filters = bytearray()
    if days:
        window = 2 if days <= 1 else 3 if days <= 7 else 4 if days <= 31 else 5 if days <= 366 else None
        if window:
            filters += bytes([0x08, window])
    filters += b"\x10\x01"
    sp += bytes([0x12, len(filters)]) + filters
    sp_param = quote(base64.b64encode(bytes(sp)).decode(), safe="")
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={sp_param}"


# ---------------------------------------------------------------- yt-dlp


def list_entries(url: str, limit: int) -> tuple[list[dict], str]:
    """Flat-list a channel/playlist/search. Returns (entries, error message)."""
    cmd = [*YTDLP, "--flat-playlist", "-J", "--playlist-end", str(limit), "--", url]
    p = subprocess.run(cmd, capture_output=True, text=True, check=False, env=YTDLP_ENV)
    try:
        data = json.loads(p.stdout) if p.stdout.strip() else None
    except json.JSONDecodeError:
        data = None
    if not data:
        problems = [l.split(":", 1)[-1].strip() for l in p.stderr.splitlines() if "ERROR" in l or "said:" in l]
        return [], redact(problems[-1]) if problems else "nothing found"
    return [e for e in (data.get("entries") or []) if e and e.get("id")], ""


def video_entries(entries: list[dict], shorts: bool) -> list[str]:
    ids = []
    for e in entries:
        if e.get("ie_key") not in (None, "Youtube"):
            continue  # channels / playlists mixed into search results
        if e.get("live_status") in ("is_upcoming", "is_live"):
            continue
        if not shorts and "/shorts/" in (e.get("url") or ""):
            continue
        ids.append(e["id"])
    return ids


def stream_details(ids: list[str], comments: int, cutoff: date | None, newest_first: bool, on_info) -> dict[str, str]:
    """Run one yt-dlp process over `ids`, calling on_info(info) as each video's details arrive.

    Returns {video_id: error message} for videos yt-dlp could not read.
    """
    cmd = [
        *YTDLP,
        "-j",
        "--skip-download",
        "--ignore-no-formats-error",
        "--ignore-errors",
        "--no-warnings",
        "--sleep-requests",
        "1",
        "--retry-sleep",
        "extractor:exp=5:60",
    ]
    if comments:
        cmd += [
            "--get-comments",
            "--extractor-args",
            f"youtube:max_comments={comments},{comments},0,0;comment_sort=top",
        ]
    if cutoff:
        # Channels list newest first, so the first too-old video means the rest are too.
        epoch = int(datetime.combine(cutoff, datetime.min.time()).timestamp())
        cmd += ["--break-match-filters" if newest_first else "--match-filters", f"timestamp>=?{epoch}"]
    cmd += ["--"] + [f"https://www.youtube.com/watch?v={i}" for i in ids]

    with tempfile.TemporaryFile("w+") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True, env=YTDLP_ENV)
        try:
            for line in proc.stdout:
                if line.startswith("{"):
                    on_info(json.loads(line))
        except BaseException:
            proc.terminate()
            raise
        finally:
            proc.wait()
        errf.seek(0)
        errors = {}
        for line in errf:
            m = YTDLP_ERROR_RE.match(line.strip())
            if m:
                errors[m.group(1)] = redact(m.group(2))
        return errors


# ---------------------------------------------------------------- transcripts


def fetch_transcript(api: YouTubeTranscriptApi, vid: str) -> tuple[list, str]:
    """Best English track: manual, then auto-generated, then a translation of whatever exists."""
    tracks = api.list(vid)
    try:
        t = tracks.find_manually_created_transcript(PREFERRED_LANGS)
        return list(t.fetch()), f"manual ({t.language_code})"
    except NoTranscriptFound:
        pass
    try:
        t = tracks.find_generated_transcript(PREFERRED_LANGS)
        return list(t.fetch()), f"auto ({t.language_code})"
    except NoTranscriptFound:
        pass
    available = list(tracks)
    if not available:
        tracks.find_transcript(PREFERRED_LANGS)  # raises NoTranscriptFound with YouTube's details
    base = next((t for t in available if not t.is_generated), available[0])
    kind = "auto" if base.is_generated else "manual"
    if base.is_translatable and any(l.language_code == "en" for l in base.translation_languages):
        try:
            return list(base.translate("en").fetch()), f"{kind} ({base.language_code} → en)"
        except CouldNotRetrieveTranscript as e:
            if isinstance(e, (IpBlocked, RequestBlocked)):
                raise
    return list(base.fetch()), f"{kind} ({base.language_code})"


def fetch_with_retry(api: YouTubeTranscriptApi, vid: str) -> tuple[list, str]:
    """fetch_transcript, but when YouTube pushes back, wait a minute and try once more before giving up."""
    try:
        return fetch_transcript(api, vid)
    except (IpBlocked, RequestBlocked):
        log(f"  YouTube is pushing back; waiting {BLOCK_WAIT} seconds before one more try…")
        time.sleep(BLOCK_WAIT)
        return fetch_transcript(api, vid)


def pause() -> None:
    time.sleep(random.uniform(*PAUSE_RANGE))


@contextmanager
def fetch_lock():
    """One fetch at a time across every process, so several Claude sessions don't hit YouTube at once."""
    YT_HOME.mkdir(parents=True, exist_ok=True)
    with open(YT_HOME / ".fetch.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Another fetch is running (probably another Claude session); waiting for it to finish…")
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def paragraphs(snippets: list) -> list[tuple[float, str]]:
    paras, cur, start = [], [], None
    for s in snippets:
        text = re.sub(r"\s+", " ", NOISE_RE.sub("", html.unescape(s.text))).strip()
        if not text:
            continue
        if start is None:
            start = s.start
        elif s.start - start >= PARAGRAPH_SECONDS and cur:
            paras.append((start, " ".join(cur)))
            cur, start = [], s.start
        cur.append(text)
    if cur:
        paras.append((start, " ".join(cur)))
    return paras


# ---------------------------------------------------------------- library files


def slug(s: str | None, n: int = 60) -> str:
    """Filename-safe form of a title or channel name: keeps non-Latin letters, drops accents."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^\w]+|_+", "-", s).strip("-")
    return (s[:n].rstrip("-")) or "untitled"


def clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def iso_date(yyyymmdd: str | None) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}" if yyyymmdd else "unknown-date"


def library_index() -> dict[str, Path]:
    if not LIBRARY.exists():
        return {}
    return {p.stem[-11:]: p for p in LIBRARY.glob("*/*.md") if not p.name.endswith(".comments.md")}


def comments_path(path: Path) -> Path:
    return path.with_name(path.stem + ".comments.md")


def write_atomic(path: Path, text: str) -> None:
    """Write through a temp file in the same directory, so an interrupted run never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_meta(path: Path) -> dict:
    meta: dict = {}
    with path.open(encoding="utf-8") as f:
        if f.readline().strip() != "---":
            return meta
        for line in f:
            line = line.rstrip("\n")
            if line == "---":
                break
            key, _, value = line.partition(": ")
            try:
                meta[key] = json.loads(value)
            except json.JSONDecodeError:
                meta[key] = value
    meta["path"] = path
    return meta


def front_matter(fields: dict) -> str:
    lines = ["---"]
    for k, v in fields.items():
        if v is None:
            continue
        plain = isinstance(v, int) and not isinstance(v, bool)
        lines.append(f"{k}: {v if plain else json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def write_video(info: dict, paras: list[tuple[float, str]], captions: str, old_path: Path | None) -> Path:
    vid = info["id"]
    channel = info.get("channel") or info.get("uploader") or "unknown"
    day = iso_date(info.get("upload_date"))
    url = f"https://www.youtube.com/watch?v={vid}"
    words = sum(len(t.split()) for _, t in paras)
    folder = slug(channel, 40)
    if folder == "untitled":  # a name with no letters at all; fall back to the handle or channel id
        folder = slug(info.get("uploader_id") or info.get("channel_id") or "unknown-channel", 40)
    path = LIBRARY / folder / f"{day}_{slug(info.get('title'))}_{vid}.md"

    header = front_matter(
        {
            "id": vid,
            "title": info.get("title"),
            "channel": channel,
            "handle": info.get("uploader_id"),
            "url": url,
            "upload_date": day,
            "duration": clock(info.get("duration")),
            "duration_seconds": int(info["duration"]) if info.get("duration") else None,
            "views": info.get("view_count"),
            "likes": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "captions": captions,
            "words": words,
            "fetched": date.today().isoformat(),
        }
    )
    stats = [
        f"{channel} ({info['uploader_id']})" if info.get("uploader_id") else channel,
        day,
        clock(info.get("duration")),
    ]
    if info.get("view_count") is not None:
        stats.append(f"{info['view_count']:,} views")
    body = [header, "", f"# {info.get('title')}", "", " · ".join(stats), url, ""]
    if info.get("chapters"):
        body += ["## Chapters", ""]
        body += [f"- [{clock(c.get('start_time'))}] {c.get('title')}" for c in info["chapters"]]
        body.append("")
    description = (info.get("description") or "").strip()
    if description:
        if len(description) > 2500:
            description = description[:2500].rstrip() + " …"
        body += ["## Description", "", description, ""]
    body += ["## Transcript", "", f"Captions: {captions}. Jump to a moment with {url}&t=<seconds>s", ""]
    body += [f"[{clock(start)}] {text}\n" for start, text in paras]

    if old_path and old_path != path:
        old_path.unlink(missing_ok=True)
        old_comments = comments_path(old_path)
        if old_comments.exists():
            old_comments.rename(comments_path(path))
    write_atomic(path, "\n".join(body))
    return path


def write_comments(path: Path, info: dict) -> int:
    top = [c for c in (info.get("comments") or []) if c.get("parent", "root") == "root"]
    lines = [
        front_matter(
            {
                "id": info["id"],
                "title": info.get("title"),
                "comments_saved": len(top),
                "comment_count": info.get("comment_count"),
                "sort": "top",
                "fetched": date.today().isoformat(),
            }
        ),
        "",
        f"# Top comments: {info.get('title')}",
        "",
    ]
    for c in top:
        tags = [f"{c.get('like_count') or 0:,} likes"]
        if c.get("is_pinned"):
            tags.append("pinned")
        if c.get("author_is_uploader"):
            tags.append("creator")
        text = re.sub(r"\s+", " ", c.get("text") or "").strip()
        lines.append(f"- **{c.get('author') or 'unknown'}** ({', '.join(tags)}): {text}")
    write_atomic(comments_path(path), "\n".join(lines) + "\n")
    return len(top)


# ---------------------------------------------------------------- collecting


def collect(ids: list[str], *, comments: int, days: int | None, newest_first: bool, refresh: bool) -> list[Result]:
    index = library_index()
    cutoff = utc_today() - timedelta(days=days) if days else None
    cutoff_str = cutoff.isoformat() if cutoff else None
    results: list[Result] = []
    to_fetch: list[str] = []

    for vid in dict.fromkeys(ids):
        path = index.get(vid)
        if path and not refresh:
            meta = read_meta(path)
            if cutoff_str and str(meta.get("upload_date", "")) < cutoff_str:
                if newest_first:
                    break
                continue
            if comments and not comments_path(path).exists():
                to_fetch.append(vid)
            else:
                results.append(Result(vid, "cached", meta))
        else:
            to_fetch.append(vid)

    if not to_fetch:
        return results

    api = YouTubeTranscriptApi(proxy_config=PROXY_CONFIG)
    seen: set[str] = set()

    def on_info(info: dict) -> None:
        vid = info["id"]
        seen.add(vid)
        try:
            handle(info)
        except Blocked:
            raise
        except Exception as e:  # noqa: BLE001  (deliberate: one bad video must not lose the rest of the run)
            where = {"title": info.get("title"), "channel": info.get("channel")}
            results.append(Result(vid, "error", where, redact(f"{type(e).__name__}: {e}".splitlines()[0])))

    def handle(info: dict) -> None:
        vid = info["id"]
        if cutoff and info.get("upload_date") and info["upload_date"] < cutoff.strftime("%Y%m%d"):
            return
        existing = index.get(vid)
        if existing and not refresh:
            n = write_comments(existing, info)
            results.append(Result(vid, "comments-added", read_meta(existing), f"{n} comments"))
            return
        log(f"  [{len(seen)}/{len(to_fetch)}] {info.get('title')}")
        try:
            snippets, captions = fetch_with_retry(api, vid)
        except (IpBlocked, RequestBlocked) as e:
            raise Blocked(type(e).__name__, results) from e
        except CouldNotRetrieveTranscript as e:
            reason = type(e).__name__
            results.append(
                Result(vid, "no-captions", {"title": info.get("title"), "channel": info.get("channel")}, reason)
            )
            return
        path = write_video(info, paragraphs(snippets), captions, existing)
        note = ""
        if comments:
            note = f"{write_comments(path, info)} comments"
        results.append(Result(vid, "new", read_meta(path), note))
        pause()

    log(f"  reading details for {len(to_fetch)} video(s)…")
    errors = stream_details(to_fetch, comments, cutoff, newest_first, on_info)
    for vid, msg in errors.items():
        results.append(Result(vid, "error", None, msg))
    if any("not a bot" in m or "429" in m for m in errors.values()):
        raise Blocked("YouTube asked to confirm this isn't a bot", results)
    order = {v: i for i, v in enumerate(ids)}
    results.sort(key=lambda r: order.get(r.id, len(order)))
    return results


def gather(kind: str, value: str, a: argparse.Namespace) -> tuple[list[str], bool, str]:
    """Turn a channel/playlist/search source into video IDs. Returns (ids, newest_first, error)."""
    if kind == "channel":
        if re.search(r"\s", value):
            return [], True, "channel handles have no spaces; use the @name from the channel's URL"
        limit = a.limit or 30
        entries, err = list_entries(channel_url(value), limit)
        if err:
            return [], True, f"channel not found or empty ({err}); check the handle"
        return video_entries(entries, a.shorts), True, ""
    if kind == "playlist":
        if PLAYLIST_ID_RE.match(value):
            url = f"https://www.youtube.com/playlist?list={value}"
        elif "list=" in value:
            u = urlparse(value if "://" in value else "https://" + value)
            if not (u.hostname or "").endswith(("youtube.com", "youtu.be")):
                return [], False, "not a YouTube playlist URL"
            url = u.geturl()
        else:
            return [], False, "not a playlist: give a youtube.com URL with list=… or the playlist ID"
        entries, err = list_entries(url, a.limit or 50)
        return video_entries(entries, a.shorts), False, err
    if kind == "search":
        if a.days and a.days > 366:
            log(
                "  YouTube's search filter only goes back a year, so results older than that are dropped after the search"
            )
        limit = a.limit or 15
        entries, err = list_entries(search_url(value, getattr(a, "sort", "relevance"), a.days), limit + 10)
        return video_entries(entries, a.shorts)[:limit], False, err
    raise ValueError(kind)


def run_sources(sources: list[str], a: argparse.Namespace) -> list[Result]:
    results: list[Result] = []
    videos: list[str] = []
    for src in sources:
        kind, value = classify(src)
        if kind == "video":
            videos.append(video_id_from(value))
            continue
        log(f"{kind}: {value}")
        ids, newest_first, err = gather(kind, value, a)
        if err and not ids:
            results.append(Result(src, "error", None, err))
            continue
        log(f"  {len(ids)} candidate video(s)")
        try:
            results += collect(ids, comments=a.comments, days=a.days, newest_first=newest_first, refresh=a.refresh)
        except Blocked as e:
            e.partial = results + e.partial
            raise
    if videos:
        log(f"videos: {len(videos)}")
        try:
            results += collect(videos, comments=a.comments, days=None, newest_first=False, refresh=a.refresh)
        except Blocked as e:
            e.partial = results + e.partial
            raise
    return results


# ---------------------------------------------------------------- output


def print_results(results: list[Result]) -> None:
    unique = list({r.id: r for r in results}.values())
    ok = [r for r in unique if r.status in ("new", "cached", "comments-added")]
    bad = [r for r in unique if r not in ok]
    for r in ok:
        m = r.meta
        extra = f", {r.note}" if r.note else ""
        has_comments = " +comments" if comments_path(m["path"]).exists() else ""
        print(
            f"- {m.get('upload_date')} | {m.get('channel')} | {m.get('title')} | {m.get('duration')} | "
            f"{m.get('words', 0):,} words | {r.status}{extra}{has_comments}"
        )
        print(f"  {m['path']}")
    if bad:
        print("\nSkipped:")
        for r in bad:
            title = (r.meta or {}).get("title")
            label = f"{title} ({r.id})" if title else r.id
            print(f"- {label}: {r.status}{' — ' + r.note if r.note else ''}")
    words = sum((r.meta or {}).get("words", 0) for r in ok)
    new = sum(r.status == "new" for r in ok)
    print(f"\nTotal: {len(ok)} video(s) ({new} new), {words:,} words (~{int(words * TOKENS_PER_WORD):,} tokens)")


# ---------------------------------------------------------------- commands


def load_watchlists() -> dict[str, list[str]]:
    if not WATCHLISTS.exists():
        return {}
    try:
        return json.loads(WATCHLISTS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"{WATCHLISTS} isn't valid JSON ({e}); fix or delete it") from e


def save_watchlists(w: dict[str, list[str]]) -> None:
    write_atomic(WATCHLISTS, json.dumps(dict(sorted(w.items())), indent=2) + "\n")


def fetch_and_report(sources: list[str], a: argparse.Namespace) -> int:
    if PROXY_URL:
        log("using the proxy from the environment")
    try:
        with fetch_lock():
            results = run_sources(sources, a)
    except Blocked as e:
        if e.partial:
            print_results(e.partial)
            print()
        print(f"STOPPED: YouTube is refusing requests from this connection ({redact(str(e))}).")
        print("On a home connection that's a rate limit: wait about an hour before any more YouTube requests,")
        print("including opening YouTube in a browser on this connection, which keeps the block going.")
        print("On a cloud server or VPN, waiting won't help; set a residential proxy (see the README).")
        return 2
    print_results(results)
    return 0


def cmd_get(a):
    return fetch_and_report(a.targets, a)


def cmd_search(a):
    return fetch_and_report(["search:" + a.query], a)


def cmd_watch(a):
    w = load_watchlists()
    if a.name not in w:
        print(f"No watchlist '{a.name}'. Existing: {', '.join(w) or 'none'}")
        return 1
    return fetch_and_report(w[a.name], a)


def cmd_watchlist(a):
    w = load_watchlists()
    if a.action == "list":
        if not w:
            print("No watchlists yet. Add one: yt watchlist add <name> @channel search:'query' …")
        for name, sources in w.items():
            print(f"{name} ({len(sources)}): {', '.join(sources)}")
    elif a.action == "show":
        if a.name not in w:
            print(f"No watchlist '{a.name}'")
            return 1
        print("\n".join(w[a.name]))
    elif a.action == "add":
        w[a.name] = list(dict.fromkeys(w.get(a.name, []) + a.sources))
        save_watchlists(w)
        print(f"{a.name}: {', '.join(w[a.name])}")
    elif a.action == "remove":
        if a.name not in w:
            print(f"No watchlist '{a.name}'")
            return 1
        if a.sources:
            w[a.name] = [s for s in w[a.name] if s not in a.sources]
        else:
            del w[a.name]
        save_watchlists(w)
        print(f"{a.name}: {', '.join(w[a.name]) or '(empty)'}" if a.name in w else f"{a.name}: removed")
    return 0


def cmd_ls(a):
    metas = [read_meta(p) for p in library_index().values()]
    if a.days:
        cutoff = (utc_today() - timedelta(days=a.days)).isoformat()
        metas = [m for m in metas if str(m.get("upload_date", "")) >= cutoff]
    if a.channel:
        metas = [m for m in metas if a.channel.lower() in f"{m.get('channel')} {m.get('handle')}".lower()]
    if a.title:
        metas = [m for m in metas if a.title.lower() in str(m.get("title", "")).lower()]
    metas.sort(key=lambda m: str(m.get("upload_date", "")), reverse=True)
    print(f"Library: {LIBRARY} ({len(metas)} matching video(s))")
    for m in metas[: a.limit]:
        print(f"- {m.get('upload_date')} | {m.get('channel')} | {m.get('title')} | {m.get('words', 0):,} words")
        print(f"  {m['path']}")
    if len(metas) > a.limit:
        print(f"… {len(metas) - a.limit} more (use --limit)")
    return 0


def cmd_update(a):
    pip = [sys.executable, "-m", "pip", "install", "-q", "-U", "-r", str(REQUIREMENTS)]
    with fetch_lock():  # don't swap packages out from under a fetch in another session
        code = subprocess.call(pip)
    subprocess.call([*YTDLP, "--version"])
    return code


def positive_int(s: str) -> int:
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="yt", description="Save YouTube transcripts, details and comments to a local library."
    )
    sub = p.add_subparsers(dest="cmd", metavar="<command>", title="commands", required=True)

    def fetch_flags(sp, limit_help):
        sp.add_argument("--days", type=positive_int, metavar="N", help="only videos uploaded in the last N days")
        sp.add_argument("--limit", type=positive_int, metavar="N", help=limit_help)
        sp.add_argument(
            "--comments",
            type=int,
            nargs="?",
            const=DEFAULT_COMMENTS,
            default=0,
            metavar="N",
            help=f"also save the top N comments (default {DEFAULT_COMMENTS})",
        )
        sp.add_argument("--refresh", action="store_true", help="re-download even if already in the library")
        sp.add_argument(
            "--shorts",
            action="store_true",
            help="keep Shorts in search results (for a channel's Shorts, use its /shorts URL)",
        )

    g = sub.add_parser(
        "get",
        help="fetch videos, channels (@handle), playlists or search:<query>",
        description="Fetch videos, channels, playlists or searches into the library.",
    )
    g.add_argument(
        "targets", nargs="+", help="video URLs or IDs, @handles, channel or playlist URLs, or search:<query>"
    )
    fetch_flags(g, "max videos per channel (30) or playlist (50)")
    g.set_defaults(func=cmd_get, sort="relevance")

    s = sub.add_parser(
        "search", help="search YouTube and fetch the results", description="Search YouTube and fetch the results."
    )
    s.add_argument("query", help="what to search for")
    s.add_argument(
        "--sort", choices=["relevance", "date", "views"], default="relevance", help="result order (relevance)"
    )
    fetch_flags(s, "number of results (15)")
    s.set_defaults(func=cmd_search)

    w = sub.add_parser(
        "watch",
        help="fetch every source in a saved watchlist",
        description="Fetch every source in a saved watchlist.",
    )
    w.add_argument("name", help="the watchlist to fetch")
    w.add_argument(
        "--sort",
        choices=["relevance", "date", "views"],
        default="relevance",
        help="result order for search sources (relevance)",
    )
    fetch_flags(w, "max videos per source (30 per channel, 50 per playlist, 15 per search)")
    w.set_defaults(func=cmd_watch)

    wl = sub.add_parser(
        "watchlist",
        help="manage saved watchlists",
        description=(
            "list shows every watchlist; show prints one; add creates a watchlist or adds sources to it; "
            "remove takes out the sources you name, or deletes the whole watchlist if you name none."
        ),
    )
    wl.add_argument("action", choices=["list", "show", "add", "remove"], help="what to do")
    wl.add_argument("name", nargs="?", help="watchlist name")
    wl.add_argument("sources", nargs="*", help="@handle, channel/playlist/video URL, or search:<query>")
    wl.set_defaults(func=cmd_watchlist)

    l = sub.add_parser(
        "ls", help="list what's already in the library", description="List videos already in the library, newest first."
    )
    l.add_argument("--days", type=positive_int, metavar="N", help="only videos uploaded in the last N days")
    l.add_argument("--channel", help="only channels whose name or handle contains this")
    l.add_argument("--title", help="only titles containing this")
    l.add_argument("--limit", type=positive_int, metavar="N", default=50, help="how many to show (50)")
    l.set_defaults(func=cmd_ls)

    u = sub.add_parser(
        "update",
        help="upgrade yt-dlp and youtube-transcript-api",
        description="Upgrade yt-dlp and youtube-transcript-api. Try this first when YouTube changes something.",
    )
    u.set_defaults(func=cmd_update)

    a = p.parse_args(argv)
    if a.cmd == "watchlist" and a.action != "list" and not a.name:
        p.error(f"watchlist {a.action} needs a name")
    if a.cmd == "watchlist" and a.action == "add" and not a.sources:
        p.error("watchlist add needs at least one source")
    try:
        return a.func(a)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
