# V4S-Orchestrator

Versión actual: 1.2.0

V4S-Orchestrator es un sistema de orquestación local diseñado para automatizar el flujo de trabajo de simulaciones en GROMACS y el cálculo del índice de orden estructural mediante binarios de C++.

El sistema centraliza la ejecución, el análisis y la visualización de resultados en una interfaz web única, eliminando la necesidad de conexiones remotas.

---

## Arquitectura del Sistema

El sistema opera bajo una arquitectura de dos capas para una ejecución local eficiente:

1. Frontend (UI Responsive): Una aplicación web (SPA) construida con HTML5, TailwindCSS y HTMX, accesible desde cualquier dispositivo en la red local.
2. Backend (Servidor Local): Servidor FastAPI que corre directamente en el servidor CRIBA. Gestiona la ejecución de binarios de C++, procesa las trayectorias moleculares y genera gráficos interactivos mediante Plotly.

---

## Estructura del Proyecto

```text
.                                  # Nivel de Infraestructura
├── data/                          # Datos de GROMACS y resultados
│   ├── queue/                     # jobs encolados (JSON) — a nivel de data/, no por sistema
│   └── <sistema>/                 # una carpeta por corrida o grupo
│       ├── system.toml            # metadata + datasets (real/inherente) — sistema individual
│       ├── group.toml             # alternativa a system.toml — agrupa varios subsistemas (a mano)
│       ├── solute.xyz             # vista previa 3D, generada automáticamente
│       ├── estabilizacion/        # EM, equilibración, producción (.gro/.top/.mdp/.log/.xtc)
│       │   └── confs/             # conf-N.gro — trayectoria separada en frames (trjconv -sep)
│       └── confs_min/             # opcional — em-N.gro minimizados + summary.csv
└── V4S-Orchestrator/              # Repositorio Git
    ├── app/
    │   ├── __init__.py
    │   ├── main.py                 # Backend FastAPI: tabs, sistemas/grupos, scheduling de cálculo
    │   ├── pipeline.py             # Genera el run.sh de cada dinámica GROMACS (por etapas)
    │   ├── queue_worker.py         # Cola de jobs + worker en background (screen + gmx_gpu)
    │   ├── routes_launch.py        # Router /api/launch/*
    │   ├── routes_queue.py         # Router /api/queue/*
    │   ├── static/
    │   │   ├── scripts/            # utilitarios (fix_pbc_solvent_conf.py, adapt_legacy_system.py, ...)
    │   │   ├── solvents/           # .gro de modelos de agua
    │   │   └── water_topologies/   # .itp + atomtypes.toml de modelos de agua
    │   └── templates/
    │       ├── base.html
    │       ├── dashboard.html
    │       └── components/         # tab_sistema.html, tab_lanzar.html, tab_calculo.html, tab_visualizacion.html
    ├── engine/                     # Motor C++ (cálculo del índice V1S–V4S)
    │   ├── src/
    │   └── build/
    ├── static/                     # Favicon, estáticos globales
    ├── requirements.txt
    └── README.md
```

---

## Flujo de Trabajo en la Interfaz

El dashboard está dividido en cuatro pestañas:

- Elegir Sistema: selección de un sistema (o grupo de subsistemas) ya simulado y disponible en `data/`, como punto de partida para calcular el índice estructural. También permite ver la cola de corridas en curso.
- Lanzar: formulario para lanzar una dinámica GROMACS nueva desde cero (subida de `.gro`/`.top`, modelo de agua, minimización, equilibración, producción, y opcionalmente minimización de confs post-producción).
- Configurar Cálculo: formulario dinámico para parametrizar el índice estructural (V1S a V4S / omega_m), geometría del cálculo, dataset (real o inherente) y temporalidad.
- Visualización: generación de gráficos interactivos mediante Plotly a partir de los archivos .csv procesados localmente.

---

## Instalación

```bash
pip install fastapi uvicorn jinja2 python-multipart tomli tomli-w pandas numpy matplotlib
```

Para compilar el motor C++:

```bash
cd engine
make
```

## Uso

```bash
python3 -m app.main
```

Accedé desde `http://localhost:5000`.

---

## Estructura de un run

Cada cálculo programado genera una carpeta en `scheduled-runs/`:

```
scheduled-runs/run-N/
├── run.toml       # parámetros del cálculo
├── status.toml    # progreso y estado (escrito por el C++)
└── results/       # archivos CSV de salida
```

---

## Datasets y sistemas agrupados (nuevo)

El sistema ahora distingue dos ejes de configuración adicionales al armar un cálculo:

- **Dataset real vs. inherente**: además de la trayectoria de producción cruda ("real"), un sistema puede tener estructuras minimizadas por-frame ("inherente" — cada conf llevado a su mínimo local, filtrando el ruido térmico). Se elige con `[dataset].which = "real" | "inherent"` en `run.toml`; disponibilidad y rutas se declaran en `system.toml` (`[dataset.real]` / `[dataset.inherent]`).
- **Sistemas agrupados**: un estudio con una variable (réplicas, barrido de un parámetro — ej. nanotubos de distinto tamaño) puede armarse a mano como un grupo: una carpeta con `group.toml` que lista subsistemas, cada uno con su propio `system.toml` normal. Se identifica con `[meta].modo = "grupo"` en `run.toml`.

El formato de salida resultante para cada caso todavía se está terminando de definir del lado del motor C++.

---

## Archivos de salida del C++

Los resultados se escriben como CSV en `results/`. El tipo de archivo depende de dos opciones elegidas en la interfaz: **modo de salida** (valores medios o serie temporal) y **ámbito de agregación** (global o por átomo).

### Valores medios · Global — `OutputMeanSimple`

Una sola fila con el promedio sobre todos los frames.

```
V1S,V1S_fMean,V1S_SEM,V4S,V4S_fMean,V4S_SEM,N
-34.07,-34.05,0.29,-14.62,-14.60,0.12,4351
```

- `V#S`: media ponderando cada molécula de agua por igual.
- `V#S_fMean`: media de los promedios por frame.
- `V#S_SEM`: error estándar de esos promedios.

### Valores medios · Por átomo — `OutputMeanAtoms`

Una fila por grupo de átomos. La primera fila es siempre `All`.

```
Atom,V1S,V1S_fMean,V1S_SEM,N
All,-34.07,-34.05,0.29,4351
C20,-34.12,-34.10,0.29,1909
C80,-33.97,-33.95,0.29,1119
```

### Serie temporal · Global — `OutputTimeSimple`

Una fila por frame.

```
conf,V1S,V4S,N
0,-33.91,-14.52,22
1,-34.20,-14.71,21
```

### Serie temporal · Por átomo — `OutputTimeAtoms`

Una fila por frame, con un bloque de columnas por grupo de átomos.

```
conf,All_V1S,All_N,C20_V1S,C20_N
0,-33.91,43,-34.12,19
1,-34.20,42,-34.50,20
```

### Notas

- Las columnas `V#S` presentes dependen de los parámetros activados (V1S–V4S).
- La columna `N` (número de moléculas) aparece solo si se activó "Guardar número de moléculas".
- Si las unidades son adimensionales, los valores se transforman como `x / DIT − 1` (DIT = −6.0).
