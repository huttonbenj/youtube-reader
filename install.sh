#!/bin/sh
# Developer install: links this checkout into ~/.claude/skills so Claude Code loads it as a
# plugin (youtube-reader@skills-dir) and picks up edits without reinstalling.
# Everyone else installs from the claude-skillset marketplace; see README.md. Safe to re-run.
set -e
REPO="$(cd "$(dirname "$0")" && pwd -P)"
SKILLS="$HOME/.claude/skills"
LINK="$SKILLS/youtube-reader"
mkdir -p "$SKILLS"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  echo "$LINK exists and isn't a symlink; leaving it alone." >&2
  exit 1
fi
ln -sfn "$REPO" "$LINK"
chmod +x "$REPO/skills/youtube/scripts/yt"

"$REPO/skills/youtube/scripts/yt" ls >/dev/null   # builds the Python environment on first run
echo "Linked $LINK -> $REPO"
echo "Start a new Claude Code session, or run /reload-plugins, to pick it up."
