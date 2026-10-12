# 13 Sponsor vocabulary, default templates, sponsor overlay

Read `docs/design-onboarding-assistant.md` section 8 (decisions on the static-entity spine) first. Vocabulary: sponsor = GP/manager (the customer),
fund = entity (entity ID, name, go-live date, optional fund group ID + name; sponsor = sponsor ID, name, go-live date; go-live mandatory), record type = investor, investment, commitment, affiliate, vendor, GL.

Prerequisite: the uncommitted work in `templates.py`, `registry.py`, `staging.py`, `tools.py` is reviewed, tested and
committed first (it already has `templates_for(client)`, `dedup`, `id_generation`, `distinct_rows`).

Do:
- Rename `client` to `sponsor` in code, state paths, API and UI (keep a read-only alias for old state folders).
- Default templates come from a configurable folder (`WORKER_TEMPLATES_DIR`; the settings tab can upload a template
  per record type). `Source_Samples/mock_data/Upload_templates` is the north star: same six names and headers as
  `templates/` today.
- A template uploaded for one job overrides the default for that job only; the audit records which template won.
- Sponsor overlay = the sponsor's own template files (different fields allowed), plus its own chart of accounts and
  mapping rules as data files under `templates/sponsors/<sponsor>/`. Overlay replaces by record type; the rest stays
  default.
- `effective_settings(sponsor, record_type, job_template=None)` returns every setting with its source label
  `organisation | sponsor override | job override`; the approval card shows it.

Tests: overlay replaces only the named record type; job template beats sponsor beats default; labels are right;
unknown sponsor is refused; a sponsor never sees another sponsor's overlay.

Acceptance: green tests (`> .logs/t13.log`), ruff clean, STATUS.md updated, one commit.
