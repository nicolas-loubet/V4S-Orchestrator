# app/pipeline.py
"""
Pipeline builder for V4S-Orchestrator.
Each stage function returns a list of shell command strings.
write_run_script() concatenates them all into run.sh.
"""

from pathlib import Path

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


def _grompp_mdrun(label: str, mdp: str, gro: str, top: str,
                  out_tpr: str, out_gro: str, out_prefix: str) -> list[str]:
    """grompp + mdrun block, all GROMACS output → GMX_LOG."""
    return [
        f'{GMX} grompp -f "{mdp}" -c "{gro}" -p "{top}" -o "{out_tpr}" -maxwarn 2'
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
        )
        lines += [f'echo "[Equilibration] {label} complete."']

    return lines


# ---------------------------------------------------------------------------
# Stage: production
# ---------------------------------------------------------------------------

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
    )
    lines += ['echo "[Production] Complete."']
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
