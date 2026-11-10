#!/usr/bin/env bash
# Move the archive between this machine/CI runner and Cloudflare.
#   pull    download state/archive.db from R2 (ok if it does not exist yet)
#   push    upload every media file on disk + archive.db to R2
#   deploy  publish site/public (index.html + data.json) + the media function to Pages
# Needs wrangler auth: `npx wrangler login` locally, or CLOUDFLARE_API_TOKEN +
# CLOUDFLARE_ACCOUNT_ID in CI. R2 bucket: ARCHIVE_BUCKET (polar-archive); Pages
# project: ARCHIVE_PROJECT (polarbeargrowth).
set -euo pipefail
BUCKET="${ARCHIVE_BUCKET:-polar-archive}"
PROJECT="${ARCHIVE_PROJECT:-polarbeargrowth}"
OUT="${ARCHIVE_DIR:-data/archive}"
WR="npx --yes wrangler@4"

case "${1:-}" in
  pull)
    mkdir -p "$OUT"
    if out=$($WR r2 object get "$BUCKET/state/archive.db" --file "$OUT/archive.db" --remote 2>&1); then
      echo "pulled archive.db"
    elif grep -qi "does not exist" <<<"$out"; then
      echo "no archive.db in R2 yet — starting fresh"
    else
      echo "$out" >&2; exit 1   # never start from empty on a transient error
    fi ;;
  push)
    # Resumable: keys already uploaded from this machine are remembered in
    # $OUT/.uploaded, and each upload is retried, so a dropped connection
    # costs one file, not the run. (A fresh CI runner only holds new files.)
    put() {
      for attempt in 1 2 3 4; do
        if $WR r2 object put "$BUCKET/$1" --file "$2" --remote >/dev/null 2>&1; then return 0; fi
        sleep $((attempt * 5))
      done
      return 1
    }
    done_list="$OUT/.uploaded"; touch "$done_list"
    n=0; failed=0
    while IFS= read -r -d '' f; do
      key="${f#"$OUT"/}"
      grep -qxF "$key" "$done_list" && continue
      if [ "$(wc -c <"$f")" -gt 300000000 ]; then
        echo "skipped (over wrangler's 300 MB upload limit): $key" >&2; continue
      fi
      if put "$key" "$f"; then echo "$key" >>"$done_list"; n=$((n + 1))
      else echo "FAILED: $key" >&2; failed=$((failed + 1)); fi
    done < <(find "$OUT/media" -type f -print0 2>/dev/null)
    put "state/archive.db" "$OUT/archive.db" || { echo "FAILED: state/archive.db" >&2; failed=$((failed + 1)); }
    echo "pushed $n new media file(s) + archive.db; $failed failed"
    [ "$failed" -eq 0 ] ;;
  deploy)
    cp "$OUT/data.json" site/public/data.json
    (cd site && $WR pages deploy public --project-name "$PROJECT" --branch main --commit-dirty=true) ;;
  *) echo "usage: $0 pull|push|deploy" >&2; exit 2 ;;
esac
