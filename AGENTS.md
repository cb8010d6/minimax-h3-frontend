# Notes for coding agents working in this repo

Operational knowledge that isn't obvious from the code alone — mostly
hard-won during actual sessions in this repo. Keep this current: add to it
when something costs real back-and-forth to figure out, prune it when it
goes stale.

## Docker: all building and testing happens here

Owner's standing instruction (supersedes any host-side dev loop): **all
building and testing in Docker** — never run `uv`, `npx`, `npm`, or a
host Python against this repo. The Compose stack is the only environment
that matters, and the mechanism that makes it so: **nothing is
bind-mounted** — `backend/Dockerfile` `COPY`s `backend/` and `resources/`
in at build time, and `frontend/Dockerfile` runs `tsc -b && vite build`
at build time. A code change is invisible to the running stack until its
image is rebuilt.

- **`docker compose exec` runs the pre-`up -d` image.** The working order
  after any change is always: `docker compose build <changed images>` →
  `docker compose up -d` (recreates the containers from the new images
  *and* re-runs the one-shot `migrate` service) → *then*
  `docker compose exec ...`. An `exec` between build and `up -d` silently
  runs the previous code — hit this for real (tests "failing" on code
  that was already fixed in the working tree).
- **`backend`, `qcluster`, and `migrate` each build their own separate
  image**, even though all three share `backend/Dockerfile` and the same
  build context — Compose does not dedupe this. `docker compose build
  backend` alone does **not** rebuild `qcluster` or `migrate`. After any
  backend change: `docker compose build backend qcluster migrate` (add
  `frontend` too if frontend changed), then `docker compose up -d`. Forgot
  `migrate` once and a whole migration silently ran against stale code
  with `Unknown command` / stale schema — always list all four explicitly
  rather than relying on Compose to infer what changed.
- **Already-applied migrations don't re-run**, even if you edit the
  migration file afterward (Django tracks `(app, name)` in
  `django_migrations`, not file content). To force a specific migration to
  actually re-execute (e.g. you fixed a bug in a data migration's body
  after it already "succeeded" but did nothing useful): unapply then
  reapply it explicitly —
  `docker compose exec backend python manage.py migrate <app> <migration_before_target>`
  then `... migrate <app> <target>`.
- **The one-shot `migrate` service may not have the same volume mounts as
  `backend`/`qcluster`** (it didn't have `media_data` until this was hit
  for real — see `docker-compose.yml`'s `migrate` comment). A data
  migration that reads/writes actual media files can fail with a
  misleading "file not found" for files that genuinely exist, just not
  inside *that* container. Prefer writing such logic as a reusable,
  idempotent management command (e.g. `backfill_thumbnails`) that can be
  run manually against `backend`/`qcluster` (which do have real media
  access), with the migration itself only calling it best-effort and never
  blocking the deploy if it can't run there.
- This is the repo owner's own dev/test stack, not a production
  deployment they've asked to be treated cautiously — confirmed explicitly
  once already this history. Default to just rebuilding + redeploying once
  changes are verified, without asking first each time. If a future
  session has reason to believe a deployment *is* meant to be treated as
  live/shared, that overrides this note — ask.

## Git

- **Commit after each logical chunk**, not one giant commit at the end —
  this session's history is a good model: one commit per feature/fix
  (extras toggle, docs reorg, the six `frontend fixes.txt` items, the
  thumbnail-scaling fix, the title-length fix all landed separately).
- `frontend fixes.txt` (repo root) is the owner's personal, deliberately
  untracked scratch notes file — never stage it unless explicitly asked to.
  `git add -A -- ':!frontend fixes.txt'` (or just add paths explicitly)
  rather than a bare `git add -A`.
- Match the existing commit style: short imperative subject, a body that
  explains *why* (not a restated diff), `Co-Authored-By: Claude Sonnet 5
  <noreply@anthropic.com>` trailer.

## Backend dev loop

- Everything runs in the container: `docker compose exec backend python
  manage.py <command>` — `check`, `makemigrations` /
  `makemigrations --check --dry-run`, `test --noinput`, `shell`,
  `migrate`, all of it. There is no host-side `uv run` loop for this repo;
  the container's Python *is* the project environment (and it only has
  the current code after a rebuild — see the exec-runs-old-image note
  above). The host shell can't reach the DB at all anyway
  (`DB_HOST=db` only resolves inside the Compose network).
- `manage.py test` uses the test DB `test_mm_h3`. After a *crashed* test
  run (interrupted mid-test) the stale test DB can make the next run fail
  confusingly — `--noinput` handles the drop/recreate prompt.
- `makemigrations app1 app2 --name X` applies the *same* `--name` to every
  app touched in that invocation — produces a misleadingly-named file for
  whichever app the name doesn't actually describe. Check the generated
  filenames and rename before committing (content is unaffected, only the
  filename).

## Frontend dev loop

- The type-check *is* the image build: `docker compose build frontend`
  runs `tsc -b && vite build` inside the image — a type error fails the
  build, and a clean build *is* the type-check. Then `docker compose up -d`
  to serve the new bundle (nginx picks the built assets up from the
  image).
- `oxlint` is **not** part of the Docker build — it isn't enforced in
  this loop; the in-image build is the bar.

## Gotchas worth knowing before you hit them

- **`backend/generation/media_views.py::_owner_id_for_path`** hardcodes
  recognized `MEDIA_ROOT` path prefixes (`generated_videos/`,
  `thumbnails/`, `references/`) to look up who owns a file for the
  protected-media view. Add a new `FileField` with a new upload directory
  and forget this, and its URLs 404 for *everyone*, including the file's
  owner — silently, no error anywhere obvious. Always add a matching
  branch here when adding a new `FileField`/`upload_to`.
- **ComfyUI's `GET /object_info/<class_type>` never 404s** — an unrecognized
  node type still returns `200 {}`. Check whether the body is empty, not
  the status code, when probing whether a custom node is actually
  installed (see `integrations/comfyui.get_object_info`).
- **Data migrations that call a command querying the live model can abort
  the whole migration transaction.** On a fresh DB (like the test DB), a
  failed SQL statement poisons the surrounding transaction — every
  subsequent query dies with `InFailedSqlTransaction` — and Django's
  `Atomic.__exit__` only rolls back to the savepoint on a *propagated*
  exception (a failed query sets neither `needs_rollback` nor raises), so
  the migration can die mid-chain with a confusing error. Keep the
  command's query to `.only(...)` the fields it actually reads (a
  full-model SELECT is what aborted 0021), wrap the call in a nested
  `transaction.atomic()` so the exception propagates out, and catch it
  outside the migration body so a failure never blocks the deploy (see
  `backend/generation/migrations/0021_backfill_video_thumbnails.py`).
- **Stale Django-Q tasks retry forever.** If a task function is renamed or
  moved (e.g. `generation.tasks.run_generation_job` was replaced by
  `process_queue`), rows already enqueued in the Q cluster's DB keep
  failing on every poll with `Function ... is not defined` and
  `attempt_count` climbing — one had 569 attempts over a month and filled
  the qcluster logs. In `manage.py shell`, check
  `Task.objects.filter(success=False)` (from `django_q.models`) for `func`
  values that no longer exist in the code and delete them.
- **The project's public port is 8080, not 3080.** `http://127.0.0.1:3080`
  is a different server that answers unknown paths with a plain lowercase
  `not found` 404 — a confusing signal that looks like a broken backend
  but isn't. `docker-compose.yml` maps `8080:8080`; nginx inside the
  frontend container listens on 8080 and proxies `/api`, `/accounts`,
  `/admin`, `/invite`, `/static`, `/media/` to backend gunicorn on :8000.
- Shell tool + this repo's path: the working directory has a space
  (`.../AI stuff/MinimaxH3 front`) — always pass the full path
  double-quoted in one go rather than a bare relative path from an
  assumed cwd.

## Where things live

- `README.md` — user-facing setup/config.
- `docs/ARCHITECTURE.md` — how/why the system is built the way it is;
  has a "Deferred" section for known-not-built-yet items.
- `docs/FUNCTION_CHECK.md` — manual end-to-end verification procedure.
- `docs/extras.md` — optional third-party ComfyUI custom-node integrations
  (`COMFYUI_EXTRAS` env var).
- This codebase has a strong convention of dense, *why*-focused inline
  docstrings/comments that cross-reference other files by name (e.g. "see
  `ARCHITECTURE.md`'s Deferred section") — match that style in new code
  rather than comments that just restate what the code does.
- **Keep these docs updated as part of the change that needs it**, not as
  a followup — a new feature, config option, endpoint, or gotcha belongs in
  the relevant doc (and this file, if it's operational) in the same commit,
  same as the code-level cross-referencing above. A stale README/
  ARCHITECTURE.md is worse than none, in a repo this deliberate about
  cross-references actually being accurate.

## Before calling something done

- Backend: `docker compose exec backend python manage.py check`,
  `makemigrations --check --dry-run`, `manage.py test --noinput` — run
  *after* `docker compose build backend qcluster migrate` + `up -d`, so
  they execute the current code (see the exec-runs-old-image note above).
- Frontend: `docker compose build frontend` (the in-image `tsc -b &&
  vite build` is both the type-check and the build).
- Anything that touches the *running* stack's behavior (new migration, new
  env var, new endpoint, new management command): actually rebuild +
  redeploy and verify against the live containers — curl the endpoint
  (port 8080), query via `manage.py shell`, check `docker compose logs`
  — rather than stopping at "the diff looks right."
