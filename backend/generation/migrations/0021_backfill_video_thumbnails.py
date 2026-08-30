"""Best-effort attempt to backfill GenerationJob.thumbnail_file for existing
done video-content-type jobs, via the reusable `backfill_thumbnails`
management command (generation/management/commands/backfill_thumbnails.py) --
see that command's docstring for the actual logic.

Deliberately best-effort, not authoritative: this needs real access to the
media files, which the one-shot `migrate` service may not have depending on
deployment (see docker-compose.yml's migrate service comment) -- if this
whole step fails outright (not found, permission error, no ffmpeg, etc.),
it's swallowed here rather than blocking the rest of the deploy, with a
pointer to run the command manually against a container that does have
media access (backend/qcluster both mount it). The command itself already
handles per-job failures gracefully; this only guards against the command
being unable to run *at all* in this environment.

The call sits inside its own transaction.atomic(): a failed SQL statement
aborts the *whole* Postgres transaction, not just the statement, and
Django only rolls a savepoint back when an exception propagates *out* of
the atomic block -- swallowing the exception inside it (as this function
must) would release the savepoint with the aborted state still in place,
and the very next statement of this migrate run (recording this migration
in django_margins) would die with InFailedSqlTransaction. That's exactly
what broke fresh-database test runs once the model grew fields
(is_favorite, use_turbo) that didn't exist yet at this point in the chain
and the command's SELECT referenced them. Rolling back to the savepoint
on the way out of the inner block clears the aborted state, so the
migration can still be recorded no matter how the command fails. (The
command itself also only SELECTs the columns it needs -- see
backfill_thumbnails.py -- so on a fresh database this path is a no-op
that never fails at all.)
"""

from __future__ import annotations

from django.db import migrations, transaction


def backfill_thumbnails(apps, schema_editor):
    from django.core.management import call_command

    try:
        with transaction.atomic():  # see module docstring -- containment, not atomicity
            call_command("backfill_thumbnails")
    except Exception as exc:  # noqa: BLE001 -- see module docstring
        print(
            "  backfill_video_thumbnails: couldn't run here "
            f"({exc}) -- run `manage.py backfill_thumbnails` manually against a "
            "container with real media access (e.g. `docker compose exec backend ...`)."
        )


class Migration(migrations.Migration):

    dependencies = [
        ('generation', '0020_generationjob_title_thumbnail'),
    ]

    operations = [
        migrations.RunPython(backfill_thumbnails, reverse_code=migrations.RunPython.noop),
    ]
