#include <iostream>
#include <filesystem>
#include <vector>
#include <string>
#include <CppLibAmberGromacs.hpp>
#include <chrono>
#include <memory>

namespace fs= filesystem;

void writeStatus(const fs::path& dir, const string& state, double progress, int eta_sec, int pid, const string& message, const vector<string>& results= {});

void writeStatusFromTime(const RunConfig& cfg, double& progress, int& eta, int frame_number,
                         const chrono::time_point<chrono::steady_clock>& start_time, const int num_frames) {
    progress= static_cast<double>(frame_number) / static_cast<double>(num_frames);
    auto elapsed_seconds= chrono::duration_cast<chrono::seconds>(chrono::steady_clock::now() - start_time).count();
    if(elapsed_seconds == 0) return;
    eta= static_cast<int>((static_cast<double>(elapsed_seconds) / frame_number) * (num_frames - frame_number));
    writeStatus(cfg.run_dir, "running", progress, eta, cfg.pid, "Procesando frames "+to_string(frame_number)+"-"+to_string(frame_number+19));
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

unique_ptr<Filter::GeometryConstraint> recalculateCenters(vector<string>& atom_names, double radius, TopolInfo& ti, Configuration& conf) {
    vector<Vector> centros;
    for(int m= 1; m <= ti.num_solutes; m++) {
        for(int a= 1; a <= conf.getMolec(m).getNAtoms(); a++) {
            auto[type,name,q,mass]= ti.atom_type_name_charge_mass[m-1].at(a);
            for(string name_i: atom_names) {
                if(name == name_i) {
                    centros.push_back(conf.getMolec(m).getAtom(a).getPosition());
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

void runCalculation(RunConfig& cfg) {
    TopolInfo ti= ReaderFactory::createTopologyReader(ReaderFactory::ProgramFormat::GROMACS)->readTopology((cfg.sistema_path / "system.top").string());
    CoordinateReader* cr= ReaderFactory::createCoordinateReader(ReaderFactory::ProgramFormat::GROMACS);

    auto files= CoordinateReader::getFileIterator(cfg.sistema_path.string(),"em-*.gro");
    double progress= 0.0; int eta= 0;
    auto start_time= chrono::steady_clock::now();

    writeStatus(cfg.run_dir, "running", progress, eta, cfg.pid, "Calculando límites");
    unique_ptr<Filter::GeometryConstraint> filter= calculateLimits(cfg);
    bool using_sph_autocenter= (cfg.geometry == "sphere") && cfg.sph_autocenter;
    bool filtering_monolayer= cfg.scope == "monolayer";

    vector<string> list_atom_names;
    if(using_sph_autocenter) list_atom_names= splitNames(cfg.atom_selection);

    writeStatus(cfg.run_dir, "running", progress, eta, cfg.pid, "Iniciando primeros frames...");
    
    unique_ptr<Writer::Output> writer;
    int n_atoms= cfg.sph_autocenter ? list_atom_names.size() : 0;
    int num_frames= files[files.size()-1].first;
    if(cfg.output_mode == "mean") {
        if(cfg.sph_autocenter) writer= make_unique<Writer::OutputMeanAtoms>(n_atoms);
        else                   writer= make_unique<Writer::OutputMeanSimple>();
    } else {
        if(cfg.sph_autocenter) writer= make_unique<Writer::OutputTimeAtoms>(num_frames, n_atoms);
        else                   writer= make_unique<Writer::OutputTimeSimple>(num_frames);
    }

    for(const auto& [frame_number, filename]: files) {
        if(frame_number % 20 == 0 && frame_number > 0) { writeStatusFromTime(cfg, progress, eta, frame_number, start_time, files[files.size()-1].first); }

        Configuration conf(cr, (cfg.sistema_path / filename).string(), ti);

        if(using_sph_autocenter) { filter= recalculateCenters(list_atom_names, cfg.sph_radius, ti, conf); }
        vector<bool> filtered_monolayer(conf.getNMolec(), false);
        if(filtering_monolayer) { findMonolayer(conf, ti.num_solutes, filtered_monolayer); };

        for(int m= ti.num_solutes+1; m <= conf.getNMolec(); m++) {
            if(filtering_monolayer && !filtered_monolayer[m-1]) continue;
            int res= filter->isInside(conf.getMolec(m).getPosition(), conf.getBounds());
            if(res == 0) continue;
            vector<Real> vis= conf.getInteractionsPerSite(m);

            if(using_sph_autocenter) {
                vector<int> list_i_sph= extractListFromInt(res, filter->getN());
                for(int i_sph: list_i_sph)
                    writer->accumulate(frame_number, i_sph+1, vis);
            }
            writer->accumulate(frame_number, 0, vis);
        }
    }

    writer->normalize(cfg);
    const string file_name= cfg.output_mode + (cfg.sph_autocenter ? "_atoms" : "") + ".csv";
    writer->write(file_name, cfg);
    writeStatus(cfg.run_dir, "finished", 100.0, 0, cfg.pid, "Completado exitosamente", {file_name});
}
