from django.db import migrations, models


def backfill_from_first_current_job(apps, schema_editor):
    Project = apps.get_model("director", "Project")
    for project in Project.objects.all().iterator():
        variant = (
            project.clips.filter(current_job__isnull=False)
            .order_by("order")
            .values_list("current_job__model_variant", flat=True)
            .first()
        )
        if variant in {"fp8", "int8"} and variant != project.model_variant:
            project.model_variant = variant
            project.save(update_fields=["model_variant"])


class Migration(migrations.Migration):
    dependencies = [
        ("director", "0010_project_use_turbo"),
        ("generation", "0030_gpuworker_standby_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="model_variant",
            field=models.CharField(
                choices=[("fp8", "FP8"), ("int8", "INT8")],
                default="fp8",
                help_text="The FP8/INT8 quantization used by every new or re-rendered Clip in this project.",
                max_length=8,
            ),
        ),
        migrations.RunPython(backfill_from_first_current_job, migrations.RunPython.noop),
    ]
