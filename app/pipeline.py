# app/pipeline.py
"""
Pipeline builder for V4S-Orchestrator.
Each stage function returns a list of shell command strings.
write_run_script() concatenates them all into run.sh.
"""

from pathlib import Path
import re

GMX      = "gmx_gpu"
APP_DIR  = Path(__file__).resolve().parent          # app/
DATA_DIR = APP_DIR.parent.parent / "data"           # ../../data/  (outside project)
SCRIPTS  = APP_DIR / "static" / "scripts"
SOLVENTS = APP_DIR / "static" / "solvents"
WATER_TOPS = APP_DIR / "static" / "water_topologies"

WATER_MODEL_FILES = {
    "TIP3P":      ("tip3p.gro",      "tip3p.itp",      3),
    "SPC/E":      ("spce.gro",       "spce.itp",       3),
    "TIP4P/2005": ("tip4p2005.gro",  "tip4p2005.itp",  4),
    "TIP5P/2018": ("tip5p2018.gro",  "tip5p2018.itp",  5),
}

# Atomtype lines loaded from atomtypes.toml — edit that file to add/change models.
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import logging

log = logging.getLogger(__name__)

_ATOMTYPES_FILE = WATER_TOPS / "atomtypes.toml"

def _load_atomtypes() -> dict[str, list[str]]:
    with open(_ATOMTYPES_FILE, "rb") as fh:
        raw = tomllib.load(fh)
    return {model: data["lines"] for model, data in raw.items()}

WATER_ATOMTYPES: dict[str, list[str]] = _load_atomtypes()
log.warning(
    "[pipeline] atomtypes.toml cargado desde %s (mtime=%s) — claves: %s",
    _ATOMTYPES_FILE, _ATOMTYPES_FILE.stat().st_mtime, list(WATER_ATOMTYPES.keys()),
)


# ---------------------------------------------------------------------------
# Python-side helper: inject water topology into system.top
# ---------------------------------------------------------------------------

def _find_section_end(lines: list[str], section_start: int) -> int:
    """
    Return the index of the first line after section_start that starts
    a new [ section ] or reaches EOF.
    """
    for i in range(section_start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            return i
    return len(lines)


def inject_water_topology(top_path: Path, water_model: str) -> None:
    """
    Modify system.top in-place to add the chosen water model:

    1. Append water atomtypes inside the existing [ atomtypes ] block.
       If OW is already present the block is left untouched (idempotent).

    2. Insert the water [ moleculetype ] .itp block just before
       [ system ].  Skipped if WAT is already defined.
    """
    log.warning(
        "[inject_water_topology] water_model=%r | WATER_MODEL_FILES keys=%s | WATER_ATOMTYPES keys=%s",
        water_model, list(WATER_MODEL_FILES.keys()), list(WATER_ATOMTYPES.keys()),
    )
    _, itp_name, _ = WATER_MODEL_FILES[water_model]
    itp_text       = (WATER_TOPS / itp_name).read_text()
    atomtype_lines = WATER_ATOMTYPES[water_model]

    top_lines = top_path.read_text().splitlines(keepends=True)

    # ── 1. Inject into [ atomtypes ] ──────────────────────────────────
    # Find the section header
    at_start = next(
        (i for i, l in enumerate(top_lines)
         if l.strip().replace(" ", "") == "[atomtypes]"),
        None,
    )

    if at_start is not None:
        at_end = _find_section_end(top_lines, at_start)
        block  = "".join(top_lines[at_start:at_end])

        # Idempotent: OW already present → skip
        if "OW" not in block:
            insert_lines = [l + "\n" for l in atomtype_lines]
            top_lines[at_end:at_end] = insert_lines
    # If no [ atomtypes ] section exists we skip silently — unusual topology.

    # ── 2. Insert water moleculetype before [ system ] ─────────────────
    top_text = "".join(top_lines)

    if "WAT" not in top_text:
        marker = "[ system ]"
        if marker in top_text:
            idx      = top_text.index(marker)
            top_text = top_text[:idx] + itp_text + "\n\n" + top_text[idx:]
        else:
            top_text = top_text + "\n" + itp_text + "\n"

    top_path.write_text(top_text)


# ---------------------------------------------------------------------------
# Stage: solvation
# ---------------------------------------------------------------------------

def build_solvation_script(
    run_name:    str,
    water_model: str,
    box_mode:    str,              # "actual" | "resize"
    box_xyz:     tuple | None,     # (x, y, z) nm, only when resize
    skip:        bool = False,     # True = system already solvated, skip stage
) -> list[str]:
    """Shell commands for the solvation stage."""

    if skip:
        return [
            "# ── Solvation ────────────────────────────────────────────────",
            'echo "[Solvation] Skipped — system already solvated."',
        ]

    stab_dir  = DATA_DIR / run_name / "estabilizacion"
    gro_name, _, n_atoms = WATER_MODEL_FILES[water_model]
    solvent   = SOLVENTS / gro_name

    lines = [
        "# ── Solvation ────────────────────────────────────────────────",
        f'STAB="{stab_dir}"',
        f'SOLVENT="{solvent}"',
        f'SCRIPTS="{SCRIPTS}"',
        'GMX_LOG="$RUN_DIR/gromacs.log"',
        "",
    ]

    if box_mode == "resize" and box_xyz:
        x, y, z = box_xyz
        lines += [
            'echo "[1/3] Resizing box..."',
            (f'{GMX} editconf'
             f' -f "$STAB/system.gro"'
             f' -o "$STAB/start.gro"'
             f' -box {x} {y} {z} -c'
             f' >> "$GMX_LOG" 2>&1'),
            "",
        ]
    else:
        lines += [
            'echo "[1/3] Box: using original dimensions."',
            'cp "$STAB/system.gro" "$STAB/start.gro"',
            "",
        ]

    lines += [
        'echo "[2/3] Solvating..."',
        (f'{GMX} solvate'
         f' -cp "$STAB/start.gro"'
         f' -cs "$SOLVENT"'
         f' -o "$STAB/tmp_solvated.gro"'
         f' -p "$STAB/system.top"'
         f' >> "$GMX_LOG" 2>&1'),
        "",
        'echo "[3/3] Removing broken molecules..."',
        (f'python3 "$SCRIPTS/remove_broken.py"'
         f' -f "$STAB/tmp_solvated.gro"'
         f' -o "$STAB/start.gro"'
         f' -na {n_atoms}'
         f' -p "$STAB/system.top"'
         f' >> "$GMX_LOG" 2>&1'),
        "",
        'rm -f "$STAB/tmp_solvated.gro"',
        'echo "Solvation complete."',
    ]

    return lines


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mdp_extra_lines(extra: str) -> str:
    """Return extra mdp lines indented, or empty string."""
    if not extra or not extra.strip():
        return ""
    lines = "\n".join(l for l in extra.strip().splitlines() if l.strip())
    return "\n" + lines + "\n"


def _heredoc(mdp_path: str, content: str) -> list[str]:
    """Wrap content in a bash heredoc writing to mdp_path."""
    lines = [f"cat << 'MDP_EOF' > {mdp_path}"]
    lines += content.splitlines()
    lines += ["MDP_EOF", ""]
    return lines


_POSRES_DEFINE_RE = re.compile(r"^\s*define\s*=.*POSRES", re.IGNORECASE | re.MULTILINE)


def _mdp_needs_posres_ref(mdp_content: str) -> bool:
    """True si el .mdp tiene un `define = -DPOSRES...` que activa algún
    bloque [ position_restraints ] del .top. Solo en ese caso grompp exige
    -r (GROMACS >= 2018); si no está activo, no hace falta pasarlo."""
    return bool(_POSRES_DEFINE_RE.search(mdp_content))


def _grompp_mdrun(label: str, mdp: str, gro: str, top: str,
                  out_tpr: str, out_gro: str, out_prefix: str,
                  needs_posres_ref: bool = False) -> list[str]:
    """grompp + mdrun block, all GROMACS output → GMX_LOG.

    -r solo se agrega si el .mdp de esta etapa activa restricciones de
    posición (define = -DPOSRES...); si no, grompp no lo exige y no hace
    falta pasarlo. Cuando hace falta, usamos la misma estructura que -c
    como referencia (lo que la propia GROMACS recomienda por defecto).
    """
    ref_flag = f' -r "{gro}"' if needs_posres_ref else ""
    return [
        f'{GMX} grompp -f "{mdp}" -c "{gro}"{ref_flag} -p "{top}" -o "{out_tpr}" -maxwarn 2'
        f' >> "$GMX_LOG" 2>&1',
        f'{GMX} mdrun -v -deffnm "{out_prefix}"'
        f' >> "$GMX_LOG" 2>&1',
        "",
    ]


# ---------------------------------------------------------------------------
# Stage: minimization
# ---------------------------------------------------------------------------

def build_minimization_script(
    run_name:  str,
    label:     str,        # "EM" or "EM_final"
    algo:      str,        # "steep" | "cg"
    nsteps:    int,
    emtol:     float,
    in_gro:    str,        # bash variable or path, e.g. "$STAB/start.gro"
    out_gro:   str,        # bash variable or path
    extra_mdp: str = "",
) -> list[str]:
    stab_dir = DATA_DIR / run_name / "estabilizacion"

    mdp_content = f"""; Minimization — {label}
integrator           = {algo}
emtol                = {emtol}
emstep               = 0.01
nsteps               = {nsteps}

; Neighborsearching
nstlist              = 1
cutoff-scheme        = Verlet
ns_type              = grid
rlist                = 1.0
coulombtype          = PME
rcoulomb             = 1.0
rvdw                 = 1.0
pbc                  = xyz{_mdp_extra_lines(extra_mdp)}"""

    mdp_path    = f'"{stab_dir}/{label}.mdp"'
    tpr_prefix  = f'"{stab_dir}/{label}"'
    out_prefix  = f'"{stab_dir}/{label}"'

    lines = [
        f"# ── Minimization: {label} ─────────────────────────────────────",
        f'echo "[Minimization] Writing {label}.mdp..."',
    ]
    lines += _heredoc(mdp_path, mdp_content)
    lines += [f'echo "[Minimization] Running {label}..."']
    lines += _grompp_mdrun(
        label    = label,
        mdp      = mdp_path,
        gro      = in_gro,
        top      = f'"{stab_dir}/system.top"',
        out_tpr  = f'"{stab_dir}/{label}.tpr"',
        out_gro  = out_gro,
        out_prefix = f'"{stab_dir}/{label}"',
        needs_posres_ref = _mdp_needs_posres_ref(mdp_content),
    )
    lines += [f'echo "[Minimization] {label} complete."']
    return lines


# ---------------------------------------------------------------------------
# Stage: equilibration (NVT / NPT steps)
# ---------------------------------------------------------------------------

def build_equilibration_script(
    run_name:   str,
    steps:      list[dict],   # list of step configs from frontend
) -> list[str]:
    """
    Each step dict has keys:
      label      str   e.g. "NVT_1"
      ensemble   str   "NVT" | "NPT"
      nsteps     int
      dt         float  (fs)
      temp       float  (K)
      pres       float  (bar, NPT only)
      aniso      bool
      extra_mdp  str
      is_first   bool   (True for the very first dynamics run → gen_vel = yes)
    """
    stab_dir = DATA_DIR / run_name / "estabilizacion"
    lines    = ["# ── Equilibration ────────────────────────────────────────────"]

    for i, step in enumerate(steps):
        label      = step["label"]
        ensemble   = step["ensemble"]
        nsteps     = int(step["nsteps"])
        dt_fs      = float(step["dt"])
        dt_ps      = dt_fs / 1000.0
        temp       = float(step["temp"])
        is_first   = step.get("is_first", False)
        extra_mdp  = step.get("extra_mdp", "")
        aniso      = step.get("aniso", False)
        pres       = float(step.get("pres", 1.0))

        # Pressure coupling block
        if ensemble == "NPT":
            if aniso:
                pcoupl_block = f"""; Pressure coupling
pcoupl               = C-rescale
pcoupltype           = semiisotropic
tau_p                = 2.0
ref_p                = {pres} {pres}
compressibility      = 0.0 4.5e-5
refcoord_scaling     = com"""
            else:
                pcoupl_block = f"""; Pressure coupling
pcoupl               = C-rescale
pcoupltype           = isotropic
tau_p                = 2.0
ref_p                = {pres}
compressibility      = 4.5e-5
refcoord_scaling     = com"""
        else:
            pcoupl_block = "; Pressure coupling\npcoupl               = no"

        # Velocity generation
        if is_first:
            vel_block = f"""; Velocity generation
gen_vel              = yes
gen_temp             = {temp}
gen_seed             = -1"""
        else:
            vel_block = "; Velocity generation\ngen_vel              = no"

        continuation = "no" if is_first else "yes"

        mdp_content = f"""; Equilibration — {label}
integrator           = md
nsteps               = {nsteps}
dt                   = {dt_ps:.6f}

; Output control (save only at end)
nstxout              = {nsteps}
nstvout              = {nsteps}
nstenergy            = {nsteps}
nstlog               = {nsteps}

; Bond parameters
continuation         = {continuation}
constraint_algorithm = lincs
constraints          = all-bonds
lincs_iter           = 1
lincs_order          = 4

; Neighborsearching
cutoff-scheme        = Verlet
ns_type              = grid
nstlist              = 20
rlist                = 1.0
rcoulomb             = 1.0
rvdw                 = 1.0

; Electrostatics
coulombtype          = PME
pme_order            = 4
fourierspacing       = 0.16

; Temperature coupling
tcoupl               = V-rescale
tc-grps              = System
tau_t                = 0.1
ref_t                = {temp}

{pcoupl_block}

; Periodic boundary conditions
pbc                  = xyz

; Dispersion correction
DispCorr             = EnerPres

{vel_block}{_mdp_extra_lines(extra_mdp)}"""

        # Input gro: first step uses EM output, rest chain from previous
        if i == 0:
            in_gro = f'"{stab_dir}/EM.gro"'
        else:
            prev_label = steps[i-1]["label"]
            in_gro = f'"{stab_dir}/{prev_label}.gro"'

        mdp_path = f'"{stab_dir}/{label}.mdp"'

        lines += [f'echo "[Equilibration] Writing {label}.mdp..."']
        lines += _heredoc(mdp_path, mdp_content)
        lines += [f'echo "[Equilibration] Running {label}..."']
        lines += _grompp_mdrun(
            label      = label,
            mdp        = mdp_path,
            gro        = in_gro,
            top        = f'"{stab_dir}/system.top"',
            out_tpr    = f'"{stab_dir}/{label}.tpr"',
            out_gro    = f'"{stab_dir}/{label}.gro"',
            out_prefix = f'"{stab_dir}/{label}"',
            needs_posres_ref = _mdp_needs_posres_ref(mdp_content),
        )
        lines += [f'echo "[Equilibration] {label} complete."']

    return lines


# ---------------------------------------------------------------------------
# Stage: production
# ---------------------------------------------------------------------------

def compute_n_confs(nsteps: int, dt_fs: float, nstxout_ps: float) -> int:
    """Cantidad de frames que va a producir trjconv -sep en producción —
    misma cuenta que usa GROMACS para nstxout-compressed. Se expone acá
    (no solo dentro de build_production_script) para que routes_launch.py
    pueda anticipar el N esperado al encolar el job, sin tener que esperar
    a que el pipeline corra para saberlo (usado para el label de la cola)."""
    nstxout_comp = max(1, round(nstxout_ps * 1000 / dt_fs))
    return nsteps // nstxout_comp + 1


def build_production_script(
    run_name:    str,
    ensemble:    str,    # "NVT" | "NPT"
    nsteps:      int,
    dt_fs:       float,
    temp:        float,
    pres:        float,
    nstxout_ps:  float,  # save compressed frames every N ps
    aniso:       bool,
    extra_mdp:   str,
    last_equil_label: str | None,   # label of last equilibration step
) -> list[str]:
    stab_dir = DATA_DIR / run_name / "estabilizacion"
    dt_ps    = dt_fs / 1000.0

    # nstxout-compressed in steps
    nstxout_comp = max(1, round(nstxout_ps * 1000 / dt_fs))

    if ensemble == "NPT":
        if aniso:
            pcoupl_block = f"""; Pressure coupling
pcoupl               = C-rescale
pcoupltype           = semiisotropic
tau_p                = 2.0
ref_p                = {pres} {pres}
compressibility      = 0.0 4.5e-5
refcoord_scaling     = com"""
        else:
            pcoupl_block = f"""; Pressure coupling
pcoupl               = C-rescale
pcoupltype           = isotropic
tau_p                = 2.0
ref_p                = {pres}
compressibility      = 4.5e-5
refcoord_scaling     = com"""
    else:
        pcoupl_block = "; Pressure coupling\npcoupl               = no"

    if last_equil_label:
        in_gro = f'"{stab_dir}/{last_equil_label}.gro"'
    else:
        in_gro = f'"{stab_dir}/EM.gro"'

    mdp_content = f"""; Production
integrator           = md
nsteps               = {nsteps}
dt                   = {dt_ps:.6f}

; Output control
nstxout              = {nsteps}
nstvout              = {nsteps}
nstenergy            = {nsteps}
nstlog               = {nsteps}
nstxout-compressed   = {nstxout_comp}

; Bond parameters
continuation         = yes
constraint_algorithm = lincs
constraints          = all-bonds
lincs_iter           = 1
lincs_order          = 4

; Neighborsearching
cutoff-scheme        = Verlet
ns_type              = grid
nstlist              = 20
rlist                = 1.0
rcoulomb             = 1.0
rvdw                 = 1.0

; Electrostatics
coulombtype          = PME
pme_order            = 4
fourierspacing       = 0.16

; Temperature coupling
tcoupl               = V-rescale
tc-grps              = System
tau_t                = 0.1
ref_t                = {temp}

{pcoupl_block}

; Periodic boundary conditions
pbc                  = xyz

; Dispersion correction
DispCorr             = EnerPres

; Velocity generation
gen_vel              = no{_mdp_extra_lines(extra_mdp)}"""

    lines = [
        "# ── Production ───────────────────────────────────────────────",
        'echo "[Production] Writing PROD.mdp..."',
    ]
    lines += _heredoc(f'"{stab_dir}/PROD.mdp"', mdp_content)
    lines += ['echo "[Production] Running PROD..."']
    lines += _grompp_mdrun(
        label      = "PROD",
        mdp        = f'"{stab_dir}/PROD.mdp"',
        gro        = in_gro,
        top        = f'"{stab_dir}/system.top"',
        out_tpr    = f'"{stab_dir}/PROD.tpr"',
        out_gro    = f'"{stab_dir}/PROD.gro"',
        out_prefix = f'"{stab_dir}/PROD"',
        needs_posres_ref = _mdp_needs_posres_ref(mdp_content),
    )
    lines += ['echo "[Production] Complete."']

    # ── Separar la trayectoria en confs individuales (conf-*.gro) ──────
    # Insumo para build_confs_minimization_script(), que se agrega como
    # una sección aparte a continuación de esta en el pipeline.
    confs_dir = stab_dir / "confs"
    lines += [
        "",
        "# ── Separación de trayectoria (trjconv -sep) ────────────────────",
        'echo "[Production] Separando trayectoria en confs individuales..."',
        f'CONFS_DIR="{confs_dir}"',
        'mkdir -p "$CONFS_DIR"',
        (f'echo 0 | {GMX} trjconv'
         f' -f "{stab_dir}/PROD.xtc"'
         f' -s "{stab_dir}/PROD.tpr"'
         f' -o "$CONFS_DIR/conf-.gro"'
         f' -sep'
         f' >> "$GMX_LOG" 2>&1'),
        'N_CONFS=$(ls "$CONFS_DIR"/conf-*.gro 2>/dev/null | wc -l)',
        'echo "[Production] $N_CONFS confs generados en confs/."',
    ]

    return lines


def build_confs_minimization_script(
    run_name:         str,
    prod_nsteps:       int,
    prod_dt_fs:        float,
    prod_nstxout_ps:   float,
    last_equil_label:  str | None,   # mismo criterio de referencia que PROD
    posres_define:     str,          # ej. "-DPOSRES"; vacío = sin restricciones
    emtol1:            float,        # EM-1 (estricto): configurable en la UI
    nsteps1:           int,          # EM-1 (estricto): configurable en la UI
) -> list[str]:
    """
    Minimiza cada conf-N.gro (generado por trjconv -sep en producción) con
    una escalera de hasta 3 niveles, del más estricto al más laxo. Por cada
    conf se prueba EM-1; si no converge (no aparece el .gro de salida) se
    prueba EM-2, y si tampoco, EM-3. El primero que converge gana y se
    pasa al siguiente conf. Si ninguno converge, el conf queda "en blanco"
    (no genera em-N.gro) y el pipeline sigue sin cortarse.

    Solo EM-1 es configurable desde la UI (posres_define/emtol1/nsteps1).
    EM-2 y EM-3 son laxados automáticamente como múltiplos de EM-1 —
    son de resguardo ante fallos, no un ajuste fino por sistema.
    """
    stab_dir  = DATA_DIR / run_name / "estabilizacion"
    confs_dir = stab_dir / "confs"
    min_dir   = DATA_DIR / run_name / "confs_min"      # carpeta nueva, aparte

    # Misma cuenta de frames que usó trjconv -sep en producción, y mismo
    # criterio de referencia (-r) que PROD: la estructura de entrada a esa
    # etapa (última equilibración, o EM si no hubo equilibración).
    n_confs = compute_n_confs(prod_nsteps, prod_dt_fs, prod_nstxout_ps)

    if last_equil_label:
        ref_gro = f'"{stab_dir}/{last_equil_label}.gro"'
    else:
        ref_gro = f'"{stab_dir}/EM.gro"'

    top_path = f'"{stab_dir}/system.top"'

    define_ok   = bool(posres_define and posres_define.strip())
    define_line = f"\ndefine               = {posres_define.strip()}" if define_ok else ""

    def _level_mdp(emtol: float, nsteps: int) -> str:
        return f"""; Minimization (confs_min)
integrator           = steep
emtol                = {emtol}
emstep               = 0.001
nsteps               = {nsteps}

; Neighborsearching
nstlist              = 1
cutoff-scheme        = Verlet
ns_type              = grid
rlist                = 1.0
coulombtype          = PME
rcoulomb             = 1.0
rvdw                 = 1.0
pbc                  = xyz{define_line}"""

    # EM-2/EM-3: laxados como múltiplos de EM-1, no hardcodeados a un valor
    # fijo — así se adaptan solos a la escala de emtol que uses (varía
    # mucho según sistema/unidades), en vez de un número mágico que solo
    # tendría sentido para un caso puntual.
    levels = [
        (1, emtol1,       nsteps1),
        (2, emtol1 * 3.0, max(1, min(nsteps1, 200_000))),
        (3, emtol1 * 8.0, max(1, min(nsteps1, 50_000))),
    ]

    lines = [
        "# ── Minimización de confs (escalera de niveles) ─────────────────",
        f'MIN_DIR="{min_dir}"',
        f'CONFS_DIR="{confs_dir}"',
        'mkdir -p "$MIN_DIR"',
        "",
    ]

    for lvl, emtol, nsteps in levels:
        mdp_content = _level_mdp(emtol, nsteps)
        lines += [f'echo "[MinConfs] Escribiendo EM-{lvl}.mdp (emtol={emtol}, nsteps={nsteps})..."']
        lines += _heredoc(f'"$MIN_DIR/EM-{lvl}.mdp"', mdp_content)

    ref_flag = f' -r {ref_gro}' if define_ok else ""

    lines += [
        f'echo "[MinConfs] Minimizando {n_confs} confs (hasta 3 niveles cada uno)..."',
        'CONF_COUNT=0',
        'CONV_COUNT=0',
        'echo "conf,nivel,estado" > "$MIN_DIR/summary.csv"',
        "",
        f'for j in $(seq 0 {n_confs - 1}); do',
        '    CONF="$CONFS_DIR/conf-${j}.gro"',
        '    [ -f "$CONF" ] || continue',
        '    CONF_COUNT=$((CONF_COUNT + 1))',
        '    DONE=0',
        '    for lvl in 1 2 3; do',
        (f'        set +e; {GMX} grompp -f "$MIN_DIR/EM-${{lvl}}.mdp" -c "$CONF"{ref_flag}'
         f' -p {top_path} -maxwarn 1 -o "$MIN_DIR/em-${{j}}.tpr" >> "$GMX_LOG" 2>&1; set -e'),
        f'        set +e; {GMX} mdrun -deffnm "$MIN_DIR/em-${{j}}" >> "$GMX_LOG" 2>&1; set -e',
        '        rm -f "$MIN_DIR/em-${j}.edr" "$MIN_DIR/em-${j}.log" "$MIN_DIR/em-${j}.tpr" \\',
        '               "$MIN_DIR/em-${j}.trr" "$MIN_DIR"/step*.pdb "$MIN_DIR/mdout.mdp"',
        '        if [ -f "$MIN_DIR/em-${j}.gro" ]; then',
        '            DONE=1',
        '            CONV_COUNT=$((CONV_COUNT + 1))',
        '            echo "${j},${lvl},ok" >> "$MIN_DIR/summary.csv"',
        '            break',
        '        fi',
        '    done',
        '    if [ "$DONE" -eq 0 ]; then',
        '        echo "${j},-,fallido" >> "$MIN_DIR/summary.csv"',
        '    fi',
        'done',
        "",
        'echo "[MinConfs] $CONV_COUNT/$CONF_COUNT confs minimizados. Detalle en confs_min/summary.csv"',
    ]

    return lines


def build_system_toml_script(
    run_name:              str,
    total_simulated_ns:    float,
    snapshot_interval_ps:  float,
    ensemble:              str,
    n_confs:               int,
    confmin_enabled:       bool,
) -> list[str]:
    """
    Escribe DATA_DIR/<run_name>/system.toml al final del pipeline — es lo
    que hace que la corrida aparezca en "Elegir Sistema" (main.py solo
    lista carpetas que tengan este archivo). Un único archivo por corrida:
    [dataset.real] y [dataset.inherent] conviven ahí, cada uno con su
    propia ruta relativa (los confs NO se mueven ni se duplican, quedan
    donde el pipeline ya los deja). El motor C++ decide con cuál dataset
    calcular a partir de esta metadata — acá solo se la dejamos servida.

    n_converged de [dataset.inherent] se calcula en runtime (recién se
    sabe cuando termina confs_min/), así que el heredoc de abajo usa un
    delimitador SIN comillas a propósito, para que "$N_CONVERGED" se
    expanda al escribir el archivo.
    """
    run_dir   = DATA_DIR / run_name
    min_dir   = run_dir / "confs_min"
    confs_rel = "estabilizacion/confs"   # relativo a run_dir, como ya está

    lines = [
        "# ── system.toml (registro para 'Elegir Sistema') ────────────────",
        'echo "[System] Escribiendo system.toml..."',
    ]

    if confmin_enabled:
        lines += [
            f'MIN_SUMMARY="{min_dir}/summary.csv"',
            'N_CONVERGED=0',
            'if [ -f "$MIN_SUMMARY" ]; then',
            '''    N_CONVERGED=$(awk -F, 'NR>1 && $3=="ok"' "$MIN_SUMMARY" | wc -l)''',
            'fi',
        ]
        inherent_block = f'''[dataset.inherent]
enabled = true
path = "confs_min"
prefix = "em-"
n_confs = {n_confs}
n_converged = $N_CONVERGED
summary = "confs_min/summary.csv"'''
    else:
        inherent_block = '''[dataset.inherent]
enabled = false'''

    toml_content = f'''[info]
name = "{run_name}"
description = "Generado automáticamente al terminar el pipeline."

[simulation]
total_simulated_ns = {total_simulated_ns}
snapshot_interval_ps = {snapshot_interval_ps}
ensemble = "{ensemble}"

[dataset.real]
path = "{confs_rel}"
prefix = "conf-"
n_confs = {n_confs}

{inherent_block}'''

    # Delimitador SIN comillas a propósito (ver docstring): necesitamos que
    # $N_CONVERGED se expanda. El resto del contenido no tiene '$' asi que
    # es seguro.
    lines += [f'cat << SYS_TOML_EOF > "{run_dir}/system.toml"']
    lines += toml_content.splitlines()
    lines += ['SYS_TOML_EOF', '']
    lines += ['echo "[System] system.toml listo — ya debería aparecer en \'Elegir Sistema\'."']

    return lines


def write_run_script(run_name: str, sections: list[list[str]]) -> Path:
    """Concatenate stage sections and write run.sh. Returns its path."""
    run_dir = DATA_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    header = [
        "#!/bin/bash",
        "set -euo pipefail",
        "",
        "module load gromacs",
        "",
        f'RUN_DIR="{run_dir}"',
        'PROGRESS="$RUN_DIR/progress.log"',
        'GMX_LOG="$RUN_DIR/gromacs.log"',
        "",
        # Redirect all stdout (echo lines) to progress.log, unbuffered
        'exec > >(tee -a "$PROGRESS") 2>/dev/null',
        "",
        # Trap: on any error, write sentinel and show tail of gromacs.log
        "trap 'echo \"PIPELINE_ERROR\"; echo \"--- last gromacs output ---\"; "
        "tail -20 \"$GMX_LOG\" 2>/dev/null || true' ERR",
        "",
        'echo "PIPELINE_START"',
        "",
    ]

    footer = [
        "",
        'echo "PIPELINE_DONE"',
        "",
        "module purge",
    ]

    all_lines = header
    for section in sections:
        all_lines += section
        all_lines.append("")
    all_lines += footer

    script_path = run_dir / "run.sh"
    script_path.write_text("\n".join(all_lines))
    script_path.chmod(0o755)
    return script_path
