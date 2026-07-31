# app/routes_queue.py
"""
FastAPI router for /api/queue/*
Exposes job list, cancel, and SSE stream for a running job.
"""

import re
import asyncio
import logging
from fastapi           import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from . import queue_worker as qw
from .pipeline import DATA_DIR

queue_router = APIRouter(prefix="/api/queue", tags=["queue"])
log = logging.getLogger(__name__)


@queue_router.get("")
async def list_queue():
    """Return all jobs sorted by id, con el estado de minimización de confs
    (sin minimización pedida / pendiente / minimizado) adjunto a cada uno."""
    jobs = qw.list_jobs()
    for job in jobs:
        job["confmin"] = qw.confmin_status(job)
    return JSONResponse(jobs)


@queue_router.delete("/{job_id}")
async def cancel_job(job_id: str):
    job = qw.cancel_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    if job["status"] != "cancelled":
        raise HTTPException(409, f"Job is '{job['status']}' and cannot be cancelled.")
    return JSONResponse(job)


@queue_router.get("/{job_id}/stream")
async def stream_job(job_id: str):
    """SSE stream of progress.log for a running or recently finished job."""
    job = qw.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")

    run_name     = job["run_name"]
    progress_log = DATA_DIR / run_name / "progress.log"

    async def event_generator():
      try:
        for _ in range(50):
            if progress_log.exists():
                break
            await asyncio.sleep(0.2)
        else:
            yield "event: pipeline_error\ndata: progress.log not found\n\n"
            return

        error_mode  = False
        quiet_ticks = 0
        with progress_log.open() as fh:
            while True:
                line = fh.readline()
                if not line:
                    if error_mode:
                        quiet_ticks += 1
                        if quiet_ticks >= 10:   # ~3s sin líneas nuevas tras el error: ya se mandó todo
                            return
                    else:
                        # Si el job ya terminó y no estamos en medio de un
                        # bloque de error, no tiene sentido seguir esperando.
                        current = qw.get_job(job_id)
                        if current and current["status"] in ("done", "failed", "cancelled"):
                            return
                    await asyncio.sleep(0.3)
                    continue
                quiet_ticks = 0

                text = line.rstrip("\n")

                if text == "PIPELINE_START":
                    yield "event: start\ndata: Pipeline iniciado\n\n"
                elif text == "PIPELINE_DONE":
                    yield "event: done\ndata: Pipeline completado\n\n"
                    return
                elif text == "PIPELINE_ERROR":
                    error_mode = True
                    yield "event: pipeline_error\ndata: El pipeline falló\n\n"
                elif error_mode:
                    yield f"event: error_detail\ndata: {text}\n\n"
                else:
                    yield f"data: {text}\n\n"
      except Exception as exc:
        log.exception("Excepción no prevista en el stream SSE del job '%s'", job_id)
        yield f"event: pipeline_error\ndata: Error interno en el servidor mientras se streameaba el progreso: {exc}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
