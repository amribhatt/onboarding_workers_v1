#!/usr/bin/env bash
# Prove a backup restores: load a dump into a scratch database, check it is at this build's schema, re-verify every
# audit chain, and compare the chains with the anchors (a restored altered dump fails here). Never touches the live
# database. Run monthly, and before relying on a backup.
#
#   scripts/restore_drill.sh backups/onboarding-<stamp>.dump
set -euo pipefail

dump="${1:?usage: restore_drill.sh <dump file>}"
scratch="onboarding_drill"
owner_url="$(cat secrets/owner_database_url.txt)"
drill_url="${owner_url%/*}/${scratch}"

echo "restoring $dump into $scratch"
docker compose exec -T postgres psql -U ow_owner -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS ${scratch}" -c "CREATE DATABASE ${scratch}"
docker compose exec -T postgres pg_restore -U ow_owner -d "${scratch}" --no-owner --exit-on-error < "$dump"

echo "schema revision (must be this build's head) and row counts"
docker compose exec -T postgres psql -U ow_owner -d "${scratch}" -At \
  -c "select 'revision ' || version_num from alembic_version" \
  -c "select 'audit entries ' || count(*) from audit_log" \
  -c "select 'streams ' || count(distinct stream) from audit_log"

echo "verifying every chain and the anchors"
docker compose run --rm --no-deps -e DATABASE_URL="${drill_url}" verifier \
  onboarding-worker db verify --anchor-dir /anchors --restore-drill

docker compose exec -T postgres psql -U ow_owner -d postgres -c "DROP DATABASE ${scratch}"
echo "drill passed"
