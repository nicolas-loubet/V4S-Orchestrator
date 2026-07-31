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
    build_confs_minimization_script,
    build_system_toml_script,
    compute_n_confs,
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
    # Minimización de confs (post-producción, escalera de niveles)
    confmin_enabled: str        = Form("0"),
    confmin_posres:  str        = Form("-DPOSRES"),
    confmin_emtol1:  float      = Form(12.0),
    confmin_nsteps1: int        = Form(1000000000000),
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
        log.exception("Fallo inyectando topología de agua para run '%s'", run_name)
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

    # 5. Minimización de confs (opcional — post-producción, escalera de niveles)
    if confmin_enabled == "1":
        sections.append(build_confs_minimization_script(
            run_name         = run_name,
            prod_nsteps      = prod_nsteps,
            prod_dt_fs       = prod_dt,
            prod_nstxout_ps  = prod_nstxout,
            last_equil_label = last_equil_label,
            posres_define    = confmin_posres,
            emtol1           = confmin_emtol1,
            nsteps1          = confmin_nsteps1,
        ))

    # 6. system.toml — SIEMPRE se escribe, con o sin minimización. Es lo que
    # hace que la corrida aparezca en "Elegir Sistema" al terminar.
    n_confs_expected = compute_n_confs(prod_nsteps, prod_dt, prod_nstxout)
    sections.append(build_system_toml_script(
        run_name             = run_name,
        total_simulated_ns   = prod_nsteps * prod_dt / 1_000_000,  # nsteps * dt(fs) / 1e6 = ns
        snapshot_interval_ps = prod_nstxout,
        ensemble             = prod_ensemble,
        n_confs              = n_confs_expected,
        confmin_enabled      = confmin_enabled == "1",
    ))

    script_path = write_run_script(run_name, sections)

    # ── Enqueue ────────────────────────────────────────────────────────
    job = qw.enqueue(run_name, script_path, meta={
        "confmin_enabled":   confmin_enabled == "1",
        "n_confs_expected":  n_confs_expected,
    })
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
      try:
        # Esperar a que el job arranque de verdad. Puede tardar por el
        # colchón de idle de gmx_gpu (60s) o porque hay otra corrida
        # ocupando la GPU — así que toleramos varios minutos, no 10s,
        # e informamos el estado mientras tanto en vez de morir en silencio.
        last_status = None
        for i in range(600):  # hasta 10 minutos (600 x 1s)
            if progress_log.exists():
                break

            job = qw.get_job_by_run_name(run_name)
            if job is None:
                yield "event: pipeline_error\ndata: No se encontró el job en la cola.\n\n"
                return
            if job["status"] != last_status:
                if job["status"] == "pending":
                    yield "event: queued\ndata: En cola, esperando turno (GPU ocupada o en período de resguardo)...\n\n"
                elif job["status"] == "failed":
                    yield "event: pipeline_error\ndata: El job fue marcado como fallido antes de generar progress.log (revisá el log del servidor).\n\n"
                    return
                elif job["status"] == "cancelled":
                    yield "event: pipeline_error\ndata: El job fue cancelado.\n\n"
                    return
                last_status = job["status"]

            await asyncio.sleep(1.0)
        else:
            yield "event: pipeline_error\ndata: progress.log not found — did the script start?\n\n"
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
        log.exception("Excepción no prevista en el stream SSE de '%s'", run_name)
        yield f"event: pipeline_error\ndata: Error interno en el servidor mientras se streameaba el progreso: {exc}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
