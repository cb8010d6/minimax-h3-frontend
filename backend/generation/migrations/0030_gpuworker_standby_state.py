from django.db import migrations, models


def mark_empty_ready_workers_standby(apps, schema_editor):
    GpuWorker = apps.get_model("generation", "GpuWorker")
    GpuWorker.objects.filter(state="ready", loaded_model="").update(state="standby")


class Migration(migrations.Migration):
    dependencies = [("generation", "0029_h3_native_capabilities")]

    operations = [
        migrations.AlterField(
            model_name="gpuworker",
            name="state",
            field=models.CharField(
                choices=[
                    ("offline", "Offline"),
                    ("free", "Free"),
                    ("standby", "Standby"),
                    ("starting", "Starting"),
                    ("ready", "Model ready"),
                    ("busy", "Busy"),
                    ("external", "Busy by another process"),
                    ("error", "Error"),
                ],
                default="offline",
                max_length=16,
            ),
        ),
        migrations.RunPython(mark_empty_ready_workers_standby, migrations.RunPython.noop),
    ]
