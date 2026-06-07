# app/routes_launch.py
"""
FastAPI router for /api/launch/*
Register with:  app.include_router(launch_router)  in main.py
"""

import re
import shutil
import asyncio
import logging
from pathlib import Path

from fastapi           import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .pipeline import (
    DATA_DIR,
    inject_water_topology,
    build_solvation_script,
    write_run_script,
)

launch_router = APIRouter(prefix="/api/launch", tags=["launch"])
log = logging.getLogger(__name__)

SCREEN_NAME = "V4SOrch"


# ---------------------------------------------------------------------------
# POST /api/launch/solvate
# ---------------------------------------------------------------------------
@launch_router.post("/solvate")
async def launch_solvate(
    run_name:    str        = Form(...),
    water_model: str        = Form(...),
    box_mode:        str        = Form(...),
    box_x:           float      = Form(None),
    box_y:           float      = Form(None),
    box_z:           float      = Form(None),
    skip_hydration:  str        = Form("0"),   # "1" = skip
    gro_file:        UploadFile = File(...),
    top_file:        UploadFile = File(...),
):
    # ── Validate ───────────────────────────────────────────────────────
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", run_name):
        raise HTTPException(400, "Nombre de run inválido.")
    if water_model not in ("TIP3P", "SPC/E", "TIP4P/2005", "TIP5P/2018"):
        raise HTTPException(400, "Modelo de agua no reconocido.")

    run_dir  = DATA_DIR / run_name
    stab_dir = run_dir / "estabilizacion"

    if run_dir.exists():
        raise HTTPException(409, f"El run '{run_name}' ya existe.")

    # ── Create dirs & save files ───────────────────────────────────────
    stab_dir.mkdir(parents=True)

    gro_path = stab_dir / "system.gro"
    top_path = stab_dir / "system.top"

    for upload, dest in [(gro_file, gro_path), (top_file, top_path)]:
        with dest.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)

    # ── Inject water topology into system.top ──────────────────────────
    try:
        inject_water_topology(top_path, water_model)
    except Exception as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(500, f"Error inyectando topología de agua: {exc}")

    # ── Build run.sh ───────────────────────────────────────────────────
    box_xyz = (box_x, box_y, box_z) if box_mode == "resize" else None
    solvation_cmds = build_solvation_script(
        run_name    = run_name,
        water_model = water_model,
        box_mode    = box_mode,
        box_xyz     = box_xyz,
        skip        = skip_hydration == "1",
    )
    script_path = write_run_script(run_name, [solvation_cmds])

    # ── Launch in screen ───────────────────────────────────────────────
    # screen -S V4SOrch -X stuff sends keystrokes to the existing screen session.
    # We pass "bash /path/to/run.sh\n" so it runs in the foreground of that screen.
    cmd = f"bash {script_path}\n"
    proc = await asyncio.create_subprocess_exec(
        "screen", "-S", SCREEN_NAME, "-X", "stuff", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        log.error("screen launch failed for '%s': %s", run_name, err)
        raise HTTPException(500, detail={
            "error": f"No se pudo enviar al screen '{SCREEN_NAME}'. "
                     f"¿Está corriendo? ({err.strip()})"
        })

    return JSONResponse({"status": "launched", "run": run_name})


# ---------------------------------------------------------------------------
# GET /api/launch/{run_name}/stream  — SSE progress log
# ---------------------------------------------------------------------------
@launch_router.get("/{run_name}/stream")
async def stream_progress(run_name: str):
    """
    Server-Sent Events stream of progress.log.
    Sends each new line as:  data: <line>
    Special sentinels emitted by run.sh:
      PIPELINE_START  → event: start
      PIPELINE_DONE   → event: done   (closes stream)
      PIPELINE_ERROR  → event: error  (closes stream, next lines are gmx tail)
    """
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", run_name):
        raise HTTPException(400, "Run name inválido.")

    progress_log = DATA_DIR / run_name / "progress.log"

    async def event_generator():
        # Wait up to 10 s for the script to create progress.log
        for _ in range(50):
            if progress_log.exists():
                break
            await asyncio.sleep(0.2)
        else:
            yield "event: error\ndata: progress.log not found — did the script start?\n\n"
            return

        # tail -f equivalent: read line by line, yield as SSE
        error_mode = False
        with progress_log.open() as fh:
            while True:
                line = fh.readline()
                if not line:
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
                    # Lines after PIPELINE_ERROR are the gmx tail
                    yield f"event: error_detail\ndata: {text}\n\n"
                    # Check if we've received the last tail line
                    # (run.sh finishes after printing them)
                    pos = fh.tell()
                    peek = fh.readline()
                    if not peek:
                        # Nothing more — script is done
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
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if present
        },
    )
