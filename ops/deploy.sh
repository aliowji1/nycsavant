#!/usr/bin/env bash
# Publish site/ somewhere static. Pick ONE by setting DEPLOY_TARGET in .env.
set -euo pipefail
cd "$(dirname "$0")/.."

# read only the deploy settings from .env (cron doesn't load it for us)
if [ -f .env ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      DEPLOY_TARGET|GH_PAGES_REMOTE|GH_PAGES_KEY|CF_PROJECT|RSYNC_DEST) [ -n "$v" ] && export "$k=$v" ;;
    esac
  done < .env
fi

case "${DEPLOY_TARGET:-none}" in
  gh-pages)
    # Keeps a clone of the gh-pages branch in build/gh-pages and pushes only what
    # changed, so a typical night uploads a few MB (GitHub Pages serves the branch).
    # On the 1st of each month the branch is squashed to one commit so it stays small.
    REMOTE="${GH_PAGES_REMOTE:-git@github.com:aliowji1/nycsavant.git}"
    KEY="${GH_PAGES_KEY:-$HOME/.ssh/nycsavant_deploy}"
    export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    dir=build/gh-pages
    g() { git -C "$dir" "$@"; }
    if [ ! -d "$dir/.git" ]; then
      mkdir -p "$dir"
      g init -q
      g remote add origin "$REMOTE"
      if g fetch -q --depth 1 origin gh-pages 2>/dev/null; then
        g checkout -q -b gh-pages FETCH_HEAD
      else
        g checkout -q --orphan gh-pages
      fi
    fi
    g config user.name "nycsavant bot"
    g config user.email "nycsavant-bot@users.noreply.github.com"

    rsync -a --delete --exclude .git site/ "$dir/"
    touch "$dir/.nojekyll"          # serve files as-is, no Jekyll processing
    g add -A

    if [ "$(date +%d)" = "01" ] || [ "${DEPLOY_SQUASH:-0}" = "1" ]; then
      g checkout -q --orphan squash
      g commit -qm "site $(date +%F)"
      g branch -D gh-pages >/dev/null 2>&1 || true
      g branch -m gh-pages
      g push -qf origin gh-pages
      g reflog expire --expire=now --all
      g gc -q --prune=now
      echo "published (squashed) $(date +%F)"
    elif g diff --cached --quiet; then
      echo "no changes to publish"
    else
      g commit -qm "nightly $(date +%F)"
      g push -q origin gh-pages
      echo "published $(date +%F)"
    fi
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
