"""Unit tests for the pure parts of yt.py: parsing, paragraphing, files, settings.

Code that talks to YouTube is stubbed out here and checked by running the tool.
Run with the tool's own environment: ~/.youtube-reader/venv/bin/python -m pytest
"""

import base64
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "youtube" / "scripts" / "yt.py"


@pytest.fixture(scope="module")
def yt():
    spec = importlib.util.spec_from_file_location("yt", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["yt"] = mod  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- input parsing


@pytest.mark.parametrize(
    "given, want",
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=60s", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abcDEF123456", "dQw4w9WgXcQ"),
        ("youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ?feature=share", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/@example", None),
        ("https://vimeo.com/12345", None),
        ("not a url", None),
        ("https://www.youtube.com/watch?v=tooshort", None),
    ],
)
def test_video_id_from(yt, given, want):
    assert yt.video_id_from(given) == want


@pytest.mark.parametrize(
    "given, want",
    [
        ("search:ai agents", ("search", "ai agents")),
        ("playlist:PL123", ("playlist", "PL123")),
        ("https://www.youtube.com/playlist?list=PL123", ("playlist", "https://www.youtube.com/playlist?list=PL123")),
        ("@Fireship", ("channel", "@Fireship")),
        ("PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf", ("playlist", "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf")),
        ("https://youtu.be/dQw4w9WgXcQ", ("video", "https://youtu.be/dQw4w9WgXcQ")),
    ],
)
def test_classify(yt, given, want):
    assert yt.classify(given) == want


@pytest.mark.parametrize(
    "given, want",
    [
        ("@Fireship", "https://www.youtube.com/@Fireship/videos"),
        ("Fireship", "https://www.youtube.com/@Fireship/videos"),
        ("UC" + "x" * 22, "https://www.youtube.com/channel/UC" + "x" * 22 + "/videos"),
        ("https://www.youtube.com/@Fireship/", "https://www.youtube.com/@Fireship/videos"),
        ("https://www.youtube.com/@Fireship/streams", "https://www.youtube.com/@Fireship/streams"),
        ("youtube.com/@Fireship?foo=1", "https://youtube.com/@Fireship/videos"),
        ("https://www.youtube.com/@Fireship/featured", "https://www.youtube.com/@Fireship/videos"),
        ("https://www.youtube.com/@Fireship/about/", "https://www.youtube.com/@Fireship/videos"),
        ("some.handle", "https://www.youtube.com/@some.handle/videos"),
    ],
)
def test_channel_url(yt, given, want):
    assert yt.channel_url(given) == want


def test_search_url_encodes_query_and_filters(yt):
    url = yt.search_url("claude code", "relevance", None)
    assert url.startswith("https://www.youtube.com/results?search_query=claude+code&sp=")
    sp = parse_qs(urlparse(url).query)["sp"][0]
    assert b"\x10\x01" in base64.b64decode(sp)  # videos-only filter is always on
    assert yt.search_url("x", "date", 7) != yt.search_url("x", "relevance", 7)
    assert yt.search_url("x", "relevance", 7) != yt.search_url("x", "relevance", 30)


def test_video_entries_filters_shorts_live_and_non_videos(yt):
    entries = [
        {"id": "a", "ie_key": "Youtube", "url": "https://www.youtube.com/watch?v=a"},
        {"id": "b", "ie_key": "Youtube", "url": "https://www.youtube.com/shorts/b"},
        {"id": "c", "ie_key": "YoutubeTab", "url": "https://www.youtube.com/@x"},
        {"id": "d", "ie_key": "Youtube", "url": "https://www.youtube.com/watch?v=d", "live_status": "is_live"},
        {"id": "e", "url": "https://www.youtube.com/watch?v=e"},
    ]
    assert yt.video_entries(entries, shorts=False) == ["a", "e"]
    assert yt.video_entries(entries, shorts=True) == ["a", "b", "e"]


# ---------------------------------------------------------------- transcripts


def snip(text, start):
    return SimpleNamespace(text=text, start=start, duration=2.0)


def test_paragraphs_split_every_30_seconds_and_strip_noise(yt):
    snippets = [
        snip("hello &amp; welcome", 0.0),
        snip("[Music]", 5.0),
        snip("second", 20.0),
        snip("third", 31.0),
        snip("fourth ♪", 45.0),
        snip("fifth", 62.0),
    ]
    assert yt.paragraphs(snippets) == [
        (0.0, "hello & welcome second"),
        (31.0, "third fourth"),
        (62.0, "fifth"),
    ]


def test_paragraphs_empty(yt):
    assert yt.paragraphs([]) == []
    assert yt.paragraphs([snip("[Applause]", 0.0)]) == []


# ---------------------------------------------------------------- library files


def test_slug(yt):
    assert yt.slug("Hello, World!! — “Quoted Title”") == "hello-world-quoted-title"
    assert yt.slug("Café Müller") == "cafe-muller"
    assert yt.slug("東京のチャンネル") == "東京のチャンネル"
    assert yt.slug("Вести недели") == "вести-недели"
    assert yt.slug("a_b  c") == "a-b-c"
    assert yt.slug("../../etc") == "etc"
    assert yt.slug("") == "untitled"
    assert yt.slug(None) == "untitled"
    assert yt.slug("a" * 100, 10) == "a" * 10
    assert yt.slug("abc-def-ghi", 8) == "abc-def"


def test_clock_and_iso_date(yt):
    assert yt.clock(65) == "1:05"
    assert yt.clock(3661) == "1:01:01"
    assert yt.clock(0) == "0:00"
    assert yt.clock(59.9) == "0:59"
    assert yt.clock(None) == "?"
    assert yt.iso_date("20261014") == "2026-10-14"
    assert yt.iso_date(None) == "unknown-date"


def test_front_matter_round_trips_through_read_meta(yt, tmp_path):
    fields = {"id": "abc", "title": 'He said "hi": ok', "views": 375051, "likes": None, "captions": "auto (en)"}
    text = yt.front_matter(fields)
    lines = text.splitlines()
    assert lines[0] == "---" and lines[-1] == "---"
    assert not any(line.startswith("likes") for line in lines)
    p = tmp_path / "v.md"
    p.write_text(text + "\n\n# body\n", encoding="utf-8")
    meta = yt.read_meta(p)
    assert meta["id"] == "abc"
    assert meta["title"] == 'He said "hi": ok'
    assert meta["views"] == 375051
    assert meta["captions"] == "auto (en)"
    assert meta["path"] == p


def test_read_meta_without_front_matter(yt, tmp_path):
    p = tmp_path / "plain.md"
    p.write_text("# no front matter\n", encoding="utf-8")
    assert yt.read_meta(p) == {}


def test_library_index_maps_video_ids_to_files(yt, tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "LIBRARY", tmp_path)
    ch = tmp_path / "example-channel"
    ch.mkdir()
    main = ch / "2026-10-14_title_dQw4w9WgXcQ.md"
    main.write_text("x")
    (ch / "2026-10-14_title_dQw4w9WgXcQ.comments.md").write_text("y")
    assert yt.library_index() == {"dQw4w9WgXcQ": main}
    assert yt.comments_path(main).name == "2026-10-14_title_dQw4w9WgXcQ.comments.md"
    monkeypatch.setattr(yt, "LIBRARY", tmp_path / "missing")
    assert yt.library_index() == {}


def test_watchlists_save_creates_parent_dir_and_sorts(yt, tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "WATCHLISTS", tmp_path / "home" / "watchlists.json")
    assert yt.load_watchlists() == {}
    yt.save_watchlists({"b": ["@x"], "a": ["search:q"]})
    assert list(yt.load_watchlists()) == ["a", "b"]


def test_print_results_summarizes(yt, tmp_path, capsys):
    p = tmp_path / "c" / "2026-10-14_t_dQw4w9WgXcQ.md"
    p.parent.mkdir()
    p.write_text("x")
    meta = {"upload_date": "2026-10-14", "channel": "C", "title": "T", "duration": "1:00", "words": 1000, "path": p}
    yt.print_results(
        [
            yt.Result("dQw4w9WgXcQ", "new", meta, "60 comments"),
            yt.Result("zzz", "no-captions", {"title": "Z"}, "NoTranscriptFound"),
        ]
    )
    out = capsys.readouterr().out
    assert "| new, 60 comments" in out
    assert "Skipped:" in out and "Z (zzz): no-captions — NoTranscriptFound" in out
    assert "Total: 1 video(s) (1 new), 1,000 words" in out


# ---------------------------------------------------------------- settings


def test_proxy_settings_off_by_default(yt, monkeypatch):
    for k in ("YT_WEBSHARE_USER", "YT_WEBSHARE_PASS", "YT_PROXY"):
        monkeypatch.delenv(k, raising=False)
    assert yt.proxy_settings() == (None, None)


def test_proxy_settings_generic(yt, monkeypatch):
    monkeypatch.delenv("YT_WEBSHARE_USER", raising=False)
    monkeypatch.setenv("YT_PROXY", "http://u:p@proxy.example:8080")
    cfg, url = yt.proxy_settings()
    assert url == "http://u:p@proxy.example:8080"
    assert type(cfg).__name__ == "GenericProxyConfig"


def test_proxy_settings_webshare(yt, monkeypatch):
    monkeypatch.delenv("YT_PROXY", raising=False)
    monkeypatch.setenv("YT_WEBSHARE_USER", "abc")
    monkeypatch.setenv("YT_WEBSHARE_PASS", "p@ss")
    cfg, url = yt.proxy_settings()
    assert type(cfg).__name__ == "WebshareProxyConfig"
    assert url == "http://abc-rotate:p%40ss@p.webshare.io:80"


# ---------------------------------------------------------------- the yt-dlp boundary


def test_list_entries_keeps_the_url_out_of_option_parsing(yt, monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return SimpleNamespace(stdout='{"entries": []}', stderr="")

    monkeypatch.setattr(yt.subprocess, "run", fake_run)
    yt.list_entries("https://www.youtube.com/playlist?list=PL123", 5)
    assert captured["cmd"][-2:] == ["--", "https://www.youtube.com/playlist?list=PL123"]
    assert "--ignore-config" in captured["cmd"]
    assert "--proxy" not in yt.YTDLP


def test_gather_rejects_values_that_are_not_playlists_or_handles(yt):
    a = SimpleNamespace(limit=None, shorts=False, days=None, sort="relevance")
    ids, _, err = yt.gather("playlist", "--config-location=/etc/passwd", a)
    assert ids == [] and "not a playlist" in err
    ids, _, err = yt.gather("playlist", "https://evil.example/?list=PL123", a)
    assert ids == [] and "not a YouTube playlist" in err
    ids, _, err = yt.gather("channel", "Some Person", a)
    assert ids == [] and "spaces" in err


def test_ytdlp_error_line_parsing(yt):
    m = yt.YTDLP_ERROR_RE.match("ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you’re not a bot. This helps.")
    assert m.group(1) == "dQw4w9WgXcQ"
    assert m.group(2).startswith("Sign in")


def test_redact_hides_credentials_in_urls(yt):
    assert (
        yt.redact("failed via http://user:s3cret@proxy.example:8080/x") == "failed via http://***@proxy.example:8080/x"
    )
    assert yt.redact("https://www.youtube.com/watch?v=abc") == "https://www.youtube.com/watch?v=abc"


# ---------------------------------------------------------------- files, again


def test_front_matter_writes_bools_as_json(yt):
    assert "flag: true" in yt.front_matter({"flag": True})
    assert "n: 3" in yt.front_matter({"n": 3})


def test_write_atomic_leaves_no_temp_file(yt, tmp_path):
    target = tmp_path / "deep" / "file.md"
    yt.write_atomic(target, "hello")
    assert target.read_text() == "hello"
    assert [p.name for p in target.parent.iterdir()] == ["file.md"]


def test_write_comments_keeps_top_level_only_and_tags(yt, tmp_path):
    path = tmp_path / "c" / "2026-10-01_t_dQw4w9WgXcQ.md"
    info = {
        "id": "dQw4w9WgXcQ",
        "title": "T",
        "comment_count": 3,
        "comments": [
            {
                "author": "creator",
                "text": "pinned  text",
                "like_count": 5,
                "parent": "root",
                "is_pinned": True,
                "author_is_uploader": True,
            },
            {"author": "fan", "text": "a reply", "parent": "xyz"},
            {"author": "other", "text": "plain", "like_count": None, "parent": "root"},
        ],
    }
    assert yt.write_comments(path, info) == 2
    text = yt.comments_path(path).read_text()
    assert "- **creator** (5 likes, pinned, creator): pinned text" in text
    assert "- **other** (0 likes): plain" in text
    assert "a reply" not in text


# ---------------------------------------------------------------- collect(), with YouTube stubbed out


@pytest.fixture
def fake_youtube(yt, tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "LIBRARY", tmp_path / "library")
    monkeypatch.setattr(yt, "TRANSCRIPT_PAUSE", 0)
    monkeypatch.setattr(yt, "YouTubeTranscriptApi", lambda proxy_config=None: object())
    videos = {
        "aaaaaaaaaaa": {
            "id": "aaaaaaaaaaa",
            "title": "First",
            "channel": "Example Channel",
            "uploader_id": "@example",
            "upload_date": "20261001",
            "duration": 65,
            "view_count": 10,
            "comments": [
                {"author": "x", "text": "nice", "like_count": 2, "parent": "root"},
                {"author": "y", "text": "reply", "parent": "abc"},
            ],
        },
        "bbbbbbbbbbb": {
            "id": "bbbbbbbbbbb",
            "title": "Second",
            "channel": "Example Channel",
            "uploader_id": "@example",
            "upload_date": "20261002",
            "duration": 30,
        },
    }

    def fake_stream(ids, comments, cutoff, newest_first, on_info):
        for vid in ids:
            if vid in videos:
                on_info(videos[vid])
        return {vid: "Video unavailable" for vid in ids if vid not in videos}

    calls = {"transcripts": 0}

    def fake_transcript(api, vid):
        calls["transcripts"] += 1
        return [snip(f"words for {vid}", 0.0)], "auto (en)"

    monkeypatch.setattr(yt, "stream_details", fake_stream)
    monkeypatch.setattr(yt, "fetch_transcript", fake_transcript)
    return SimpleNamespace(videos=videos, calls=calls)


def run_collect(yt, ids, **kw):
    opts = {"comments": 0, "days": None, "newest_first": False, "refresh": False}
    opts.update(kw)
    return yt.collect(ids, **opts)


def test_collect_new_cached_comments_added_and_refresh(yt, fake_youtube):
    r = run_collect(yt, ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
    assert [(x.id, x.status) for x in r] == [("aaaaaaaaaaa", "new"), ("bbbbbbbbbbb", "new"), ("ccccccccccc", "error")]
    assert r[0].meta["path"].name == "2026-10-01_first_aaaaaaaaaaa.md"
    assert r[0].meta["path"].parent.name == "example-channel"
    assert r[0].meta["words"] == 3
    assert fake_youtube.calls["transcripts"] == 2

    r = run_collect(yt, ["aaaaaaaaaaa"])
    assert r[0].status == "cached"
    assert fake_youtube.calls["transcripts"] == 2

    r = run_collect(yt, ["aaaaaaaaaaa"], comments=5)
    assert r[0].status == "comments-added" and r[0].note == "1 comments"
    assert yt.comments_path(r[0].meta["path"]).read_text().count("- **") == 1
    assert fake_youtube.calls["transcripts"] == 2

    r = run_collect(yt, ["aaaaaaaaaaa"], refresh=True)
    assert r[0].status == "new"
    assert fake_youtube.calls["transcripts"] == 3


def test_collect_channel_folder_falls_back_to_handle_for_nameless_channels(yt, fake_youtube):
    fake_youtube.videos["bbbbbbbbbbb"]["channel"] = "!!!"
    r = run_collect(yt, ["bbbbbbbbbbb"])
    assert r[0].meta["path"].parent.name == "example"


def test_collect_keeps_going_when_one_video_fails(yt, fake_youtube, monkeypatch):
    def flaky(api, vid):
        if vid == "aaaaaaaaaaa":
            raise RuntimeError("boom via http://u:secret@proxy.example/")
        return [snip("ok", 0.0)], "auto (en)"

    monkeypatch.setattr(yt, "fetch_transcript", flaky)
    r = run_collect(yt, ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    assert [(x.id, x.status) for x in r] == [("aaaaaaaaaaa", "error"), ("bbbbbbbbbbb", "new")]
    assert r[0].note.startswith("RuntimeError") and "secret" not in r[0].note


def test_collect_reports_partial_results_when_blocked(yt, fake_youtube, monkeypatch):
    def blocked(api, vid):
        if vid == "bbbbbbbbbbb":
            raise yt.IpBlocked(vid)
        return [snip("ok", 0.0)], "auto (en)"

    monkeypatch.setattr(yt, "fetch_transcript", blocked)
    with pytest.raises(yt.Blocked) as caught:
        run_collect(yt, ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    assert [(x.id, x.status) for x in caught.value.partial] == [("aaaaaaaaaaa", "new")]


def test_collect_date_cutoff_breaks_on_newest_first_sources(yt, fake_youtube):
    run_collect(yt, ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    # Both cached videos are older than one day; newest-first sources stop at the first old one.
    assert run_collect(yt, ["bbbbbbbbbbb", "ccccccccccc"], days=1, newest_first=True) == []
    r = run_collect(yt, ["bbbbbbbbbbb", "ccccccccccc"], days=1, newest_first=False)
    assert [(x.id, x.status) for x in r] == [("ccccccccccc", "error")]


# ---------------------------------------------------------------- watchlist command


def ns(action, name=None, sources=()):
    return SimpleNamespace(action=action, name=name, sources=list(sources))


def test_watchlist_add_dedupes_and_missing_names_fail(yt, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(yt, "WATCHLISTS", tmp_path / "w.json")
    assert yt.cmd_watchlist(ns("add", "ai", ["@a", "@a", "search:q", "@a"])) == 0
    assert yt.load_watchlists() == {"ai": ["@a", "search:q"]}
    assert yt.cmd_watchlist(ns("show", "nope")) == 1
    assert yt.cmd_watchlist(ns("remove", "nope")) == 1
    assert yt.cmd_watchlist(ns("remove", "ai", ["@a"])) == 0
    assert yt.load_watchlists() == {"ai": ["search:q"]}
    capsys.readouterr()


def test_corrupt_watchlists_file_gives_a_clear_error(yt, tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    p.write_text("{not json")
    monkeypatch.setattr(yt, "WATCHLISTS", p)
    with pytest.raises(SystemExit, match="isn't valid JSON"):
        yt.load_watchlists()


def test_days_must_be_positive(yt):
    with pytest.raises(SystemExit):
        yt.main(["ls", "--days", "0"])
