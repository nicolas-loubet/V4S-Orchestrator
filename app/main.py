import json
import uvicorn
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
    return templates.TemplateResponse(template_name, ctx)


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
