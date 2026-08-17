#include <iostream>
#include <filesystem>
#include <vector>
#include <string>
#include <CppLibAmberGromacs.hpp>
#include <chrono>
#include <memory>

namespace fs= filesystem;

void writeStatus(const fs::path& dir, const string& state, double progress, int eta_sec, int pid, const string& message, const vector<string>& results= {});

// progress/eta ahora se calculan sobre el TOTAL de frames de la corrida
// (todos los sistemas sumados), no solo del sistema que se está procesando
// en este momento -- frame_offset es cuántos frames ya se procesaron de
// sistemas anteriores, total_frames es la suma de todos.
void writeStatusFromTime(const RunConfig& cfg, double& progress, int& eta, int frame_number, int frame_offset, int total_frames,
                         const chrono::time_point<chrono::steady_clock>& start_time, const string& label_prefix= "") {
    int global_frame= frame_offset + frame_number;
    progress= static_cast<double>(global_frame) / static_cast<double>(total_frames);
    auto elapsed_seconds= chrono::duration_cast<chrono::seconds>(chrono::steady_clock::now() - start_time).count();
    if(elapsed_seconds == 0) return;
    // Tasa medida en ESTE sistema (start_time se resetea por sistema), extrapolada
    // a lo que falta en TODA la corrida (no solo en el sistema actual).
    eta= static_cast<int>((static_cast<double>(elapsed_seconds) / frame_number) * (total_frames - global_frame));
    writeStatus(cfg.run_dir, "running", progress, eta, cfg.pid, label_prefix+"Procesando frames "+to_string(frame_number)+"-"+to_string(frame_number+19));
}

unique_ptr<Filter::GeometryConstraint> calculateLimits(RunConfig& cfg) {
    unique_ptr<Filter::GeometryConstraint> filter;
    if(cfg.geometry == "cube") {
        filter= make_unique<Filter::BoxConstraint>(
            Geometrics::Box(cfg.cube_xmin, cfg.cube_ymin, cfg.cube_zmin, cfg.cube_xmax, cfg.cube_ymax, cfg.cube_zmax)
        );
    } else if(cfg.geometry == "cylinder") {
        Vector dir, point;
        if(cfg.cyl_axis == "X") {dir.x= 1; point= Vector(cfg.cyl_hmin, cfg.cyl_c1, cfg.cyl_c2);}
        else if(cfg.cyl_axis == "Y") {dir.y= 1; point= Vector(cfg.cyl_c1, cfg.cyl_hmin, cfg.cyl_c2);}
        else if(cfg.cyl_axis == "Z") {dir.z= 1; point= Vector(cfg.cyl_c1, cfg.cyl_c2, cfg.cyl_hmin);}
        else throw runtime_error("Cylinder axis not supported");

        filter= make_unique<Filter::CylinderConstraint>(
            Geometrics::Line(dir, point), cfg.cyl_radius, cfg.cyl_hmin, cfg.cyl_hmax
        );
    } else if(cfg.geometry == "sphere") {
        if(cfg.sph_autocenter) return nullptr;
        filter= make_unique<Filter::SphereConstraint>(
            Vector(cfg.sph_cx, cfg.sph_cy, cfg.sph_cz), cfg.sph_radius
        );
    } else {
        throw runtime_error("Geometry not supported");
    }
    return filter;
}

#ifndef DIFF_RESID_MOLECULE
#error "El motor asume compilacion con -DDIFF_RESID_MOLECULE (molecule_sequence/name_to_diffid en TopolInfo); sin eso, buildSoluteDiffIdMap no puede armar el mapeo instancia->tipo."
#endif

// atom_type_name_charge_mass está indexado por TIPO de molécula (diff_id),
// no por instancia -- si hay varias copias del mismo tipo de soluto (ej. 8
// iones CL, todos el mismo tipo), la instancia m NO es lo mismo que m-1 como
// índice de tipo. Este mapeo se arma UNA VEZ por sistema (no cambia entre
// frames, es pura topología) recorriendo ti.molecule_sequence -bloques en el
// mismo orden que [molecules] en el .top- y resolviendo cada bloque a su
// diff_id vía ti.name_to_diffid.
vector<int> buildSoluteDiffIdMap(const TopolInfo& ti) {
    vector<int> diff_id_of(ti.num_solutes + 1, -1); // 1-indexado; [0] no se usa

    int instance= 1;
    for(const auto& [name, count]: ti.molecule_sequence) {
        auto it= ti.name_to_diffid.find(name);
        if(it == ti.name_to_diffid.end())
            throw runtime_error("No se encontró diff_id para la molécula '" + name + "' en name_to_diffid");

        for(int k= 0; k < count && instance <= ti.num_solutes; k++, instance++)
            diff_id_of[instance]= it->second;

        if(instance > ti.num_solutes) break;
    }

    if(instance <= ti.num_solutes)
        throw runtime_error("molecule_sequence no cubre las " + to_string(ti.num_solutes) +
                             " instancias de soluto esperadas (solo llegó a " + to_string(instance-1) + ")");

    return diff_id_of;
}

unique_ptr<Filter::GeometryConstraint> recalculateCenters(vector<string>& atom_names, double radius, TopolInfo& ti, Configuration& conf, const vector<int>& diff_id_of) {
    vector<Vector> centros;
    for(int m= 1; m <= ti.num_solutes; m++) {
        for(int a= 1; a <= conf.getMolec(m).getNAtoms(); a++) {
            auto[type,name,q,mass]= ti.atom_type_name_charge_mass.at(diff_id_of.at(m)).at(a);
            for(string name_i: atom_names) {
                if(name == name_i) {
                    centros.push_back(conf.getMolec(m).getAtom(a).getPosition());
                    break; // un solo centro por átomo de soluto, sin importar cuántas veces
                           // aparezca su nombre repetido en atom_names (ej. modo ALL, donde
                           // "CA"/"N"/"C"/"O" se repiten una vez por residuo)
                }
            }
        }
    }
    return make_unique<Filter::MultiSphereConstraint>(centros, radius);
}

vector<int> extractListFromInt(const int res, const int N) {
    vector<int> ls;
    bool extra_one= true;
    int copy_res= res;
    while(copy_res > N || extra_one) {
        ls.push_back((copy_res-1) % N);
        if(copy_res <= N) extra_one= false;
        copy_res/= N;
    }
    reverse(ls.begin(), ls.end());
    return ls;
}

void findMonolayer(Configuration& conf, const int N_SOLUTES, vector<bool>& monolayer) {
    for(int solute= 1; solute <= N_SOLUTES; solute++) {
        for(int a= 1; a <= conf.getMolec(solute).getNAtoms(); a++) {
            Real max_radii= 100.0;
            int id_molecule= -1;
            for(int m= N_SOLUTES+1; m <= conf.getNMolec(); m++) {
                Real d= conf.getMolec(m).distanceTo(conf.getMolec(solute).getAtom(a), conf.getBounds());
                if(d > max_radii) continue;
                max_radii= d;
                id_molecule= m;
            }
            monolayer[id_molecule-1]= (id_molecule != -1) || monolayer[id_molecule];
        }
    }
}

/*
// Testing
string escribirGRO(Vector pos, int res) {
    stringstream ss;
    ss << setw(5) << res+1
       << "WAT     OW"
       << setw(5) << res+240
       << fixed << setprecision(3) << setw(8) << pos.x/10
       << fixed << setprecision(3) << setw(8) << pos.y/10
       << fixed << setprecision(3) << setw(8) << pos.z/10 << endl;
    return ss.str();
}
*/

// La librería externa, si no encuentra el .top en la ruta dada, no tira
// excepción: imprime "Topology not found" y sigue con un TopolInfo vacío,
// que después explota más abajo con un críptico "map::at" al indexar datos
// que nunca se cargaron. Por eso acá chequeamos existencia ANTES de
// llamarla, y fallamos con mensaje claro si no aparece.
//
// system.top vive junto al dataset, no junto a system.toml. Pero "junto al
// dataset" es ambiguo cuando which=="inherent": el .top puede estar cerca de
// confs_min/ (donde estamos parados) O cerca de estabilizacion/confs/ (el
// dataset real), que son carpetas HERMANAS, no una contenida en la otra. La
// topología describe el sistema físico, no cambia entre datasets, así que
// probamos las 4 combinaciones: dataset elegido (+contenedora) y dataset
// real (+contenedora) -- si which=="real" ya son las mismas dos, sin costo
// extra real (fs::exists es barato).
static fs::path resolveTopologyPath(const ResolvedSystem& rs) {
    const vector<fs::path> candidates= {
        rs.dataset_dir / "system.top",
        rs.dataset_dir.parent_path() / "system.top",
        rs.real_dataset_dir / "system.top",
        rs.real_dataset_dir.parent_path() / "system.top",
    };

    for(const fs::path& c: candidates) if(fs::exists(c)) return c;

    string tried;
    for(const fs::path& c: candidates) tried+= "\n  - " + c.string();
    throw runtime_error("No se encontró system.top en ninguna de estas rutas:" + tried);
}

// Un sistema resuelto junto con la lista de frames ya globbeada. Se
// precomputa una sola vez en el orquestador (runCalculation) para no
// re-globbear adentro de runCalculationForSystem, y para poder conocer de
// antemano cuántos frames hay en TOTAL entre todos los sistemas.
struct SystemFiles {
    ResolvedSystem rs;
    vector<pair<int,string>> files;
};

// Corre el pipeline completo (topología + filtro + loop de frames) para UN
// sistema ya resuelto. No escribe a disco: devuelve el Writer normalizado
// para que el orquestador decida dónde y cómo persistirlo (archivo único, o
// combinado entre varios sistemas en modo grupo).
// frame_offset/total_frames son solo para reportar progreso agregado en
// status.toml -- no afectan el cálculo en sí.
unique_ptr<Writer::Output> runCalculationForSystem(RunConfig& cfg, const ResolvedSystem& rs,
                                                    const vector<pair<int,string>>& files,
                                                    int frame_offset, int total_frames) {
    const string label_prefix= rs.label.empty() ? "" : ("[" + rs.label + "] ");

    TopolInfo ti= ReaderFactory::createTopologyReader(ReaderFactory::ProgramFormat::GROMACS)->readTopology(resolveTopologyPath(rs).string());
    CoordinateReader* cr= ReaderFactory::createCoordinateReader(ReaderFactory::ProgramFormat::GROMACS);
    const vector<int> diff_id_of= buildSoluteDiffIdMap(ti); // instancia de soluto -> tipo, una sola vez

    double progress= 0.0; int eta= 0;
    auto start_time= chrono::steady_clock::now();

    writeStatus(cfg.run_dir, "running", progress, eta, cfg.pid, label_prefix+"Calculando límites");
    unique_ptr<Filter::GeometryConstraint> filter= calculateLimits(cfg);
    bool using_sph_autocenter= (cfg.geometry == "sphere") && cfg.sph_autocenter;
    bool filtering_monolayer= cfg.scope == "monolayer";

    vector<string> list_atom_names;

    if(cfg.all_mode) {
        try {
            Configuration conf_0(cr, (rs.dataset_dir / files[0].second).string(), ti);
            for(int m= 1; m <= ti.num_solutes; m++) {
                int diff_id;
                try {
                    diff_id= diff_id_of.at(m);
                } catch(const exception& e) {
                    throw runtime_error("diff_id_of.at(m=" + to_string(m) + ") fuera de rango (diff_id_of.size()=" +
                                         to_string(diff_id_of.size()) + "): " + e.what());
                }

                int n_atoms_m;
                try {
                    n_atoms_m= conf_0.getMolec(m).getNAtoms();
                } catch(const exception& e) {
                    throw runtime_error("conf_0.getMolec(m=" + to_string(m) + ") falló: " + e.what());
                }

                for(int a= 1; a <= n_atoms_m; a++) {
                    try {
                        list_atom_names.push_back( get<1>(ti.atom_type_name_charge_mass.at(diff_id).at(a)) );
                    } catch(const exception& e) {
                        throw runtime_error("atom_type_name_charge_mass.at(diff_id=" + to_string(diff_id) +
                                             ").at(a=" + to_string(a) + ") para instancia m=" + to_string(m) +
                                             " (n_atoms_m=" + to_string(n_atoms_m) +
                                             ", atom_type_name_charge_mass.size()=" + to_string(ti.atom_type_name_charge_mass.size()) +
                                             "): " + e.what());
                    }
                }
            }
            cfg.atom_selection= "";
            for(string name: list_atom_names) cfg.atom_selection+= name + " ";
            cfg.atom_selection= cfg.atom_selection.substr(0, cfg.atom_selection.size()-1);
        } catch(const exception& e) {
            throw runtime_error(label_prefix + "Fallo armando la selección ALL (num_solutes=" +
                                 to_string(ti.num_solutes) + "): " + e.what());
        }
    } else if(using_sph_autocenter) list_atom_names= splitNames(cfg.atom_selection);

    writeStatus(cfg.run_dir, "running", progress, eta, cfg.pid, label_prefix+"Iniciando primeros frames...");

    unique_ptr<Writer::Output> writer;
    int n_atoms= cfg.sph_autocenter ? list_atom_names.size() : 0;
    int num_frames= files.empty() ? 0 : files[files.size()-1].first; // LOCAL a este sistema: dimensiona el writer, no el progreso
    if(cfg.output_mode == "mean") {
        if(cfg.sph_autocenter) writer= make_unique<Writer::OutputMeanAtoms>(n_atoms);
        else                   writer= make_unique<Writer::OutputMeanSimple>();
    } else {
        if(cfg.sph_autocenter) writer= make_unique<Writer::OutputTimeAtoms>(num_frames, n_atoms);
        else                   writer= make_unique<Writer::OutputTimeSimple>(num_frames);
    }

    for(const auto& [frame_number, filename]: files) {
        if(frame_number % 20 == 0 && frame_number > 0) {
            writeStatusFromTime(cfg, progress, eta, frame_number, frame_offset, total_frames, start_time, label_prefix);
        }

        const fs::path frame_path= rs.dataset_dir / filename;

        // Cada llamada a la librería externa queda en su propio try/catch,
        // con un mensaje distinto según la etapa. Así, si algo falla (por
        // ejemplo por un mismatch entre topología y frame), sabemos EXACTO
        // en qué paso pasó -leyendo el .gro, calculando monolayer, aplicando
        // el filtro geométrico, o calculando interacciones- sin necesitar
        // gdb ni reproducir a mano.
        unique_ptr<Configuration> conf_ptr;
        try {
            writeStatus(cfg.run_dir, "running", progress, eta, cfg.pid,
                        label_prefix+"Frame "+to_string(frame_number)+": leyendo "+filename+"...");
            conf_ptr= make_unique<Configuration>(cr, frame_path.string(), ti);
        } catch(const exception& e) {
            throw runtime_error(label_prefix + "Fallo LEYENDO frame " + to_string(frame_number) +
                                 " (" + frame_path.string() + "): " + e.what());
        }
        Configuration& conf= *conf_ptr;

        if(using_sph_autocenter) {
            try {
                filter= recalculateCenters(list_atom_names, cfg.sph_radius, ti, conf, diff_id_of);
            } catch(const exception& e) {
                throw runtime_error(label_prefix + "Fallo RECALCULANDO CENTROS en frame " + to_string(frame_number) + ": " + e.what());
            }
        }

        vector<bool> filtered_monolayer(conf.getNMolec(), false);
        if(filtering_monolayer) {
            try {
                writeStatus(cfg.run_dir, "running", progress, eta, cfg.pid,
                            label_prefix+"Frame "+to_string(frame_number)+": calculando monolayer ("+
                            to_string(conf.getNMolec())+" moleculas totales)...");
                findMonolayer(conf, ti.num_solutes, filtered_monolayer);
            } catch(const exception& e) {
                throw runtime_error(label_prefix + "Fallo calculando MONOLAYER en frame " + to_string(frame_number) +
                                     " (num_solutes=" + to_string(ti.num_solutes) + ", nMolec=" + to_string(conf.getNMolec()) + "): " + e.what());
            }
        }

        writeStatus(cfg.run_dir, "running", progress, eta, cfg.pid,
                    label_prefix+"Frame "+to_string(frame_number)+": calculando interacciones ("+
                    to_string(conf.getNMolec()-ti.num_solutes)+" moleculas a evaluar)...");

        for(int m= ti.num_solutes+1; m <= conf.getNMolec(); m++) {
            if(filtering_monolayer && !filtered_monolayer[m-1]) continue;

            int res;
            try {
                res= filter->isInside(conf.getMolec(m).getPosition(), conf.getBounds());
            } catch(const exception& e) {
                throw runtime_error(label_prefix + "Fallo en FILTRO GEOMÉTRICO, frame " + to_string(frame_number) +
                                     " molécula " + to_string(m) + ": " + e.what());
            }
            if(res == 0) continue;

            vector<Real> vis;
            try {
                vis= conf.getInteractionsPerSite(m);
            } catch(const exception& e) {
                throw runtime_error(label_prefix + "Fallo en getInteractionsPerSite, frame " + to_string(frame_number) +
                                     " molécula " + to_string(m) + " (de " + to_string(conf.getNMolec()) +
                                     ", num_solutes=" + to_string(ti.num_solutes) + "): " + e.what());
            }

            if(using_sph_autocenter) {
                //vector<int> list_i_sph= extractListFromInt(res, filter->getN());
                //for(int i_sph: list_i_sph)
                //    writer->accumulate(frame_number, i_sph+1, vis);
                writer->accumulate(frame_number, res, vis);
            }
            writer->accumulate(frame_number, 0, vis);
        }
    }

    writer->normalize(cfg);
    return writer;
}

// Orquesta sobre cfg.systems (1 elemento en modo sistema individual, N en
// modo grupo). Precomputa los frames de cada sistema una sola vez (para el
// progreso agregado, ver arriba), y después:
//  - grupo + output_mode=="mean": un único CSV combinado (OutputMeanGrouped),
//    con columna 'label' al frente.
//  - cualquier otro caso: cada sistema escribe su propio archivo -- en la
//    raíz de results/ si es un único sistema (idéntico a como era antes), o
//    bajo results/<label>/ si hay varios (para no pisarse entre sí).
void runCalculation(RunConfig& cfg) {
    const bool grouped= cfg.systems.size() > 1;
    const string base_file_name= cfg.output_mode + (cfg.sph_autocenter ? "_atoms" : "") + ".csv";
    vector<string> written_files;

    vector<SystemFiles> all_files;
    all_files.reserve(cfg.systems.size());
    int total_frames= 0;
    for(const ResolvedSystem& rs: cfg.systems) {
        auto files= CoordinateReader::getFileIterator(rs.dataset_dir.string(), rs.prefix + "*.gro");
        total_frames+= files.empty() ? 0 : files.back().first;
        all_files.push_back(SystemFiles{rs, move(files)});
    }

    const bool combine_mean= grouped && cfg.output_mode == "mean";
    Writer::OutputMeanGrouped combined;
    int frame_offset= 0;

    for(const SystemFiles& sf: all_files) {
        unique_ptr<Writer::Output> writer= runCalculationForSystem(cfg, sf.rs, sf.files, frame_offset, total_frames);
        frame_offset+= sf.files.empty() ? 0 : sf.files.back().first;

        if(combine_mean) {
            auto* row_source= dynamic_cast<Writer::MeanRowSource*>(writer.get());
            if(!row_source) throw runtime_error("Writer inesperado para output_mode=\"mean\" (esto no debería pasar)");
            combined.addSubsystem(sf.rs.label, *row_source, cfg);
        } else {
            string relative_path= base_file_name;
            if(grouped) {
                fs::create_directories(cfg.run_dir / "results" / sf.rs.label);
                relative_path= sf.rs.label + "/" + base_file_name;
            }
            writer->write(relative_path, cfg);
            written_files.push_back(relative_path);
        }
    }

    if(combine_mean) {
        combined.write(base_file_name, cfg);
        written_files.push_back(base_file_name);
    }

    writeStatus(cfg.run_dir, "finished", 100.0, 0, cfg.pid, "Completado exitosamente", written_files);
}
