# V4S-Orchestrator

Versión actual: 1.1.1

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
└── V4S-Orchestrator/              # Repositorio Git
    ├── app/
    │   ├── __init__.py
    │   ├── main.py                # Backend FastAPI
    │   └── templates/
    │       ├── base.html
    │       ├── dashboard.html
    │       └── components/        # Vistas modulares para HTMX
    ├── static/                    # Archivos CSS/JS
    ├── requirements.txt
    └── README.md
```

---

## Flujo de Trabajo en la Interfaz

El dashboard está dividido en tres secciones modulares:

- Elegir Sistema: Selección de archivos de estructura y topología (.gro / .top) disponibles en la carpeta local de datos.
- Configurar Cálculo: Formulario dinámico para parametrizar el índice estructural (V1S a V4S / omega_m), geometría del cálculo y temporalidad.
- Visualización: Generación de gráficos interactivos mediante Plotly a partir de los archivos .csv procesados localmente.

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
