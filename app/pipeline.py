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

_ATOMTYPES_FILE = WATER_TOPS / "atomtypes.toml"

def _load_atomtypes() -> dict[str, list[str]]:
    with open(_ATOMTYPES_FILE, "rb") as fh:
        raw = tomllib.load(fh)
    return {model: data["lines"] for model, data in raw.items()}

WATER_ATOMTYPES: dict[str, list[str]] = _load_atomtypes()


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
# Write run.sh
# ---------------------------------------------------------------------------

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
