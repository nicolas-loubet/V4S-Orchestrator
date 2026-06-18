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
    """Return all jobs sorted by id."""
    return JSONResponse(qw.list_jobs())


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
        for _ in range(50):
            if progress_log.exists():
                break
            await asyncio.sleep(0.2)
        else:
            yield "event: error\ndata: progress.log not found\n\n"
            return

        error_mode = False
        with progress_log.open() as fh:
            while True:
                line = fh.readline()
                if not line:
                    # If job is no longer running, stop streaming
                    current = qw.get_job(job_id)
                    if current and current["status"] in ("done", "failed", "cancelled"):
                        return
                    await asyncio.sleep(0.3)
                    continue

                text = line.rstrip("\n")

                if text == "PIPELINE_START":
                    yield "event: start\ndata: Pipeline iniciado\n\n"
                elif text == "PIPELINE_DONE":
                    yield "event: done\ndata: Pipeline completado\n\n"
                    return
                elif text == "PIPELINE_ERROR":
                    error_mode = True
                    yield "event: error\ndata: El pipeline falló\n\n"
                elif error_mode:
                    yield f"event: error_detail\ndata: {text}\n\n"
                    pos  = fh.tell()
                    peek = fh.readline()
                    if not peek:
                        await asyncio.sleep(1.0)
                        peek = fh.readline()
                        if not peek:
                            return
                    fh.seek(pos if not peek else fh.tell() - len(peek))
                else:
                    yield f"data: {text}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
