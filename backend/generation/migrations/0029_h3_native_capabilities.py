from django.db import migrations, models


VISUAL_MODES = ["t2v", "i2v", "r2v", "t2i", "r2i"]
VIDEO_MODES = ["t2v", "i2v", "r2v"]
IMAGE_MODES = ["t2i", "r2i"]
TIMED_MODES = VIDEO_MODES + ["t2a", "r2a"]


def enforce_h3_capabilities(apps, schema_editor):
    RenderPreset = apps.get_model("generation", "RenderPreset")
    RenderDuration = apps.get_model("generation", "RenderDuration")

    # Official H3 training range is approximately 5-15 seconds at 24 fps.
    RenderDuration.objects.filter(
        preset__mode__in=TIMED_MODES
    ).exclude(duration_seconds__gte=5, duration_seconds__lte=15).update(is_active=False)

    # 0.6 MiP exceeds the 768x768 native square canvas after the official
    # ResolutionSelector rounding; retain only fixed tiers valid at every
    # offered aspect ratio and add one aspect-dependent native maximum.
    RenderPreset.objects.filter(mode__in=VIDEO_MODES, megapixels__gt=0.5).update(is_active=False)

    # The prior image 1/2/4MP rows were experimental extrapolations. H3 has
    # no public 2K image weights here; use the same verified native canvas
    # as the underlying video model and extract the still frame as before.
    RenderPreset.objects.filter(mode__in=IMAGE_MODES).update(is_active=False)

    for mode in VISUAL_MODES:
        is_image = mode in IMAGE_MODES
        preset, _ = RenderPreset.objects.update_or_create(
            mode=mode,
            label="Native",
            defaults={
                "megapixels": 0.98,
                "steps": 20,
                "is_draft": False,
                "is_active": True,
                "sort_order": 100,
                "resolution_policy": "h3_native",
            },
        )
        if is_image:
            RenderDuration.objects.update_or_create(
                preset=preset,
                duration_seconds=0.0,
                defaults={"estimated_render_seconds": 90, "is_active": True},
            )
        else:
            for seconds in range(5, 16):
                RenderDuration.objects.update_or_create(
                    preset=preset,
                    duration_seconds=float(seconds),
                    defaults={
                        "estimated_render_seconds": round(15 + 2.5 * 0.98 * 60 * seconds),
                        "is_active": True,
                    },
                )


class Migration(migrations.Migration):
    dependencies = [("generation", "0028_gpuworker_generationjob_scheduler_fields")]

    operations = [
        migrations.AddField(
            model_name="renderpreset",
            name="resolution_policy",
            field=models.CharField(
                choices=[("fixed", "Fixed megapixels"), ("h3_native", "MiniMax H3 native canvas")],
                default="fixed",
                help_text="Fixed megapixel target or the aspect-dependent native H3 maximum canvas.",
                max_length=16,
            ),
        ),
        migrations.RunPython(enforce_h3_capabilities, migrations.RunPython.noop),
    ]
