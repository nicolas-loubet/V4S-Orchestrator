#!/usr/bin/env python3
"""
app/static/scripts/adapt_legacy_system.py

Adapta una carpeta de corrida ya hecha (con una version anterior del
pipeline, o armada a mano) para que aparezca en "Elegir Sistema", sin
tener que volver a simular nada.

Que hace:
1. Si hay conf-*.gro sueltos en la raiz de la carpeta, los mueve a
   estabilizacion/confs/ (donde el pipeline actual los espera, y donde
   _ensure_xyz() los busca para la vista previa 3D).
2. Lee estabilizacion/PROD.mdp para sacar nsteps, dt y
   nstxout-compressed, y con eso calcula total_simulated_ns y
   snapshot_interval_ps — no hace falta tipearlos a mano.
3. Si existe confs_min/summary.csv (se corrio minimizacion de confs),
   arma el bloque [dataset.inherent]; si no, lo deja enabled = false.
4. Escribe system.toml en la raiz, con el mismo esquema que genera el
   pipeline automatico (ver pipeline.build_system_toml_script).

Uso:
    python3 adapt_legacy_system.py /ruta/a/data/<carpeta_del_sistema>

Por ejemplo, para el caso de Lisozima:
    python3 adapt_legacy_system.py ~/datastore/2026_V4S-Orchestrator/data/Lisozima
"""

import sys
from pathlib import Path


def parse_mdp(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.split(";", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip().lower()] = val.strip()
    return values


def main() -> None:
    if len(sys.argv) != 2:
        print("Uso: python3 adapt_legacy_system.py /ruta/a/data/<sistema>")
        sys.exit(1)

    run_dir   = Path(sys.argv[1]).expanduser().resolve()
    stab_dir  = run_dir / "estabilizacion"
    confs_dir = stab_dir / "confs"
    min_dir   = run_dir / "confs_min"
    prod_mdp  = stab_dir / "PROD.mdp"

    if not run_dir.is_dir():
        sys.exit(f"No existe la carpeta: {run_dir}")
    if not prod_mdp.exists():
        sys.exit(f"No encontré {prod_mdp} — ¿la ruta es la carpeta del sistema, "
                  f"no la de estabilizacion/?")

    # ── 1. Mover conf-*.gro sueltos de la raíz, si los hay ─────────────
    confs_dir.mkdir(parents=True, exist_ok=True)
    loose = sorted(run_dir.glob("conf-*.gro"))
    if loose:
        print(f"Moviendo {len(loose)} conf-*.gro sueltos de la raíz a {confs_dir}/ ...")
        for f in loose:
            f.rename(confs_dir / f.name)
    else:
        print("No había conf-*.gro sueltos en la raíz (ya estaban en estabilizacion/confs/, u otra cosa).")

    n_confs = len(list(confs_dir.glob("conf-*.gro")))
    if n_confs == 0:
        sys.exit(f"No hay ningún conf-*.gro en {confs_dir} — no hay nada que registrar todavía.")
    print(f"conf-*.gro en estabilizacion/confs/: {n_confs}")

    # ── 2. Leer PROD.mdp para derivar tiempo total e intervalo ─────────
    mdp = parse_mdp(prod_mdp)
    try:
        nsteps = int(float(mdp["nsteps"]))
        dt_ps  = float(mdp["dt"])
    except KeyError as e:
        sys.exit(f"Al PROD.mdp le falta el campo {e} — no puedo calcular el tiempo simulado.")

    nstxout_comp = int(float(mdp.get("nstxout-compressed", 1)))
    ensemble     = "NPT" if mdp.get("pcoupl", "no").lower() != "no" else "NVT"

    total_simulated_ns   = nsteps * dt_ps / 1000.0
    snapshot_interval_ps = nstxout_comp * dt_ps

    print(f"De PROD.mdp: nsteps={nsteps}  dt={dt_ps}ps  nstxout-compressed={nstxout_comp}  "
          f"pcoupl={mdp.get('pcoupl', 'no')} → ensemble={ensemble}")
    print(f"→ total_simulated_ns={total_simulated_ns:.4f}  snapshot_interval_ps={snapshot_interval_ps:.4f}")

    # ── 3. Dataset inherente, si ya se minimizaron confs ────────────────
    summary = min_dir / "summary.csv"
    if summary.exists():
        rows   = [r for r in summary.read_text().splitlines()[1:] if r.strip()]
        n_min  = len(rows)
        n_conv = sum(1 for r in rows if r.rstrip().endswith(",ok"))
        inherent_block = (
            f'[dataset.inherent]\n'
            f'enabled = true\n'
            f'path = "confs_min"\n'
            f'prefix = "em-"\n'
            f'n_confs = {n_min}\n'
            f'n_converged = {n_conv}\n'
            f'summary = "confs_min/summary.csv"'
        )
        print(f"Encontré confs_min/summary.csv: {n_conv}/{n_min} confs minimizados.")
    else:
        inherent_block = '[dataset.inherent]\nenabled = false'
        print("No hay confs_min/ — se registra solo el dataset real (sin minimización).")

    # ── 4. Escribir system.toml ─────────────────────────────────────────
    toml_content = f'''[info]
name = "{run_dir.name}"
description = "Importado a mano desde una corrida ya existente."

[simulation]
total_simulated_ns = {total_simulated_ns}
snapshot_interval_ps = {snapshot_interval_ps}
ensemble = "{ensemble}"

[dataset.real]
path = "estabilizacion/confs"
prefix = "conf-"
n_confs = {n_confs}

{inherent_block}
'''
    (run_dir / "system.toml").write_text(toml_content)
    print(f"\nEscribí {run_dir / 'system.toml'}:\n")
    print(toml_content)
    print("Listo — debería aparecer en 'Elegir Sistema' la próxima vez que cargues ese tab.")


if __name__ == "__main__":
    main()
