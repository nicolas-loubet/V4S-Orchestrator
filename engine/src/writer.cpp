#include <CppLibAmberGromacs.hpp>

namespace Writer {
    // Clase base abstracta
    class Output {
    protected:
        vector<bool> show_ViS;

        void normalizeDIT(vector<Real>& values, const Real DIT= -6.0) const {
            for(int i= 0; i < values.size(); i++) values[i]= (values[i] / DIT) - 1.0;
        }

        void calculateShowViS(const RunConfig& cfg) {
            show_ViS.resize(4,false);
            for(string s: cfg.params) show_ViS[stoi(s.substr(1,s.size()-2))-1]= true;
        }

    public:
        virtual ~Output()= default;
        // 1. Save values when computed
        virtual void accumulate(int frame, int idx, const vector<Real>& vis)= 0;
        // 2. Calculate mean values
        virtual void normalize(RunConfig& cfg)= 0;
        // 3. Save results
        virtual void write(const string& filename, const RunConfig& cfg) const= 0;
    };

    // 1. Temporal + Global
    class OutputTimeSimple: public Output {
        vector<vector<Real>> data;
        vector<int> N_wat;
    public:
        OutputTimeSimple(int n_conf): data(n_conf+1, vector<Real>(4,0.0)), N_wat(n_conf+1,0) {}

        void accumulate(int frame, int, const vector<Real>& vis) override {
            for(int i= 0; i < 4; i++) data[frame][i]+= vis[i];
            N_wat[frame]++;
        }

        void normalize(RunConfig& cfg) override {
            calculateShowViS(cfg);
            for(int i= 0; i < data.size(); i++) {
                for(int j= 0; j < 4; j++) data[i][j]/= N_wat[i];
                if(cfg.units == "adimensional") normalizeDIT(data[i]);
            }
        }

        void write(const string& filename, const RunConfig& cfg) const override {
            CSVWriter csvw(cfg.run_dir.string() + "/results/" + filename);
            
            vector<string> headers= {"conf"};
            for(int iv= 1; iv <= 4; iv++) if(show_ViS[iv-1]) headers.push_back("V"+to_string(iv)+"S");
            if(cfg.save_mol_count) headers.push_back("N");
            csvw.writeHeader(headers);

            for(int t= 0; t < data.size(); t++) {
                vector<string> row= {to_string(t)};
                for(int iv= 1; iv <= 4; iv++) if(show_ViS[iv-1]) row.push_back(to_string(data[t][iv-1]));
                if(cfg.save_mol_count) row.push_back(to_string(N_wat[t]));
                csvw.writeRow(row);
            }
        }
    };

    // 2. Temporal + Átomos
    class OutputTimeAtoms: public Output {
        vector<vector<vector<Real>>> data;
        vector<vector<int>> N_wat;
    public:
        OutputTimeAtoms(int n_conf, int n_atoms): data(n_conf+1, vector<vector<Real>>(n_atoms+1, vector<Real>(4,0.0))), N_wat(n_conf+1, vector<int>(n_atoms+1,0)) {}

        void accumulate(int frame, int idx, const vector<Real>& vis) override {
            for(int i= 0; i < 4; i++) data[frame][idx][i]+= vis[i];
            N_wat[frame][idx]++;
        }

        void normalize(RunConfig& cfg) override {
            calculateShowViS(cfg);
            for(int i= 0; i < data.size(); i++) {
                for(int j= 0; j < data[i].size(); j++) {
                    for(int k= 0; k < 4; k++) data[i][j][k]/= N_wat[i][j];
                    if(cfg.units == "adimensional") normalizeDIT(data[i][j]);
                }
            }
        }

        void write(const string& filename, const RunConfig& cfg) const override {
            CSVWriter csvw(cfg.run_dir.string() + "/results/" + filename);

            vector<string> atoms= splitNames(cfg.atom_selection);
            atoms.insert(atoms.begin(), "All");
            
            vector<string> headers= {"conf"};
            for(int ia= 0; ia < atoms.size(); ia++) {
                for(int iv= 1; iv <= 4; iv++) if(show_ViS[iv-1]) headers.push_back(atoms[ia]+"_V"+to_string(iv)+"S");
                if(cfg.save_mol_count) headers.push_back(atoms[ia]+"_N");
            }
            csvw.writeHeader(headers);

            for(int t= 0; t < data.size(); t++) {
                vector<string> row= {to_string(t)};
                for(int ia= 0; ia < atoms.size(); ia++) {
                    for(int iv= 1; iv <= 4; iv++) if(show_ViS[iv-1]) row.push_back(to_string(data[t][ia][iv-1]));
                    if(cfg.save_mol_count) row.push_back(to_string(N_wat[t][ia]));
                }
                csvw.writeRow(row);
            }
        }
    };

    // 3. Promedio + Global
    class OutputMeanSimple: public Output {
        // Media global
        vector<Real> data= vector<Real>(4,0.0);
        int N_wat= 0;
        // Media por frame (para SEM): acumulo sumas y conteos, keyed por frame
        map<int, vector<Real>> frame_sums;
        map<int, int> frame_counts;
        // Resultados
        vector<Real> frame_mean= vector<Real>(4,0.0);
        vector<Real> sem= vector<Real>(4,0.0);

    public:
        void accumulate(int frame, int, const vector<Real>& vis) override {
            for(int i= 0; i < 4; i++) data[i]+= vis[i];
            N_wat++;

            if(!frame_sums.count(frame)) {
                frame_sums[frame]= vector<Real>(4,0.0);
                frame_counts[frame]= 0;
            }
            for(int i= 0; i < 4; i++) frame_sums[frame][i]+= vis[i];
            frame_counts[frame]++;
        }

        void normalize(RunConfig& cfg) override {
            calculateShowViS(cfg);

            for(int i= 0; i < 4; i++) data[i]/= N_wat;
            if(cfg.units == "adimensional") normalizeDIT(data);

            // Calculo la media de cada frame (solo frames con datos)
            vector<vector<Real>> means;
            for(auto& [f, sums]: frame_sums) {
                vector<Real> m(4);
                for(int i= 0; i < 4; i++) m[i]= sums[i] / frame_counts[f];
                if(cfg.units == "adimensional") normalizeDIT(m);
                means.push_back(m);
            }

            int n_frames= means.size();
            for(int i= 0; i < 4; i++) {
                Real sum= 0.0;
                for(auto& m: means) sum+= m[i];
                frame_mean[i]= sum / n_frames;
            }
            for(int i= 0; i < 4; i++) {
                Real sq= 0.0;
                for(auto& m: means) sq+= pow(m[i] - frame_mean[i], 2);
                sem[i]= sqrt(sq / (n_frames * (n_frames - 1)));
            }
        }
        
        void write(const string& filename, const RunConfig& cfg) const override {
            CSVWriter csvw(cfg.run_dir.string() + "/results/" + filename);

            vector<string> headers;
            for(int iv= 1; iv <= 4; iv++) if(show_ViS[iv-1]) {
                headers.push_back("V"+to_string(iv)+"S");
                headers.push_back("V"+to_string(iv)+"S_fMean");
                headers.push_back("V"+to_string(iv)+"S_SEM");
            }
            if(cfg.save_mol_count) headers.push_back("N");
            csvw.writeHeader(headers);

            vector<string> row;
            for(int iv= 1; iv <= 4; iv++) if(show_ViS[iv-1]) {
                row.push_back(to_string(data[iv-1]));
                row.push_back(to_string(frame_mean[iv-1]));
                row.push_back(to_string(sem[iv-1]));
            }
            if(cfg.save_mol_count) row.push_back(to_string(N_wat));
            csvw.writeRow(row);
        }
    };

    // 4. Promedio + Átomos
    class OutputMeanAtoms: public Output {
        // Media global por átomo
        vector<vector<Real>> data;
        vector<int> N_wat;
        // Media por frame y átomo
        map<int, vector<vector<Real>>> frame_sums; // frame -> [atom][4]
        map<int, vector<int>> frame_counts;        // frame -> [atom]
        int n_atoms;
        // Resultados
        vector<vector<Real>> frame_mean;
        vector<vector<Real>> sem;

    public:
        OutputMeanAtoms(int n_atoms): 
            n_atoms(n_atoms),
            data(n_atoms+1, vector<Real>(4,0.0)), 
            N_wat(n_atoms+1,0),
            frame_mean(n_atoms+1, vector<Real>(4,0.0)),
            sem(n_atoms+1, vector<Real>(4,0.0)) {}

        void accumulate(int frame, int idx, const vector<Real>& vis) override {
            for(int i= 0; i < 4; i++) data[idx][i]+= vis[i];
            N_wat[idx]++;

            if(!frame_sums.count(frame)) {
                frame_sums[frame]= vector<vector<Real>>(n_atoms+1, vector<Real>(4,0.0));
                frame_counts[frame]= vector<int>(n_atoms+1, 0);
            }
            for(int i= 0; i < 4; i++) frame_sums[frame][idx][i]+= vis[i];
            frame_counts[frame][idx]++;
        }

        void normalize(RunConfig& cfg) override {
            calculateShowViS(cfg);

            for(int i= 0; i < (int)data.size(); i++) {
                for(int j= 0; j < 4; j++) data[i][j]/= N_wat[i];
                if(cfg.units == "adimensional") normalizeDIT(data[i]);
            }

            // means[frame_idx][atom][4], solo frames con datos
            vector<vector<vector<Real>>> means;
            for(auto& [f, sums]: frame_sums) {
                vector<vector<Real>> mf(n_atoms+1, vector<Real>(4));
                for(int ia= 0; ia <= n_atoms; ia++) {
                    for(int i= 0; i < 4; i++)
                        mf[ia][i]= frame_counts[f][ia] > 0 ? sums[ia][i]/frame_counts[f][ia] : 0.0;
                    if(cfg.units == "adimensional") normalizeDIT(mf[ia]);
                }
                means.push_back(mf);
            }

            int n_frames= means.size();
            for(int ia= 0; ia <= n_atoms; ia++) {
                for(int i= 0; i < 4; i++) {
                    Real sum= 0.0;
                    for(auto& mf: means) sum+= mf[ia][i];
                    frame_mean[ia][i]= sum / n_frames;
                }
                for(int i= 0; i < 4; i++) {
                    Real sq= 0.0;
                    for(auto& mf: means) sq+= pow(mf[ia][i] - frame_mean[ia][i], 2);
                    sem[ia][i]= sqrt(sq / (n_frames * (n_frames - 1)));
                }
            }
        }
        
        void write(const string& filename, const RunConfig& cfg) const override {
            CSVWriter csvw(cfg.run_dir.string() + "/results/" + filename);

            vector<string> atoms= splitNames(cfg.atom_selection);
            atoms.insert(atoms.begin(), "All");

            vector<string> headers= {"Atom"};
            for(int iv= 1; iv <= 4; iv++) if(show_ViS[iv-1]) {
                headers.push_back("V"+to_string(iv)+"S");
                headers.push_back("V"+to_string(iv)+"S_fMean");
                headers.push_back("V"+to_string(iv)+"S_SEM");
            }
            if(cfg.save_mol_count) headers.push_back("N");
            csvw.writeHeader(headers);

            for(int ia= 0; ia < (int)atoms.size(); ia++) {
                vector<string> row= {atoms[ia]};
                for(int iv= 1; iv <= 4; iv++) if(show_ViS[iv-1]) {
                    row.push_back(to_string(data[ia][iv-1]));
                    row.push_back(to_string(frame_mean[ia][iv-1]));
                    row.push_back(to_string(sem[ia][iv-1]));
                }
                if(cfg.save_mol_count) row.push_back(to_string(N_wat[ia]));
                csvw.writeRow(row);
            }
        }
    };
}
