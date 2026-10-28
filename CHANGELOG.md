# Changelog

## Unreleased

- Gentler on YouTube, to avoid rate limits: a randomized few-second pause between videos, slower metadata requests, and a one-minute wait with one retry when YouTube pushes back.
- Only one fetch runs at a time across every Claude session; others wait their turn.

## First public release (October 2026)

- Saves transcripts, video details and top comments for videos, channels, playlists and searches as Markdown files in a local library.
- Watchlists: save a set of channels, playlists and searches, then fetch them all with `yt watch`.
- Videos already in the library are reused instead of downloaded again.
- Sets up its own Python environment on first run and keeps everything in `~/.youtube-reader`, outside the plugin, so updates leave your library alone.
- Keeps going when a single video fails, and stops cleanly if YouTube starts refusing requests.
- Optional residential proxy support for running it away from a home connection.
- Claude treats transcripts, descriptions and comments as untrusted text, never as instructions.
