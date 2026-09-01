from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("generation", "0027_jobfolder_generationjob_folders_and_more")]

    operations = [
        migrations.CreateModel(
            name="GpuWorker",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("host", models.CharField(max_length=64)),
                ("cuda_index", models.PositiveSmallIntegerField()),
                ("gpu_uuid", models.CharField(max_length=96, unique=True)),
                ("name", models.CharField(blank=True, default="", max_length=128)),
                ("port", models.PositiveIntegerField()),
                ("state", models.CharField(choices=[("offline", "Offline"), ("free", "Free"), ("starting", "Starting"), ("ready", "Ready"), ("busy", "Busy"), ("external", "Busy by another process"), ("error", "Error")], default="offline", max_length=16)),
                ("managed_pid", models.PositiveIntegerField(blank=True, null=True)),
                ("loaded_model", models.CharField(blank=True, default="", max_length=32)),
                ("memory_used_mb", models.PositiveIntegerField(default=0)),
                ("memory_total_mb", models.PositiveIntegerField(default=0)),
                ("utilization_percent", models.PositiveSmallIntegerField(default=0)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
            ],
            options={"ordering": ["host", "cuda_index"]},
        ),
        migrations.AddConstraint(
            model_name="gpuworker",
            constraint=models.UniqueConstraint(fields=("host", "cuda_index"), name="unique_gpu_host_index"),
        ),
        migrations.AddField(
            model_name="generationjob",
            name="model_variant",
            field=models.CharField(choices=[("fp8", "FP8"), ("int8", "INT8")], default="fp8", help_text="MiniMax H3 quantization selected by the user for this render.", max_length=8),
        ),
        migrations.AddField(
            model_name="generationjob",
            name="assigned_worker",
            field=models.ForeignKey(blank=True, help_text="Physical GPU leased for this job; null while queued.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="jobs", to="generation.gpuworker"),
        ),
        migrations.AddField(
            model_name="gpuworker",
            name="current_job",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="leased_worker", to="generation.generationjob"),
        ),
    ]
