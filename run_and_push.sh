#!/bin/zsh
# Runs the P/S scan and pushes the snapshot to GitHub Pages.
# Triggered by launchd every Friday at 21:30 UTC (≈30 min after market close).
set -e

cd "$HOME/ps-dashboard" || exit 1

# launchd has a barebones PATH — add Python + git
export PATH="$HOME/.homebrew/bin:$HOME/.homebrew/Caskroom/miniconda/base/bin:/usr/bin:/bin:/usr/local/bin"

PY="$HOME/.homebrew/Caskroom/miniconda/base/bin/python3"

"$PY" scan.py >> /tmp/ps-scan.log 2>&1

if [[ -n $(git status --porcelain docs/results.json) ]]; then
    TS=$(date -u +"%Y-%m-%d %H:%M UTC")
    git add docs/results.json
    git -c user.name='Kuba Kaszynski' -c user.email='piraci26@gmail.com' commit -m "snapshot @ $TS" -q
    git push -q 2>> /tmp/ps-scan.log
    echo "[$TS] pushed snapshot" >> /tmp/ps-scan.log
fi
