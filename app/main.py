import json
import uvicorn
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
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


@app.on_event("startup")
async def startup_checks() -> None:
    if DATA_PATH.exists():
        print(f"[V4S] DATA_PATH encontrado: {DATA_PATH}")
    else:
        print(f"[V4S] AVISO: La carpeta de datos no existe en '{DATA_PATH}'.")
        print( "[V4S]        El servidor continua, pero las funciones que lean")
        print( "[V4S]        archivos externos fallaran hasta que la crees.")


def _render(template_name: str, request: Request, context: dict | None = None) -> HTMLResponse:
    ctx = {"request": request}
    if context:
        ctx.update(context)
    return templates.TemplateResponse(template_name, ctx)


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
                sistemas.append({
                    "id":          folder.name,
                    "name":        meta.get("info", {}).get("name", folder.name),
                    "description": meta.get("info", {}).get("description", "Sin descripcion."),
                    "total_ns":    meta.get("simulation", {}).get("total_simulated_ns", 0.0),
                    "interval_ps": meta.get("simulation", {}).get("snapshot_interval_ps", 0.0),
                    "ensemble":    meta.get("simulation", {}).get("ensemble", "N/A"),
                })
            except Exception as e:
                print(f"[V4S] Error leyendo {toml_file}: {e}")

    return _render("components/tab_sistema.html", request, {
        "sistemas": sistemas,
        "sistemas_json": json.dumps({s["id"]: s for s in sistemas}, ensure_ascii=False),
    })


@app.get("/tabs/calculo", response_class=HTMLResponse)
async def tab_calculo(request: Request):
    return _render("components/tab_calculo.html", request)


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
