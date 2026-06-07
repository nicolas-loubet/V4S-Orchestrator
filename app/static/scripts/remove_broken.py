#!/usr/bin/env python3
"""
remove_broken.py
Removes broken or invalid water molecules from a GROMACS .gro file.
Intended to clean configurations where molecules cross PBC or overlap with solid substrates.
"""

import math
import argparse

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parser= argparse.ArgumentParser( description="Remove broken or invalid molecules from a GROMACS .gro file." )
parser.add_argument("-f", "--file",    required=True, metavar="FILE", help="Input .gro file to be corrected.")
parser.add_argument("-o", "--output",  required=True, metavar="OUTPUT", help="Output corrected .gro file.")
parser.add_argument("-na", "--number_atoms", required=True, type=int, metavar="N", help="Number of atoms per solvent molecule.")
parser.add_argument("-p", "--topol",   required=True, metavar="TOPOLOGY", help="Topology (.top) file to be updated.")
args= parser.parse_args()

N_ATOMS_MOL= args.number_atoms

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fmt_int(value: int, width: int) -> str:
    """Right-align an integer in a fixed-width field (wraps at 10^width)."""
    return str(value % (10 ** width)).rjust(width)


def bounding_box(xs, ys, zs, n_substrate):
    """Return [xmin, ymin, zmin, xmax, ymax, zmax] of the substrate atoms."""
    return [
        min(xs[2:n_substrate+2]),
        min(ys[2:n_substrate+2]),
        min(zs[2:n_substrate+2]),
        max(xs[2:n_substrate+2]),
        max(ys[2:n_substrate+2]),
        max(zs[2:n_substrate+2]),
    ]


def inside_substrate(x, y, z, bbox) -> bool:
    """Check whether a point lies inside the substrate bounding box."""
    return (bbox[0] <= x <= bbox[3] and
            bbox[1] <= y <= bbox[4] and
            bbox[2] <= z <= bbox[5])


def too_close_to_substrate(x, y, z, xs, ys, zs, n_substrate, threshold=0.3) -> bool:
    """Check whether a point is within *threshold* nm of any substrate atom."""
    for i in range(n_substrate):
        if math.sqrt((x - xs[i])**2 + (y - ys[i])**2 + (z - zs[i])**2) < threshold:
            return True
    return False


def molecule_is_broken(oxygen_xyz, other_atoms) -> bool:
    """Return True if any atom in *other_atoms* is more than 2 nm away from the reference oxygen along any axis."""
    for atom in other_atoms:
        for coord, ref in zip(atom, oxygen_xyz):
            if abs(coord - ref) > 2:
                return True
    return False

# ---------------------------------------------------------------------------
# Read .gro file
# ---------------------------------------------------------------------------
lines: list[str]= []
xs:    list[float]= []
ys:    list[float]= []
zs:    list[float]= []
box= [0.0, 0.0, 0.0]
n_total= 0
n_substrate_molecules= 0
n_substrate_atoms= 0

with open(args.file) as fh:
    for i_line, line in enumerate(fh, start=1):
        x= y= z= 0.0
        if i_line == 2: n_total= int(line)
        elif 3 <= i_line <= n_total + 2:
            x= float(line[21:29])
            y= float(line[29:37])
            z= float(line[37:45])
            if n_substrate_molecules == 0 and line[5:8] == "WAT":
                n_substrate_molecules= int(line[:5]) - 1
                n_substrate_atoms    = int(line[15:20]) - 1
        elif i_line == n_total + 3:
            box[0]= float(line[1:10])
            box[1]= float(line[11:20])
            box[2]= float(line[21:])
        lines.append(line)
        xs.append(x)
        ys.append(y)
        zs.append(z)

# ---------------------------------------------------------------------------
# Filter molecules
# ---------------------------------------------------------------------------
first_water_atom= n_substrate_atoms + 1   # 0-based index into lines[]
first_water_mol = n_substrate_molecules + 1

mol_counter = first_water_mol
atom_counter= first_water_atom

kept_lines: list[str]= list(lines[:first_water_atom + 1])  # header + substrate

bbox= bounding_box(xs, ys, zs, n_substrate_atoms - 1)

print("Removing broken or misplaced solvent molecules...")

start= first_water_atom + 1
for i in range(start, len(xs) - 3, N_ATOMS_MOL):
    ox, oy, oz= xs[i], ys[i], zs[i]
    neighbors = [[xs[i+k], ys[i+k], zs[i+k]] for k in range(1, N_ATOMS_MOL)]

    discard= (
        inside_substrate(ox, oy, oz, bbox) or
        too_close_to_substrate(ox, oy, oz, xs, ys, zs, n_substrate_atoms) or
        molecule_is_broken([ox, oy, oz], neighbors) or
        any( xs[i+k] > box[0] or ys[i+k] > box[1] or zs[i+k] > box[2] for k in range(N_ATOMS_MOL) )
    )

    if not discard:
        for k in range(N_ATOMS_MOL):
            old= lines[i + k]
            kept_lines.append( fmt_int(mol_counter,  5) + old[5:15] + fmt_int(atom_counter, 5) + old[20:] )
            atom_counter+= 1
        mol_counter+= 1

n_water= mol_counter - 1 - n_substrate_molecules
print(f"Done. Kept {n_water} solvent molecules.")

kept_lines.append(lines[-1])                    # box line
kept_lines[1]= f"{atom_counter-1}\n"        # updated atom count

# ---------------------------------------------------------------------------
# Write output .gro
# ---------------------------------------------------------------------------
with open(args.output, "w") as fh:
    fh.writelines(kept_lines)

# ---------------------------------------------------------------------------
# Update topology (last numeric entry= solvent count)
# ---------------------------------------------------------------------------
with open(args.topol) as fh:
    top_lines= fh.readlines()

last= top_lines[-1].split()
top_lines[-1]= f" {last[0]}{' ' * 14}{n_water}\n"

with open(args.topol, "w") as fh:
    fh.writelines(top_lines)

print(f"Topology updated: {last[0]}= {n_water}")
