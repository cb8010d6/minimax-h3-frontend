#!/usr/bin/env python3
"""Safe per-GPU ComfyUI controller, executed on gpu01/gpu02 over SSH.

Every physical GPU is eligible, but start performs a last-moment system
compute-process check and refuses to touch a GPU used by another process.
Only process groups whose pid file lives in this tool's state directory are
ever stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = Path(os.environ.get("MINIMAX_H3_ROOT", "/opt/minimax-h3"))
COMFY_ROOT = Path(os.environ.get("COMFYUI_ROOT", "/opt/comfyui"))
PYTHON = Path(os.environ.get("COMFYUI_PYTHON", "/opt/comfyui/venv/bin/python"))
START_SCRIPT = Path(os.environ.get("COMFYUI_START_SCRIPT", "/opt/comfyui/start_comfy.sh"))
COMFYUI_LOG_ROOT = Path(os.environ.get("COMFYUI_LOG_ROOT", "/var/log/comfyui"))
STATE_ROOT = BASE / "state" / "workers"
# Per-job payloads live in the GPU host's system RAM, not GPU VRAM and not
# persistent disk.  The user-selected references and final result remain on
# admin's MEDIA_ROOT; these directories are only the leased worker's staging
# area and are cleared after every render/prewarm.
RUNTIME_DATA = Path(
    os.environ.get("COMFYUI_RUNTIME_DATA_ROOT", "/dev/shm/minimax-h3/comfyui")
)
USER_DATA = BASE / "data" / "comfyui-user"
PORT_BASE = int(os.environ.get("COMFYUI_PORT_BASE", "18100"))


def _run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=20)


def _pid_file(index: int) -> Path:
    # BASE is shared between gpu01/gpu02.  A flat gpu3.pid lets one host
    # overwrite the other host's ownership record, after which a genuine H3
    # worker is misclassified as an external process and can never be safely
    # stopped.  Keep ownership host-local inside the shared filesystem.
    raw_host = os.environ.get("COMFYUI_HOST_ID") or socket.gethostname().split(".", 1)[0]
    host_id = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_host)
    if not host_id:
        raise RuntimeError("Cannot derive a safe GPU host id for worker state")
    return STATE_ROOT / host_id / f"gpu{index}.pid"


def _read_pid(index: int) -> int | None:
    try:
        pid = int(_pid_file(index).read_text().strip())
        os.kill(pid, 0)
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            errors="replace"
        )
        expected_port = str(PORT_BASE + index)
        if str(PYTHON) not in cmdline or "main.py" not in cmdline or expected_port not in cmdline:
            return None
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def _descendants(root_pid: int | None) -> set[int]:
    if not root_pid:
        return set()
    children: dict[int, list[int]] = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fields = (proc / "stat").read_text().split()
            children.setdefault(int(fields[3]), []).append(int(proc.name))
        except (OSError, ValueError, IndexError):
            continue
    found, stack = {root_pid}, [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _compute_processes() -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    try:
        raw = _run(
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return result
    for line in raw.splitlines():
        parts = [part.strip() for part in line.split(",", 3)]
        if len(parts) != 4 or not parts[1].isdigit():
            continue
        result.setdefault(parts[0], []).append(
            {"pid": int(parts[1]), "name": parts[2], "memory_mb": int(parts[3] or 0)}
        )
    return result


def inventory() -> list[dict]:
    compute = _compute_processes()
    raw = _run(
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    )
    rows = []
    for line in raw.splitlines():
        index_s, uuid, name, used, total, util = [part.strip() for part in line.split(",", 5)]
        index = int(index_s)
        managed_pid = _read_pid(index)
        managed_tree = _descendants(managed_pid)
        processes = compute.get(uuid, [])
        external = [proc for proc in processes if proc["pid"] not in managed_tree]
        port = PORT_BASE + index
        rows.append(
            {
                "index": index,
                "uuid": uuid,
                "name": name,
                "memory_used_mb": int(used),
                "memory_total_mb": int(total),
                "utilization_percent": int(util),
                "managed_pid": managed_pid,
                "managed_running": bool(managed_pid),
                "external_processes": external,
                "port": port,
                "healthy": _healthy(port),
            }
        )
    return rows


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/system_stats", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _target(index: int) -> dict:
    return next(row for row in inventory() if row["index"] == index)


def start(index: int) -> dict:
    row = _target(index)
    if row["external_processes"]:
        raise SystemExit(f"GPU{index} is occupied by unmanaged compute processes")
    if row["healthy"]:
        return row
    _pid_file(index).parent.mkdir(parents=True, exist_ok=True)
    for child in ("input", "output", "temp"):
        (RUNTIME_DATA / f"gpu{index}" / child).mkdir(parents=True, exist_ok=True)
    (USER_DATA / f"gpu{index}").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["H3_CACHE_MODE"] = "lru"
    env["H3_CACHE_LRU"] = "1"
    completed = subprocess.run(
        [
            str(START_SCRIPT), str(row["port"]), str(index),
            "--input-directory", str(RUNTIME_DATA / f"gpu{index}" / "input"),
            "--output-directory", str(RUNTIME_DATA / f"gpu{index}" / "output"),
            "--temp-directory", str(RUNTIME_DATA / f"gpu{index}" / "temp"),
            "--user-directory", str(USER_DATA / f"gpu{index}"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=210,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stdout + completed.stderr)
    actual_pid_file = COMFYUI_LOG_ROOT / f"comfy-{row['port']}.pid"
    try:
        actual_pid = int(actual_pid_file.read_text().strip())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ComfyUI started without a valid pid file: {exc}")
    _pid_file(index).write_text(f"{actual_pid}\n")
    if not _healthy(row["port"]):
        raise SystemExit(f"ComfyUI GPU{index} start script returned but API is unhealthy")
    return _target(index)


def unload(index: int) -> dict:
    row = _target(index)
    if not row["healthy"]:
        return row
    request = urllib.request.Request(
        f"http://127.0.0.1:{row['port']}/free",
        data=b'{"unload_models":true,"free_memory":true}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30):
        pass
    return _target(index)


def cleanup(index: int) -> dict:
    """Remove only this managed worker's RAM-backed payload directories."""
    row = _target(index)
    root = (RUNTIME_DATA / f"gpu{index}").resolve()
    runtime_root = RUNTIME_DATA.resolve()
    if root.parent != runtime_root:
        raise SystemExit("Refusing to clean outside the configured runtime data root")
    for child in ("input", "output", "temp"):
        path = root / child
        if path.exists():
            for entry in path.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    return row


def stop(index: int) -> dict:
    pid = _read_pid(index)
    if pid:
        owned = _descendants(pid)
        for owned_pid in sorted(owned, reverse=True):
            try:
                os.kill(owned_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _ in range(30):
            remaining = [owned_pid for owned_pid in owned if Path(f"/proc/{owned_pid}").exists()]
            if not remaining:
                break
            time.sleep(1)
        for owned_pid in owned:
            if Path(f"/proc/{owned_pid}").exists():
                try:
                    os.kill(owned_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    _pid_file(index).unlink(missing_ok=True)
    return _target(index)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["inventory", "start", "unload", "cleanup", "stop"])
    parser.add_argument("--gpu", type=int)
    args = parser.parse_args()
    if args.action == "inventory":
        result = inventory()
    else:
        if args.gpu is None:
            parser.error("--gpu is required")
        result = globals()[args.action](args.gpu)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
