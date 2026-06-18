import json
import shutil
import subprocess
import tomli_w
import uvicorn
from datetime import datetime, timezone
from fastapi import Body
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

try:
    import tomllib
except ImportError:
    import tomli as tomllib

BASE_DIR  = Path(__file__).resolve().parent.parent
DATA_PATH = BASE_DIR.parent / "data"

app = FastAPI(title="V4S-Orchestrator")

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


# ---------------------------------------------------------------------------
# GRO parser + XYZ generator
# ---------------------------------------------------------------------------

def _parse_gro(gro_path: Path) -> tuple[list[dict], dict]:
    """
    Lee un archivo .gro y devuelve (atomos, box).
    atomos: lista de dicts con keys symbol, name, x, y, z (en Angstroms).
    box:    dict con xmin/xmax/ymin/ymax/zmin/zmax (en Angstroms).
    """
    atoms = []
    with open(gro_path, "r") as f:
        lines = f.readlines()

    try:
        n_atoms = int(lines[1].strip())
    except (IndexError, ValueError):
        return atoms, {}

    for line in lines[2 : 2 + n_atoms]:
        if len(line) < 44:
            continue
        res_name  = line[5:10].strip()
        if res_name in ("WAT", "SOL"):
            continue
        atom_name = line[10:15].strip()
        symbol    = atom_name[0] if atom_name else "X"
        try:
            x = float(line[20:28]) * 10.0
            y = float(line[28:36]) * 10.0
            z = float(line[36:44]) * 10.0
        except ValueError:
            continue
        atoms.append({"symbol": symbol, "name": atom_name, "x": x, "y": y, "z": z})

    # Ultima linea del GRO: dimensiones de la caja (en nm)
    box = {}
    try:
        box_parts = lines[2 + n_atoms].split()
        bx = float(box_parts[0]) * 10.0
        by = float(box_parts[1]) * 10.0
        bz = float(box_parts[2]) * 10.0
        box = {"xmin": 0.0, "xmax": bx, "ymin": 0.0, "ymax": by, "zmin": 0.0, "zmax": bz}
    except (IndexError, ValueError):
        pass

    return atoms, box


def _write_xyz(atoms: list[dict], box: dict, xyz_path: Path) -> None:
    box_str = (
        f"{box.get('xmin',0):.4f} {box.get('xmax',0):.4f} "
        f"{box.get('ymin',0):.4f} {box.get('ymax',0):.4f} "
        f"{box.get('zmin',0):.4f} {box.get('zmax',0):.4f}"
    )
    with open(xyz_path, "w") as f:
        f.write(f"{len(atoms)}\n")
        f.write(f"box={box_str}\n")
        for a in atoms:
            f.write(f"{a['symbol']:2s}  {a['name']:<6s}  {a['x']:12.6f}  {a['y']:12.6f}  {a['z']:12.6f}\n")


def _ensure_xyz(system_dir: Path) -> bool:
    """
    Genera solute.xyz a partir de conf-0.gro si no existe o si el GRO es mas nuevo.
    Devuelve True si el archivo existe al finalizar.
    """
    gro  = system_dir / "conf-0.gro"
    xyz  = system_dir / "solute.xyz"

    if not gro.exists():
        return xyz.exists()

    needs_regen = (not xyz.exists()) or (gro.stat().st_mtime > xyz.stat().st_mtime)
    if needs_regen:
        try:
            atoms, box = _parse_gro(gro)
            _write_xyz(atoms, box, xyz)
            print(f"[V4S] solute.xyz generado: {xyz} ({len(atoms)} atomos)")
        except Exception as e:
            print(f"[V4S] Error generando solute.xyz para {system_dir.name}: {e}")
            return False

    return xyz.exists()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_checks() -> None:
    if DATA_PATH.exists():
        print(f"[V4S] DATA_PATH encontrado: {DATA_PATH}")
    else:
        print(f"[V4S] AVISO: La carpeta de datos no existe en '{DATA_PATH}'.")
        print( "[V4S]        El servidor continua, pero las funciones que lean")
        print( "[V4S]        archivos externos fallaran hasta que la crees.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render(template_name: str, request: Request, context: dict | None = None) -> HTMLResponse:
    ctx = {"request": request}
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request=ctx['request'], name=template_name, context={k: v for k, v in ctx.items() if k != 'request'})


# ---------------------------------------------------------------------------
# Rutas principales
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return _render("dashboard.html", request)


@app.get("/tabs/sistema", response_class=HTMLResponse)
async def tab_sistema(request: Request):
    sistemas = []

    if DATA_PATH.exists():
        for folder in sorted(DATA_PATH.iterdir()):
            if not folder.is_dir():
                continue
            toml_file = folder / "system.toml"
            if not toml_file.exists():
                continue
            try:
                with open(toml_file, "rb") as f:
                    meta = tomllib.load(f)

                _ensure_xyz(folder)

                sistemas.append({
                    "id":          folder.name,
                    "name":        meta.get("info", {}).get("name", folder.name),
                    "description": meta.get("info", {}).get("description", "Sin descripcion."),
                    "total_ns":    meta.get("simulation", {}).get("total_simulated_ns", 0.0),
                    "interval_ps": meta.get("simulation", {}).get("snapshot_interval_ps", 0.0),
                    "ensemble":    meta.get("simulation", {}).get("ensemble", "N/A"),
                    "has_xyz":     (folder / "solute.xyz").exists(),
                })
            except Exception as e:
                print(f"[V4S] Error leyendo {toml_file}: {e}")

    return _render("components/tab_sistema.html", request, {
        "sistemas": sistemas,
        "sistemas_json": json.dumps({s["id"]: s for s in sistemas}, ensure_ascii=False),
    })


@app.get("/tabs/calculo", response_class=HTMLResponse)
async def tab_calculo(request: Request):
    sistema_id = request.query_params.get("sistema", "")
    has_xyz    = False
    if sistema_id:
        has_xyz = (DATA_PATH / sistema_id / "solute.xyz").exists()
    return _render("components/tab_calculo.html", request, {
        "sistema_id": sistema_id,
        "has_xyz":    has_xyz,
    })


@app.get("/api/solute/{sistema_id}", response_class=JSONResponse)
async def get_solute(sistema_id: str):
    xyz = DATA_PATH / sistema_id / "solute.xyz"
    if not xyz.exists():
        return JSONResponse({"error": "solute.xyz no encontrado"}, status_code=404)

    atoms = []
    with open(xyz, "r") as f:
        lines = f.readlines()

    # Linea 1 (indice 1): box=xmin xmax ymin ymax zmin zmax
    box = {}
    header = lines[1].strip() if len(lines) > 1 else ""
    if header.startswith("box="):
        try:
            vals = [float(v) for v in header[4:].split()]
            box = {"xmin": vals[0], "xmax": vals[1],
                   "ymin": vals[2], "ymax": vals[3],
                   "zmin": vals[4], "zmax": vals[5]}
        except (IndexError, ValueError):
            pass

    for line in lines[2:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        atoms.append({
            "s": parts[0],
            "n": parts[1],
            "x": float(parts[2]),
            "y": float(parts[3]),
            "z": float(parts[4]),
        })
    return {"atoms": atoms, "box": box}


# ---------------------------------------------------------------------------
# API: programar calculo
# ---------------------------------------------------------------------------

RUNS_DIR   = BASE_DIR / "scheduled-runs"
ENGINE_BIN = BASE_DIR / "engine" / "build" / "v4s"


def _next_run_dir() -> tuple[int, Path]:
    """Devuelve (numero, carpeta) para el proximo run-N, thread-safe."""
    RUNS_DIR.mkdir(exist_ok=True)
    existing = sorted(
        [d for d in RUNS_DIR.iterdir() if d.is_dir() and d.name.startswith("run-")],
        key=lambda d: int(d.name.split("-")[1])
    )
    n = (int(existing[-1].name.split("-")[1]) + 1) if existing else 1
    run_dir = RUNS_DIR / f"run-{n}"
    run_dir.mkdir()
    (run_dir / "results").mkdir()
    return n, run_dir


def _build_run_toml(n: int, sistema_id: str, data: dict) -> dict:
    geo = data.get("geometry", "cube")

    geometry: dict = {"type": geo}
    if geo == "cube":
        geometry.update({
            "xmin": data.get("cube_xmin", 0.0), "xmax": data.get("cube_xmax", 0.0),
            "ymin": data.get("cube_ymin", 0.0), "ymax": data.get("cube_ymax", 0.0),
            "zmin": data.get("cube_zmin", 0.0), "zmax": data.get("cube_zmax", 0.0),
        })
    elif geo == "cylinder":
        geometry.update({
            "axis":   data.get("cyl_axis", "Z"),
            "c1":     data.get("cyl_c1",     0.0),
            "c2":     data.get("cyl_c2",     0.0),
            "radius": data.get("cyl_radius", 0.0),
            "hmin":   data.get("cyl_hmin",   0.0),
            "hmax":   data.get("cyl_hmax",   0.0),
        })
    elif geo == "sphere":
        geometry.update({
            "cx":         data.get("sph_cx",         0.0),
            "cy":         data.get("sph_cy",         0.0),
            "cz":         data.get("sph_cz",         0.0),
            "radius":     data.get("sph_radius",     0.0),
            "autocenter": data.get("sph_autocenter", False),
        })

    return {
        "meta": {
            "run_id":       f"run-{n}",
            "sistema_id":   sistema_id,
            "sistema_path": str(DATA_PATH / sistema_id),
            "created_at":   datetime.now(timezone.utc).isoformat(),
        },
        "parametros": {
            "params":          data.get("params", []),
            "units":           data.get("units", "kJ/mol"),
            "output_mode":     data.get("output_mode", "mean"),
            "save_mol_count":  data.get("save_mol_count", False),
        },
        "agregacion": {
            "scope":          data.get("scope", "all"),
            "atom_selection": data.get("atom_selection", ""),
        },
        "geometria": geometry,
    }


@app.post("/api/run", response_class=JSONResponse)
async def schedule_run(data: dict = Body(...)):
    sistema_id = data.get("sistema_id", "")
    if not sistema_id:
        return JSONResponse({"detail": "sistema_id requerido"}, status_code=400)
    if not (DATA_PATH / sistema_id).is_dir():
        return JSONResponse({"detail": f"Sistema no encontrado: {sistema_id}"}, status_code=404)

    n, run_dir = _next_run_dir()
    run_toml   = run_dir / "run.toml"
    doc        = _build_run_toml(n, sistema_id, data)

    # Escribir status inicial
    status_doc = {
        "status": {
            "state":    "pending",
            "progress": 0.0,
            "eta_sec":  0,
            "pid":      0,
            "message":  "En cola",
        }
    }

    try:
        with open(run_toml, "wb") as f:
            tomli_w.dump(doc, f)
        with open(run_dir / "status.toml", "wb") as f:
            tomli_w.dump(status_doc, f)
    except Exception as e:
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)
        return JSONResponse({"detail": f"Error escribiendo TOML: {e}"}, status_code=500)

    # Lanzar el motor C++ como proceso en background
    pid = 0
    if ENGINE_BIN.exists():
        try:
            proc = subprocess.Popen(
                [str(ENGINE_BIN), str(run_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            pid = proc.pid
            # Actualizar status con el PID real
            with open(run_dir / "status.toml", "wb") as f:
                tomli_w.dump({
                    "status": {
                        "state":    "running",
                        "progress": 0.0,
                        "eta_sec":  0,
                        "pid":      pid,
                        "message":  "Iniciando",
                    }
                }, f)
            print(f"[V4S] run-{n} lanzado con PID {pid}")
        except Exception as e:
            print(f"[V4S] Error lanzando motor: {e}")
    else:
        print(f"[V4S] run-{n} programado (motor no compilado en {ENGINE_BIN})")

    return {"run_number": n, "run_id": f"run-{n}", "pid": pid, "path": str(run_dir)}


@app.get("/api/run/{n}/meta", response_class=JSONResponse)
async def get_run_meta(n: int):
    """Devuelve los parametros clave del run.toml para que el frontend sepa qué tipo de output esperar."""
    run_toml = RUNS_DIR / f"run-{n}" / "run.toml"
    if not run_toml.exists():
        return JSONResponse({"detail": "Run no encontrado"}, status_code=404)
    with open(run_toml, "rb") as f:
        doc = tomllib.load(f)

    output_mode = doc.get("parametros", {}).get("output_mode", "mean")
    scope       = doc.get("agregacion", {}).get("scope", "all")
    params      = doc.get("parametros", {}).get("params", [])
    save_n      = doc.get("parametros", {}).get("save_mol_count", False)
    atom_sel    = doc.get("agregacion", {}).get("atom_selection", "")
    atoms       = [a.strip() for a in atom_sel.split() if a.strip()] if atom_sel else []

    # Determinar tipo de output
    is_atoms = (scope == "selection" and len(atoms) > 0)
    if output_mode == "mean" and not is_atoms:
        output_type = "mean_simple"
    elif output_mode == "mean" and is_atoms:
        output_type = "mean_atoms"
    elif output_mode == "time_series" and not is_atoms:
        output_type = "time_simple"
    else:
        output_type = "time_atoms"

    # Leer DT desde system.toml del sistema asociado
    dt = 0.0
    sistema_id = doc.get("meta", {}).get("sistema_id", "")
    if sistema_id:
        sys_toml = DATA_PATH / sistema_id / "system.toml"
        if sys_toml.exists():
            with open(sys_toml, "rb") as f:
                sys_doc = tomllib.load(f)
            dt = sys_doc.get("simulation", {}).get("snapshot_interval_ps", 0.0)

    return {
        "output_type": output_type,
        "params":      params,
        "atoms":       atoms,
        "save_n":      save_n,
        "output_mode": output_mode,
        "scope":       scope,
        "dt":          dt,
    }


@app.get("/api/run/{n}/status", response_class=JSONResponse)
async def get_run_status(n: int):
    run_dir    = RUNS_DIR / f"run-{n}"
    status_file = run_dir / "status.toml"
    if not status_file.exists():
        return JSONResponse({"detail": "Run no encontrado"}, status_code=404)
    with open(status_file, "rb") as f:
        data = tomllib.load(f)
    return data


@app.get("/api/run/{n}/results", response_class=JSONResponse)
async def get_run_results(n: int):
    run_dir     = RUNS_DIR / f"run-{n}"
    results_dir = run_dir / "results"
    if not results_dir.exists():
        return JSONResponse({"detail": "Run no encontrado"}, status_code=404)
    files = [f.name for f in sorted(results_dir.glob("*.csv"))]
    return {"files": files}


@app.get("/api/run/{n}/csv/{filename}", response_class=JSONResponse)
async def get_run_csv(n: int, filename: str):
    import csv
    run_dir  = RUNS_DIR / f"run-{n}"
    # Buscar con y sin prefijo results/
    csv_path = run_dir / "results" / filename
    if not csv_path.exists():
        csv_path = run_dir / filename
    if not csv_path.exists() or csv_path.suffix != ".csv":
        return JSONResponse({"detail": "Archivo no encontrado"}, status_code=404)
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return {"filename": filename, "rows": rows}


@app.post("/api/run/execute", response_class=JSONResponse)
async def execute_code(payload: dict = Body(...)):
    """
    Ejecuta codigo Python del usuario en un namespace restringido.
    Recibe: { code: str, data: { filename: [rows] } }
    Devuelve: { figure_b64, stdout, error }
    """
    import io
    import sys
    import base64
    import traceback
    import pandas as pd
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    code = payload.get("code", "")
    raw  = payload.get("data", {})  # { filename: [ {col: val, ...}, ... ] }

    # Convertir cada CSV a DataFrame con tipos inferidos
    data: dict[str, pd.DataFrame] = {}
    for fname, rows in raw.items():
        if rows:
            df = pd.DataFrame(rows)
            # Intentar convertir columnas numericas automaticamente
            for col in df.columns:
                try:
                    df[col] = pd.to_numeric(df[col])
                except (ValueError, TypeError):
                    pass
            data[fname] = df

    safe_builtins = {
        k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
        for k in ("print", "range", "len", "list", "dict", "tuple", "set",
                  "int", "float", "str", "bool", "min", "max", "sum",
                  "sorted", "enumerate", "zip", "map", "filter", "abs", "round")
        if (isinstance(__builtins__, dict) and k in __builtins__)
           or hasattr(__builtins__, k)
    }

    dt = float(payload.get("dt", 0.0))
    namespace = {
        "__builtins__": safe_builtins,
        "plt":          plt,
        "pd":           pd,
        "np":           np,
        "data":         data,
        "DT":           dt,
    }

    stdout_capture = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout  = stdout_capture

    try:
        plt.rcParams.update({
            'figure.facecolor':  '#0f1520',
            'axes.facecolor':    '#0f1520',
            'axes.edgecolor':    '#1e2d45',
            'axes.labelcolor':   '#94a3b8',
            'axes.titlecolor':   '#cbd5e1',
            'xtick.color':       '#64748b',
            'ytick.color':       '#64748b',
            'grid.color':        '#1e2d45',
            'grid.linestyle':    '--',
            'grid.alpha':        0.6,
            'text.color':        '#cbd5e1',
            'legend.facecolor':  '#0f1520',
            'legend.edgecolor':  '#1e2d45',
            'legend.labelcolor': '#94a3b8',
        })
        plt.figure(figsize=(8, 4.5))
        exec(compile(code, "<user>", "exec"), namespace)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor="#0a0e17", edgecolor="none")
        buf.seek(0)
        fig_b64 = base64.b64encode(buf.read()).decode()
        plt.close("all")
        sys.stdout = old_stdout
        out = stdout_capture.getvalue()
        return {"figure_b64": fig_b64, "stdout": out}
    except Exception:
        plt.close("all")
        sys.stdout = old_stdout
        err = traceback.format_exc()
        err = "\n".join(
            l for l in err.splitlines()
            if "<frozen" not in l and "site-packages" not in l
        )
        return {"error": err}


@app.get("/api/run/{n}/csv/{filename}/raw")
async def download_run_csv(n: int, filename: str):
    from fastapi.responses import FileResponse
    run_dir  = RUNS_DIR / f"run-{n}"
    csv_path = run_dir / "results" / filename
    if not csv_path.exists():
        csv_path = run_dir / filename
    if not csv_path.exists() or csv_path.suffix != ".csv":
        return JSONResponse({"detail": "Archivo no encontrado"}, status_code=404)
    return FileResponse(csv_path, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/tabs/visualizacion", response_class=HTMLResponse)
async def tab_visualizacion(request: Request):
    return _render("components/tab_visualizacion.html", request)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=5000,
        reload=True,
    )
