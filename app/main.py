import os
import uvicorn
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR  = Path(__file__).resolve().parent.parent   # V4S-Orchestrator/
DATA_PATH = BASE_DIR.parent / "data"                 # ../data/

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

app = FastAPI(title="V4S-Orchestrator")

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# ---------------------------------------------------------------------------
# Startup checks
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
# Route helpers  (lugar donde se inyectara logica de procesamiento a futuro)
# ---------------------------------------------------------------------------

def _render(template_name: str, request: Request, context: dict | None = None) -> HTMLResponse:
    ctx = {"request": request}
    if context:
        ctx.update(context)
    return templates.TemplateResponse(template_name, ctx)

# ---------------------------------------------------------------------------
# Main routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return _render("dashboard.html", request)

# ---------------------------------------------------------------------------
# Tab routes
# ---------------------------------------------------------------------------

@app.get("/tabs/sistema", response_class=HTMLResponse)
async def tab_sistema(request: Request):
    # TODO: cargar lista de sistemas desde DATA_PATH
    return _render("components/tab_sistema.html", request)


@app.get("/tabs/calculo", response_class=HTMLResponse)
async def tab_calculo(request: Request):
    # TODO: leer parametros de simulacion desde DATA_PATH
    return _render("components/tab_calculo.html", request)


@app.get("/tabs/visualizacion", response_class=HTMLResponse)
async def tab_visualizacion(request: Request):
    # TODO: recolectar resultados de trayectorias desde DATA_PATH
    return _render("components/tab_visualizacion.html", request)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=5000,
        reload=True,
    )
