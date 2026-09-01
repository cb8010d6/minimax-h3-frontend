from django.urls import path

from . import admin_api, api

urlpatterns = [
    path("health/", api.health, name="health"),
    path("config/", api.config, name="config"),
    path("resolution-preview/", api.resolution_preview, name="resolution_preview"),
    path("prompt/refine/", api.refine_prompt, name="refine_prompt"),
    path("prompt/chat/", api.chat_message, name="chat_message"),
    path("presets/", api.list_presets, name="list_presets"),
    path("queue-estimate/", api.queue_estimate, name="queue_estimate"),
    path("gpu/workers/", api.gpu_workers, name="gpu_workers"),
    path("gpu/prewarm/", api.gpu_prewarm, name="gpu_prewarm"),
    path("gpu/unload/", api.gpu_unload, name="gpu_unload"),
    path("jobs/", api.jobs, name="jobs"),
    path("jobs/<int:job_id>/", api.job_detail, name="job_detail"),
    path("folders/", api.folders, name="folders"),
    path("folders/<int:folder_id>/", api.folder_detail, name="folder_detail"),
    path("jobs/<int:job_id>/cancel/", api.cancel_job, name="cancel_job"),
    path("jobs/<int:job_id>/requeue/", api.requeue_job, name="job_requeue"),
    path("jobs/<int:job_id>/steam_deck_export/", api.steam_deck_export, name="steam_deck_export"),
    path("quality-catalog/", admin_api.quality_catalog, name="quality_catalog"),
    path("quality-levels/", admin_api.create_quality_level, name="create_quality_level"),
    # Literal paths must precede <str:label>/<str:seconds> below -- Django
    # tries urlpatterns in order and "reorder"/"estimate" would otherwise
    # themselves match those wildcard converters.
    path(
        "quality-levels/reorder/",
        admin_api.reorder_quality_levels,
        name="reorder_quality_levels",
    ),
    path(
        "quality-levels/<str:label>/", admin_api.update_quality_level, name="update_quality_level"
    ),
    path(
        "quality-durations/estimate/",
        admin_api.estimate_durations,
        name="estimate_durations",
    ),
    path("quality-durations/<str:seconds>/", admin_api.update_duration, name="update_duration"),
]
