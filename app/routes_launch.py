# app/routes_launch.py
"""
FastAPI router for /api/launch/*
Register with:  app.include_router(launch_router)  in main.py
"""

import re
import json
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
    build_minimization_script,
    build_equilibration_script,
    build_production_script,
    write_run_script,
)

from . import queue_worker as qw

launch_router = APIRouter(prefix="/api/launch", tags=["launch"])
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# POST /api/launch/solvate
# ---------------------------------------------------------------------------
@launch_router.post("/solvate")
async def launch_solvate(
    run_name:        str        = Form(...),
    water_model:     str        = Form(...),
    box_mode:        str        = Form(...),
    box_x:           float      = Form(None),
    box_y:           float      = Form(None),
    box_z:           float      = Form(None),
    skip_hydration:  str        = Form("0"),
    # Minimization
    minim_algo:      str        = Form("steep"),
    minim_nsteps:    int        = Form(10000),
    minim_emtol:     float      = Form(50.0),
    minim_extra:     str        = Form(""),
    # Equilibration: JSON list of step dicts
    equil_steps:     str        = Form("[]"),
    # Production
    prod_ensemble:   str        = Form("NPT"),
    prod_nsteps:     int        = Form(100000),
    prod_dt:         float      = Form(1.0),
    prod_temp:       float      = Form(300.0),
    prod_pres:       float      = Form(1.0),
    prod_nstxout:    float      = Form(10.0),
    prod_aniso:      str        = Form("0"),
    prod_extra:      str        = Form(""),
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

    # ── Inject water topology ──────────────────────────────────────────
    try:
        inject_water_topology(top_path, water_model)
    except Exception as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(500, f"Error inyectando topología de agua: {exc}")

    # ── Parse equilibration steps ──────────────────────────────────────
    try:
        equil_list = json.loads(equil_steps)
    except Exception:
        raise HTTPException(400, "equil_steps JSON inválido.")

    # Mark the first dynamics run (gen_vel = yes)
    if equil_list:
        equil_list[0]["is_first"] = True
        for s in equil_list[1:]:
            s["is_first"] = False
    
    last_equil_label = equil_list[-1]["label"] if equil_list else None

    # ── Build sections ─────────────────────────────────────────────────
    box_xyz = (box_x, box_y, box_z) if box_mode == "resize" else None

    sections = []

    # 1. Solvation
    sections.append(build_solvation_script(
        run_name    = run_name,
        water_model = water_model,
        box_mode    = box_mode,
        box_xyz     = box_xyz,
        skip        = skip_hydration == "1",
    ))

    # 2. Minimization
    in_gro  = f'"{stab_dir}/start.gro"'
    out_gro = f'"{stab_dir}/EM.gro"'
    sections.append(build_minimization_script(
        run_name  = run_name,
        label     = "EM",
        algo      = minim_algo,
        nsteps    = minim_nsteps,
        emtol     = minim_emtol,
        in_gro    = in_gro,
        out_gro   = out_gro,
        extra_mdp = minim_extra,
    ))

    # 3. Equilibration
    if equil_list:
        sections.append(build_equilibration_script(
            run_name = run_name,
            steps    = equil_list,
        ))

    # 4. Production
    sections.append(build_production_script(
        run_name         = run_name,
        ensemble         = prod_ensemble,
        nsteps           = prod_nsteps,
        dt_fs            = prod_dt,
        temp             = prod_temp,
        pres             = prod_pres,
        nstxout_ps       = prod_nstxout,
        aniso            = prod_aniso == "1",
        extra_mdp        = prod_extra,
        last_equil_label = last_equil_label,
    ))

    script_path = write_run_script(run_name, sections)

    # ── Enqueue ────────────────────────────────────────────────────────
    job = qw.enqueue(run_name, script_path)
    return JSONResponse({"status": "queued", "job_id": job["id"], "run": run_name})


# ---------------------------------------------------------------------------
# GET /api/launch/{run_name}/stream  — SSE progress log
# ---------------------------------------------------------------------------
@launch_router.get("/{run_name}/stream")
async def stream_progress(run_name: str):
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", run_name):
        raise HTTPException(400, "Run name inválido.")

    progress_log = DATA_DIR / run_name / "progress.log"

    async def event_generator():
        for _ in range(50):
            if progress_log.exists():
                break
            await asyncio.sleep(0.2)
        else:
            yield "event: error\ndata: progress.log not found — did the script start?\n\n"
            return

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
                    yield f"event: error_detail\ndata: {text}\n\n"
                    pos = fh.tell()
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
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
