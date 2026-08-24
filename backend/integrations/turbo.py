"""Splices the native MiniMax H3 Turbo speedup into an already-loaded
API-format workflow, when settings.TURBO_LEVEL enables it for a job
(generation/api.py::_resolve_use_turbo, GenerationJob.use_turbo) -- see
generation/tasks.py::build_api_workflow().

This supersedes the third-party Larryvrh/ComfyUI-MiniMax-H3-Turbo project
(a turbo LoRA + a custom per-stream sampler node) that extras.md originally
documented but didn't integrate: the ComfyUI instance this app now points at
ships the per-stream video/audio sigma-shift natively as MiniMaxH3SigmaShift,
so an ordinary LoraLoaderModelOnly node plus that one node is enough -- no
custom sampler node needed. Both confirmed live against a real instance's
GET /object_info (Aug 2026); see extras.md#turbo.

Same MODEL -> MODEL splice-right-after-the-loader shape as
integrations/spectrum.py -- see that module's docstring for the generic
mechanics (every resources/workflows_api/*.api.json has exactly one
UNETLoader). This inserts two nodes chained instead of one: loader -> LoRA ->
SigmaShift -> (whatever the loader used to feed directly).

Combining with Spectrum: generation/tasks.py calls apply_spectrum() before
apply_turbo() so the two nest in the order both projects' own docs
recommend (loader -> LoRA -> SigmaShift -> Spectrum -> guider/sampler) --
see that call site's comment for why the call order has to be the reverse
of the resulting graph order.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

_UNET_LOADER_CLASS = "UNETLoader"
# Public (not _-prefixed): manage.py check_extras imports these to check
# comfyui.get_object_info() for them, rather than duplicating the literal
# class names in two places.
TURBO_LORA_NODE_CLASS = "LoraLoaderModelOnly"
TURBO_SIGMA_SHIFT_NODE_CLASS = "MiniMaxH3SigmaShift"

# Turbo LoRA filenames, one per MiniMax H3 base-model family -- confirmed
# against a live instance's GET /object_info/LoraLoaderModelOnly ("lora_name"
# combo options), Aug 2026. t2v/i2v share the "fl2v" checkpoint family (see
# resources/workflows_api/*.api.json's UNETLoader.unet_name), r2v uses
# "ref2v". Not env-configurable (unlike the step counts below) -- bump these
# constants directly if a future checkpoint release ships under a different
# filename. apply_turbo() still succeeds either way (it's just building a
# dict); a filename ComfyUI doesn't actually have surfaces as its own
# /prompt validation error, same as any other bad workflow (job.error_message).
LORA_NAME_T2V_I2V = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
LORA_NAME_R2V = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"

# MiniMaxH3SigmaShift's own live defaults -- separate flow schedules for the
# video vs. audio streams (see extras.md#turbo for why a single shared
# schedule over-steps/distorts audio at low step counts).
_SHIFT_VIDEO = 12.0
_SHIFT_AUDIO = 3.0


def _next_node_id(workflow: dict[str, Any]) -> str:
    return str(max(int(nid) for nid in workflow) + 1)


def lora_name(*, is_reference_flow: bool) -> str:
    return LORA_NAME_R2V if is_reference_flow else LORA_NAME_T2V_I2V


def default_steps(*, is_reference_flow: bool) -> int:
    """Sampler steps to use for a turbo job -- see settings.TURBO_STEPS_R2V/
    TURBO_STEPS_T2V_I2V. Called from generation/api.py at job-creation time
    to override RenderPreset.steps entirely (not from build_api_workflow()
    itself, same as every other already-resolved job field -- see that
    function's docstring)."""
    return settings.TURBO_STEPS_R2V if is_reference_flow else settings.TURBO_STEPS_T2V_I2V


def apply_turbo(workflow: dict[str, Any], *, is_reference_flow: bool) -> dict[str, Any]:
    """Mutates and returns `workflow` with the turbo LoRA and sigma-shift
    nodes spliced in right after its sole UNETLoader (loader -> LoRA ->
    SigmaShift -> whatever the loader fed directly before). Raises
    RuntimeError if the workflow doesn't have exactly one -- see
    integrations/spectrum.py::apply_spectrum for why that's always true for
    every shipped template.

    `is_reference_flow` picks the LoRA trained for this workflow's base
    model (r2v's "ref2v" vs. t2v/i2v's "fl2v") -- pass
    `mode in generation.models.REFERENCE_FLOW_MODES` from the caller rather
    than importing that here, keeping this module free of a generation-app
    dependency (matches spectrum.py/motion_context.py both staying
    mode-agnostic).
    """
    loader_ids = [nid for nid, node in workflow.items() if node.get("class_type") == _UNET_LOADER_CLASS]
    if len(loader_ids) != 1:
        raise RuntimeError(
            f"apply_turbo: expected exactly one {_UNET_LOADER_CLASS} node, found {len(loader_ids)}"
        )
    loader_id = loader_ids[0]

    lora_id = _next_node_id(workflow)
    shift_id = str(int(lora_id) + 1)

    for node in workflow.values():
        for value in node.get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2 and value[0] == loader_id:
                value[0] = shift_id

    workflow[lora_id] = {
        "class_type": TURBO_LORA_NODE_CLASS,
        "inputs": {
            "model": [loader_id, 0],
            "lora_name": lora_name(is_reference_flow=is_reference_flow),
            "strength_model": 1.0,
        },
        "_meta": {"title": "Turbo LoRA"},
    }
    workflow[shift_id] = {
        "class_type": TURBO_SIGMA_SHIFT_NODE_CLASS,
        "inputs": {"model": [lora_id, 0], "shift_video": _SHIFT_VIDEO, "shift_audio": _SHIFT_AUDIO},
        "_meta": {"title": "MiniMax H3 Sigma Shift"},
    }
    return workflow
