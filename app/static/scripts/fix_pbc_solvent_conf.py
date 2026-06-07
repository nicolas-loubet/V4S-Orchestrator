import pandas as pd
import argparse

# Correr como: python3 corregir_PBC.py -i estabilizado_300K.gro -o corregido_pbc.gro -n 4

def leer_gro(f):
    df= pd.read_fwf(f, skiprows=2, skipfooter=1, widths=[5,5,5,5,8,8,8], header=None)
    with open(f, "r") as f:
        lineas= f.readlines()
        bounds= [float(x) for x in lineas[-1].split()]
    return df, bounds

def corregirMolec(df, i, j, k, bound):
    if(df.iloc[i,k+4] > bound/2):
        df.iloc[j,k+4]+= bound
    else:
        df.iloc[j,k+4]-= bound

def dist(a,b,k):
    return abs(a[k+4] - b[k+4])

def corregir_PBC(df, bounds, n_atoms_water):
    for i in range(0,len(df),n_atoms_water):
        for j in range(i+1,i+n_atoms_water):
            for k in range(3):
                if(dist(df.iloc[i], df.iloc[j],k) > bounds[k]/2):
                    corregirMolec(df,i,j,k,bounds[k])
    return df

def escribir_gro(df, f, bounds):
    with open(f, "w") as fw:
        fw.write("Agua tip4p_2005 bulk\n")
        fw.write(f"{len(df)}\n")
        for i,row in df.iterrows():
            fw.write(f"{row[0]:>5}{row[1]:<5}{row[2]:>5}{row[3]:>5}{row[4]:>8.3f}{row[5]:>8.3f}{row[6]:>8.3f}\n")
        fw.write(f"{bounds[0]:>10.5f}{bounds[1]:>10.5f}{bounds[2]:>10.5f}\n")

if __name__ == "__main__":
    parser= argparse.ArgumentParser()
    input_file= parser.add_argument("-i", "--input_file", help="Input file", required=True)
    output_file= parser.add_argument("-o", "--output_file", help="Output file", required=True)
    n_atoms_water= parser.add_argument("-n", "--n_atoms_water", help="Number of atoms in water", required=True)
    args= parser.parse_args()

    df,bounds= leer_gro(args.input_file)
    df= corregir_PBC(df, bounds, int(args.n_atoms_water))
    escribir_gro(df, args.output_file, bounds)
