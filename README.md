# youtube-reader

Lets Claude read YouTube. Give Claude a video, a channel, a playlist or a search, and it saves the transcripts and video details, plus top comments when they help, to a folder on your computer. Claude reads those files to answer you, and since they stay on disk, later questions can cover everything you've saved.

Things you can ask:

- "Summarize this video and check whether the claims hold up: <link>"
- "What have Fireship and ThePrimeagen said about AI coding tools this month?"
- "Find videos about agent memory from the last two weeks. What are people struggling with?"
- "Save Fireship, ThePrimeagen and a search for 'ai agents' as my `ai` watchlist."
- "Go through my `ai` watchlist for the last 14 days. What's trending, and what is nobody covering?"

## Install

In Claude Code, add the [claude-skillset](https://github.com/huttonbenj/claude-skillset) catalog and install from it:

```
/plugin marketplace add huttonbenj/claude-skillset
/plugin install youtube-reader@claude-skillset
```

That's all. The first time Claude uses it, the tool sets up its own Python environment, which takes up to a minute.

You'll need:

- Claude Code on macOS or Linux
- Python 3.10 or newer with the `venv` module (on Debian or Ubuntu: `sudo apt install python3-venv`)
- A home internet connection. YouTube blocks cloud servers and many VPNs; see [Running it somewhere else](#running-it-somewhere-else).

To update, run `claude plugin update youtube-reader`, then restart Claude Code. Uninstalling the plugin leaves `~/.youtube-reader` behind (about 100 MB of Python packages plus your library), so delete that folder too if you want everything gone.

## Where your files go

Everything lives in `~/.youtube-reader`, outside the plugin, so plugin updates never touch your library or watchlists.

| Path | What's there |
|---|---|
| `library/<channel>/<date>_<title>_<id>.md` | One file per video: details, chapters, description and the transcript with timestamps |
| `library/<channel>/…<id>.comments.md` | Top comments, when you ask for them |
| `watchlists.json` | Your saved watchlists |
| `venv/` | The tool's Python environment |

Set `YT_HOME` to move the whole folder, or `YT_LIBRARY` to move just the library.

## Using it from the terminal

Claude runs the tool for you, but you can also run it yourself from a clone of this repo. The script is `skills/youtube/scripts/yt`, and it uses the same `~/.youtube-reader` folder, so you and Claude share one library. The examples assume an alias:

```bash
alias yt=~/path/to/youtube-reader/skills/youtube/scripts/yt
```

```bash
yt get "https://www.youtube.com/watch?v=VIDEO_ID"   # one video
yt get @Fireship --days 14 --comments                # a channel's last two weeks, with comments
yt search "claude code" --days 7 --limit 10          # search results
yt watchlist add ai @Fireship "search:ai agents"     # save a set of sources
yt watch ai --days 14                                # fetch everything in a watchlist
yt ls --days 30                                      # what's already saved
yt update                                            # upgrade yt-dlp and youtube-transcript-api
```

Add `--help` to any command to see all its options.

## How it works

Two open-source libraries do the downloading. [yt-dlp](https://github.com/yt-dlp/yt-dlp) finds videos and reads their details and comments, and [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) gets the captions, the same ones YouTube shows in its transcript panel. No API key is needed.

It prefers captions the creator uploaded, then YouTube's auto-generated ones. For a video in another language, it saves YouTube's English translation when there is one; each file's `captions:` line says which you got.

Videos you've already saved are reused instead of downloaded again; `--refresh` gets a fresh copy. The tool pauses between requests and stops if YouTube starts refusing them.

It reads captions and comments that YouTube shows publicly, and it's meant for personal research. You're responsible for using it in line with YouTube's Terms of Service.

## Running it somewhere else

On a cloud server or behind a VPN, the tool needs a residential proxy. Set one of these and it's used for everything:

| Variable | Use |
|---|---|
| `YT_WEBSHARE_USER` and `YT_WEBSHARE_PASS` | A [Webshare](https://www.webshare.io) rotating residential plan |
| `YT_PROXY` | Any other proxy, as `http://user:pass@host:port` |

No proxy is used unless you set one.

## If something breaks

- YouTube changes things every few months. Run `yt update` first.
- If it stops and says YouTube is refusing requests, wait 30 to 60 minutes and try again. If that happens on the very first video, the connection is probably blocked outright; see [Running it somewhere else](#running-it-somewhere-else).
- Auto-generated captions often misspell names, so search your library by topic rather than by person.

## Development

`./install.sh` links this checkout into `~/.claude/skills/youtube-reader`, so Claude Code loads your working copy and picks up edits at the next session or after `/reload-plugins`. If the marketplace version is also installed, it takes priority, so uninstall it while you work.

```bash
~/.youtube-reader/venv/bin/python -m pip install -r requirements-dev.txt
~/.youtube-reader/venv/bin/python -m pytest
~/.youtube-reader/venv/bin/ruff check . && ~/.youtube-reader/venv/bin/ruff format --check .
claude plugin validate .
```

GitHub Actions runs these checks plus shellcheck on pushes to main and on pull requests, with the tests on Python 3.10 and 3.14.

There's no `version` in `plugin.json`, so anyone who installed from the marketplace gets the latest commit when they run `claude plugin update youtube-reader`.

## License

MIT. See [LICENSE](LICENSE).
