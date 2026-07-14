# app/queue_worker.py
"""
Queue worker for V4S-Orchestrator.
- Jobs are stored as JSON files in ../data/queue/<id>_<run_name>.json
- The worker loop runs as an asyncio background task (started via lifespan).
- Every 10 minutes it checks if gmx_gpu has been idle for >= 60 seconds.
  If the coast is clear, it picks the oldest pending job and launches it.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Injected at startup from pipeline.py values
QUEUE_DIR:   Path | None = None
SCREEN_NAME: str = "V4SOrch"

CHECK_INTERVAL_S  = 600   # poll de resguardo cada 10 minutos (por si se pierde algún wake-up)
IDLE_REQUIRED_S   = 60    # gmx_gpu must have been gone for this long

# Se dispara cada vez que se encola un job nuevo, para que el worker
# lo revise ya mismo en vez de esperar el próximo ciclo de CHECK_INTERVAL_S.
_new_job_event = asyncio.Event()


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def queue_dir() -> Path:
    assert QUEUE_DIR is not None, "QUEUE_DIR not set"
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return QUEUE_DIR


def _next_id() -> str:
    existing = sorted(queue_dir().glob("*.json"))
    if not existing:
        return "001"
    last = int(existing[-1].stem.split("_")[0])
    return f"{last + 1:03d}"


def enqueue(run_name: str, script_path: Path) -> dict:
    """Create a pending job JSON and return the job dict."""
    job_id   = _next_id()
    filename = f"{job_id}_{run_name}.json"
    job = {
        "id":          job_id,
        "run_name":    run_name,
        "script_path": str(script_path),
        "status":      "pending",
        "created_at":  datetime.now(timezone.utc).isoformat(),
        "started_at":  None,
        "finished_at": None,
    }
    (queue_dir() / filename).write_text(json.dumps(job, indent=2))
    log.info("Enqueued job %s → %s", job_id, run_name)
    _new_job_event.set()   # despertar al worker ya
    return job


def get_job_by_run_name(run_name: str) -> dict | None:
    matches = list(queue_dir().glob(f"*_{run_name}.json"))
    if not matches:
        return None
    try:
        return json.loads(matches[0].read_text())
    except Exception:
        return None


def _job_path(job_id: str, run_name: str) -> Path | None:
    matches = list(queue_dir().glob(f"{job_id}_*.json"))
    return matches[0] if matches else None


def list_jobs() -> list[dict]:
    jobs = []
    for p in sorted(queue_dir().glob("*.json")):
        try:
            jobs.append(json.loads(p.read_text()))
        except Exception:
            pass
    return jobs


def get_job(job_id: str) -> dict | None:
    matches = list(queue_dir().glob(f"{job_id}_*.json"))
    if not matches:
        return None
    try:
        return json.loads(matches[0].read_text())
    except Exception:
        return None


def _update_job(job: dict) -> None:
    matches = list(queue_dir().glob(f"{job['id']}_*.json"))
    if matches:
        matches[0].write_text(json.dumps(job, indent=2))


def cancel_job(job_id: str) -> dict | None:
    """Cancel a pending job. Running jobs cannot be cancelled here."""
    job = get_job(job_id)
    if job is None:
        return None
    if job["status"] != "pending":
        return job   # caller decides what to do
    job["status"]      = "cancelled"
    job["finished_at"] = datetime.now(timezone.utc).isoformat()
    _update_job(job)
    return job


# ---------------------------------------------------------------------------
# gmx_gpu idle detection
# ---------------------------------------------------------------------------

_last_gmx_seen: float = 0.0   # epoch seconds


async def _gmx_is_running() -> bool:
    """Return True if any gmx_gpu process exists right now."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-x", "gmx_gpu",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def _gmx_idle_long_enough() -> bool:
    """
    Return True only if gmx_gpu has been absent for at least IDLE_REQUIRED_S.
    Updates _last_gmx_seen whenever it is found running.
    """
    global _last_gmx_seen
    if await _gmx_is_running():
        _last_gmx_seen = time.monotonic()
        return False
    elapsed = time.monotonic() - _last_gmx_seen
    return elapsed >= IDLE_REQUIRED_S


# ---------------------------------------------------------------------------
# Launch a job into the existing screen session
# ---------------------------------------------------------------------------

async def _launch_job(job: dict) -> bool:
    """Send run.sh to the screen. Returns True on success."""
    script = job["script_path"]
    cmd    = f"bash {script}\n"

    proc = await asyncio.create_subprocess_exec(
        "screen", "-S", SCREEN_NAME, "-X", "stuff", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        log.error("screen launch failed for job %s: %s",
                  job["id"], stderr.decode(errors="replace"))
        return False
    return True


async def _wait_for_completion(job: dict) -> str:
    """
    Tail progress.log until PIPELINE_DONE or PIPELINE_ERROR.
    Returns "done" or "failed".
    Yields control to the event loop frequently — no busy-wait.
    """
    from .pipeline import DATA_DIR
    progress_log = DATA_DIR / job["run_name"] / "progress.log"

    # Wait up to 30 s for the file to appear
    for _ in range(150):
        if progress_log.exists():
            break
        await asyncio.sleep(0.2)
    else:
        log.error("progress.log never appeared for job %s", job["id"])
        return "failed"

    with progress_log.open() as fh:
        while True:
            line = fh.readline()
            if not line:
                await asyncio.sleep(1.0)
                continue
            text = line.strip()
            if text == "PIPELINE_DONE":
                return "done"
            if text == "PIPELINE_ERROR":
                return "failed"


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------

async def worker_loop() -> None:
    """Background task — runs forever alongside FastAPI."""
    log.info("Queue worker started (check-interval fallback %ds, idle threshold %ds)",
             CHECK_INTERVAL_S, IDLE_REQUIRED_S)

    # Initialise _last_gmx_seen so we don't fire immediately at startup
    global _last_gmx_seen
    _last_gmx_seen = time.monotonic()

    while True:
        # Esperar a que llegue un job nuevo (enqueue() dispara el evento) o,
        # como red de seguridad, revisar igual cada CHECK_INTERVAL_S por si
        # el evento se perdió por algún motivo.
        try:
            await asyncio.wait_for(_new_job_event.wait(), timeout=CHECK_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        _new_job_event.clear()

        try:
            # Find oldest pending job
            pending = [j for j in list_jobs() if j["status"] == "pending"]
            if not pending:
                continue

            if not await _gmx_idle_long_enough():
                log.debug("gmx_gpu still active or not idle long enough — skipping")
                # Nos volvemos a despertar en breve para reintentar sin
                # esperar los 10 minutos completos del check de resguardo.
                asyncio.get_event_loop().call_later(5, _new_job_event.set)
                continue

            job = pending[0]
            log.info("Launching job %s (%s)", job["id"], job["run_name"])

            job["status"]     = "running"
            job["started_at"] = datetime.now(timezone.utc).isoformat()
            _update_job(job)

            ok = await _launch_job(job)
            if not ok:
                job["status"]      = "failed"
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                _update_job(job)
                continue

            result = await _wait_for_completion(job)
            job["status"]      = result        # "done" | "failed"
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            _update_job(job)
            log.info("Job %s finished with status: %s", job["id"], result)

            # Si quedan mas jobs pendientes, procesarlos ya sin esperar
            # otro ciclo completo.
            if any(j["status"] == "pending" for j in list_jobs()):
                _new_job_event.set()

        except Exception as exc:
            log.exception("Unexpected error in worker loop: %s", exc)
