# V4S-Orchestrator

Versión actual: 1.0.4

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
