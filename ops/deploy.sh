#!/usr/bin/env bash
# Publish site/ somewhere static. Pick ONE of these by setting DEPLOY_TARGET in .env.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${DEPLOY_TARGET:-none}" in
  gh-pages)
    # Pushes site/ to the gh-pages branch of this repo (GitHub Pages serves it).
    tmp=$(mktemp -d)
    cp -r site/. "$tmp"
    cd "$tmp"
    git init -q && git checkout -q -b gh-pages
    git add -A && git commit -qm "nightly build $(date +%F)"
    git push -qf "${GH_PAGES_REMOTE:?set GH_PAGES_REMOTE, e.g. git@github.com:you/nycsavant.git}" gh-pages
    ;;
  cloudflare)
    # Cloudflare Pages: npm i -g wrangler; wrangler login (once)
    wrangler pages deploy site --project-name "${CF_PROJECT:-nycsavant}"
    ;;
  rsync)
    rsync -az --delete site/ "${RSYNC_DEST:?set RSYNC_DEST, e.g. user@host:/var/www/nycsavant}"
    ;;
  none)
    echo "DEPLOY_TARGET not set; site built in ./site" ;;
esac
