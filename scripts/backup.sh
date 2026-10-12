#!/usr/bin/env bash
# Back up the stack: a PostgreSQL dump, a fresh anchor of every audit head, and the files volume (after the dump,
# so a file is never missing for a row the dump holds). Run from the repository folder; keeps nothing but copies.
#
#   scripts/backup.sh [backup-folder]      (default ./backups)
set -euo pipefail

dest="${1:-backups}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
project="$(docker compose config --format json | python -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
mkdir -p "$dest"

echo "1/3 database dump"
docker compose exec -T postgres pg_dump -U ow_owner -Fc -Z 6 onboarding > "$dest/onboarding-$stamp.dump"

echo "2/3 anchor (the head of every audit stream, written outside the database)"
docker compose run --rm --no-deps anchor onboarding-worker db anchor --anchor-dir /anchors

echo "3/3 files volume (inbox, uploads, replay blobs, engine store) and anchors"
docker run --rm -v "${project}_worker-data:/data:ro" -v "${project}_anchors:/anchors:ro" \
  -v "$(pwd)/$dest:/backup" alpine \
  sh -c "tar czf /backup/files-$stamp.tgz -C /data . && tar czf /backup/anchors-$stamp.tgz -C /anchors ."

( cd "$dest" && sha256sum "onboarding-$stamp.dump" "files-$stamp.tgz" "anchors-$stamp.tgz" > "SHA256SUMS-$stamp" )
echo "done: $dest/*-$stamp.*"
