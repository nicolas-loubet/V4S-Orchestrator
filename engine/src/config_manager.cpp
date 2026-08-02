#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <map>
#include <stdexcept>
#include <filesystem>

namespace fs= std::filesystem;

struct TomlValue {
    enum class Type { String, Double, Bool, StringArray };
    Type           type;
    std::string    str;
    double         num  = 0.0;
    bool           flag = false;
    std::vector<std::string> arr;
};

using TomlSection = std::map<std::string, TomlValue>;
using TomlDoc     = std::map<std::string, TomlSection>;

// Resultado completo de parsear un archivo TOML: secciones simples [x] +
// arrays de tablas [[x]] (necesarios para group.toml -> [[subsystem]]).
struct TomlData {
    TomlDoc sections;
    std::map<std::string, std::vector<TomlSection>> arrays;
};

static std::string trim(const std::string& s) {
    size_t a= s.find_first_not_of(" \t\r\n");
    size_t b= s.find_last_not_of(" \t\r\n");
    return (a == std::string::npos) ? "" : s.substr(a, b-a+1);
}

static std::string stripQuotes(const std::string& s) {
    if(s.size() >= 2 && s.front() == '"' && s.back() == '"')
        return s.substr(1, s.size()-2);
    return s;
}

static TomlData parseTOML(const fs::path& path) {
    std::ifstream file(path);
    if(!file.is_open()) throw std::runtime_error("No se pudo abrir: " + path.string());

    TomlData result;
    TomlSection* currentTable= nullptr; // sección o tabla de array actualmente activa
    std::string line;

    while(std::getline(file, line)) {
        line= trim(line);
        if(line.empty() || line[0] == '#') continue;

        // Array de tablas: [[nombre]] — debe chequearse ANTES que [nombre],
        // porque también matchea el chequeo de "empieza y termina con corchete".
        if(line.size() >= 5 && line[0] == '[' && line[1] == '[' &&
           line[line.size()-1] == ']' && line[line.size()-2] == ']') {
            std::string name= trim(line.substr(2, line.size()-4));
            result.arrays[name].push_back(TomlSection{});
            currentTable= &result.arrays[name].back();
            continue;
        }

        // Sección simple: [nombre] (incluye "dataset.real", "dataset.inherent", etc.)
        if(line[0] == '[' && line.back() == ']') {
            std::string name= trim(line.substr(1, line.size()-2));
            currentTable= &result.sections[name];
            continue;
        }

        size_t eq= line.find('=');
        if(eq == std::string::npos) continue;
        if(currentTable == nullptr) continue; // key=value suelta antes de cualquier sección: se ignora

        std::string key= trim(line.substr(0, eq));
        std::string val= trim(line.substr(eq+1));

        size_t comment= val.find('#');
        if(comment != std::string::npos && !val.empty() && val[0] != '"')
            val= trim(val.substr(0, comment));

        TomlValue tv;
        if(!val.empty() && val[0] == '[') {
            while(val.find(']') == std::string::npos) {
                std::string next;
                if(!std::getline(file, next))
                    throw std::runtime_error("Array TOML sin cerrar en key: " + key);
                next= trim(next);
                size_t c= next.find('#');
                if(c != std::string::npos && !next.empty() && next[0] != '"')
                    next= trim(next.substr(0, c));
                val+= " " + next;
            }
            tv.type= TomlValue::Type::StringArray;

            size_t close= val.rfind(']');
            std::string inner= val.substr(1, close - 1);
            std::istringstream ss(inner);
            std::string token;
            while(std::getline(ss, token, ',')) {
                token= trim(token);
                if(token.empty()) continue;
                tv.arr.push_back(stripQuotes(token));
            }
        } else if(val == "true" || val == "false") {
            tv.type= TomlValue::Type::Bool;
            tv.flag= (val == "true");
        } else if(!val.empty() && val[0] == '"') {
            tv.type= TomlValue::Type::String;
            tv.str = stripQuotes(val);
        } else {
            try {
                tv.num = std::stod(val);
                tv.type= TomlValue::Type::Double;
            } catch(...) {
                tv.type= TomlValue::Type::String;
                tv.str = val;
            }
        }
        (*currentTable)[key]= tv;
    }
    return result;
}

static std::string getString(const TomlDoc& doc, const std::string& sec, const std::string& key, const std::string& def= "") {
    auto s= doc.find(sec);
    if(s == doc.end()) return def;
    auto k= s->second.find(key);
    if(k == s->second.end()) return def;
    return k->second.str.empty() ? def : k->second.str;
}

static double getDouble(const TomlDoc& doc, const std::string& sec, const std::string& key, double def= 0.0) {
    auto s= doc.find(sec);
    if(s == doc.end()) return def;
    auto k= s->second.find(key);
    if(k == s->second.end()) return def;
    return (k->second.type == TomlValue::Type::Double) ? k->second.num : def;
}

static int getInt(const TomlDoc& doc, const std::string& sec, const std::string& key, int def= 0) {
    auto s= doc.find(sec);
    if(s == doc.end()) return def;
    auto k= s->second.find(key);
    if(k == s->second.end()) return def;
    if(k->second.type == TomlValue::Type::Double) return static_cast<int>(k->second.num);
    return def;
}

static bool getBool(const TomlDoc& doc, const std::string& sec, const std::string& key, bool def= false) {
    auto s= doc.find(sec);
    if(s == doc.end()) return def;
    auto k= s->second.find(key);
    if(k == s->second.end()) return def;
    return (k->second.type == TomlValue::Type::Bool) ? k->second.flag : def;
}

static std::vector<std::string> getArray(const TomlDoc& doc, const std::string& sec, const std::string& key) {
    auto s= doc.find(sec);
    if(s == doc.end()) return {};
    auto k= s->second.find(key);
    if(k == s->second.end()) return {};
    return k->second.arr;
}

// Helper para leer un campo string de una tabla suelta (usado con las tablas
// de [[subsystem]], que no viven dentro de un TomlDoc sino de un vector<TomlSection>).
static std::string getStringFromTable(const TomlSection& table, const std::string& key, const std::string& def= "") {
    auto k= table.find(key);
    if(k == table.end()) return def;
    return k->second.str.empty() ? def : k->second.str;
}

static std::vector<std::string> splitNames(std::string names, const std::string delimiter=" ") {
    std::vector<std::string> res;
    size_t pos= 0;
    while((pos= names.find(delimiter)) != std::string::npos) {
        res.push_back(names.substr(0,pos));
        names.erase(0, pos+delimiter.length());
    }
    res.push_back(names);
    return res;
}

// ---------------------------------------------------------------------------
// system.toml / group.toml
// ---------------------------------------------------------------------------

struct DatasetInfo {
    bool        enabled= false;
    std::string path;
    std::string prefix;
    int         n_confs= 0;
    int         n_converged= 0;
    std::string summary;
};

struct SystemInfo {
    std::string name;
    std::string description;
    double      total_simulated_ns= 0.0;
    double      snapshot_interval_ps= 0.0;
    std::string ensemble;
    DatasetInfo dataset_real;
    DatasetInfo dataset_inherent;
};

struct SubsystemRef {
    std::string label;
    std::string path; // relativo a la carpeta del grupo
};

struct GroupInfo {
    std::string name;
    std::string description;
    std::string variable;
    bool        afecta_conf= false;
    std::vector<SubsystemRef> subsystems;
};

static SystemInfo parseSystemToml(const fs::path& system_toml_path) {
    if(!fs::exists(system_toml_path))
        throw std::runtime_error("No existe system.toml: " + system_toml_path.string());

    TomlData doc= parseTOML(system_toml_path);
    SystemInfo si;

    si.name                 = getString(doc.sections, "info", "name");
    si.description          = getString(doc.sections, "info", "description");
    si.total_simulated_ns   = getDouble(doc.sections, "simulation", "total_simulated_ns");
    si.snapshot_interval_ps = getDouble(doc.sections, "simulation", "snapshot_interval_ps");
    si.ensemble             = getString(doc.sections, "simulation", "ensemble");

    // Dataset real: siempre existe (es la trayectoria de producción tal cual).
    si.dataset_real.enabled = true;
    si.dataset_real.path    = getString(doc.sections, "dataset.real", "path", "estabilizacion/confs");
    si.dataset_real.prefix  = getString(doc.sections, "dataset.real", "prefix", "conf-");
    si.dataset_real.n_confs = getInt   (doc.sections, "dataset.real", "n_confs");

    // Dataset inherente: opcional, depende de si se corrió minimización de confs.
    si.dataset_inherent.enabled     = getBool  (doc.sections, "dataset.inherent", "enabled", false);
    si.dataset_inherent.path        = getString(doc.sections, "dataset.inherent", "path");
    si.dataset_inherent.prefix      = getString(doc.sections, "dataset.inherent", "prefix", "em-");
    si.dataset_inherent.n_confs     = getInt   (doc.sections, "dataset.inherent", "n_confs");
    si.dataset_inherent.n_converged = getInt   (doc.sections, "dataset.inherent", "n_converged");
    si.dataset_inherent.summary     = getString(doc.sections, "dataset.inherent", "summary");

    return si;
}

static GroupInfo parseGroupToml(const fs::path& group_toml_path) {
    if(!fs::exists(group_toml_path))
        throw std::runtime_error("No existe group.toml: " + group_toml_path.string());

    TomlData doc= parseTOML(group_toml_path);
    GroupInfo gi;

    gi.name         = getString(doc.sections, "group", "name");
    gi.description  = getString(doc.sections, "group", "description");
    gi.variable     = getString(doc.sections, "group", "variable");
    gi.afecta_conf  = getBool  (doc.sections, "group", "afecta_conf", false);

    auto it= doc.arrays.find("subsystem");
    if(it != doc.arrays.end()) {
        for(const TomlSection& tbl: it->second) {
            SubsystemRef sr;
            sr.label= getStringFromTable(tbl, "label");
            sr.path = getStringFromTable(tbl, "path");
            if(sr.label.empty() || sr.path.empty())
                throw std::runtime_error("[[subsystem]] con 'label' o 'path' faltante en " + group_toml_path.string());
            gi.subsystems.push_back(sr);
        }
    }
    if(gi.subsystems.empty())
        throw std::runtime_error("group.toml sin ningún [[subsystem]]: " + group_toml_path.string());

    return gi;
}

// ---------------------------------------------------------------------------
// Resolución: de "modo sistema/grupo" + "dataset elegido" a la lista concreta
// de sistemas sobre los que hay que calcular. Uniforma el caso sistema
// individual (1 elemento) y grupo (N elementos), para que calculator.cpp
// pueda iterar siempre de la misma forma.
// ---------------------------------------------------------------------------

struct ResolvedSystem {
    std::string label;       // "" en modo sistema individual
    fs::path    root;        // carpeta con system.top y system.toml
    fs::path    dataset_dir; // root / dataset.path
    std::string prefix;      // dataset.prefix
};

// system.toml puede estar en 'path' directo, o -si 'path' quedó apuntando
// un nivel de más (ej. a la carpeta de estabilización en vez de a la raíz
// del sistema)- un nivel arriba de 'path'. Devuelve la ruta real encontrada;
// el CALLER debe usar su .parent_path() como raíz real del sistema (que
// puede no ser exactamente 'path').
static fs::path resolveSystemTomlPath(const fs::path& path) {
    fs::path direct= path / "system.toml";
    if(fs::exists(direct)) return direct;

    fs::path one_up= path.parent_path() / "system.toml";
    if(fs::exists(one_up)) return one_up;

    throw std::runtime_error("No se encontró system.toml ni en '" + direct.string() +
                              "' ni un nivel arriba en '" + one_up.string() + "'");
}

// Valida el dataset elegido contra el system.toml de un sistema puntual y,
// si es válido, arma su ResolvedSystem. Corta duro (excepción) si el dataset
// pedido no está disponible — es la validación acordada como responsabilidad
// del motor, no de Python.
static ResolvedSystem resolveOne(const fs::path& path, const std::string& dataset_which, const std::string& label= "") {
    fs::path system_toml_path= resolveSystemTomlPath(path);
    fs::path root= system_toml_path.parent_path(); // raíz real -- puede diferir de 'path' si hubo fallback

    SystemInfo si= parseSystemToml(system_toml_path);

    const DatasetInfo* ds= nullptr;
    if(dataset_which == "real")           ds= &si.dataset_real;
    else if(dataset_which == "inherent")  ds= &si.dataset_inherent;
    else throw std::runtime_error("dataset.which desconocido: '" + dataset_which + "' (esperado 'real' o 'inherent')");

    if(!ds->enabled) {
        std::string sistema_desc= label.empty() ? root.string() : (label + " (" + root.string() + ")");
        throw std::runtime_error("El sistema '" + sistema_desc + "' no tiene dataset '" + dataset_which + "' habilitado");
    }

    ResolvedSystem rs;
    rs.label      = label;
    rs.root       = root;
    rs.dataset_dir= root / ds->path;
    rs.prefix     = ds->prefix;
    return rs;
}

static std::vector<ResolvedSystem> resolveSystems(const fs::path& path, const std::string& modo, const std::string& dataset_which) {
    std::vector<ResolvedSystem> out;

    if(modo == "sistema") {
        out.push_back(resolveOne(path, dataset_which));
    } else if(modo == "grupo") {
        GroupInfo gi= parseGroupToml(path / "group.toml");
        out.reserve(gi.subsystems.size());
        for(const SubsystemRef& sub: gi.subsystems)
            out.push_back(resolveOne(path / sub.path, dataset_which, sub.label));
    } else {
        throw std::runtime_error("meta.modo desconocido: '" + modo + "' (esperado 'sistema' o 'grupo')");
    }

    return out;
}

// ---------------------------------------------------------------------------
// RunConfig
// ---------------------------------------------------------------------------

struct RunConfig {
    fs::path    run_dir;
    int         pid;

    std::string run_id;
    std::string sistema_id;
    std::string modo;           // "sistema" | "grupo"
    fs::path    path;           // raíz del sistema, o de la carpeta del grupo
    std::string dataset_which;  // "real" | "inherent"

    std::vector<ResolvedSystem> systems; // 1 elemento si modo=="sistema", N si modo=="grupo"

    std::vector<std::string> params;
    std::string units;
    std::string output_mode;
    bool        save_mol_count= false;

    std::string scope;
    std::string atom_selection;

    std::string geometry;
    double      cube_xmin, cube_xmax, cube_ymin, cube_ymax, cube_zmin, cube_zmax;
    std::string cyl_axis;
    double      cyl_c1, cyl_c2, cyl_radius, cyl_hmin, cyl_hmax;
    double      sph_cx, sph_cy, sph_cz, sph_radius;
    bool        sph_autocenter= false;

    bool        all_mode= false;


    void init(fs::path dir) {
        run_dir= dir;
        pid= getInt(parseTOML(dir / "status.toml").sections, "status", "pid");

        TomlDoc doc= parseTOML(run_dir / "run.toml").sections;

        run_id        = getString(doc, "meta", "run_id");
        sistema_id    = getString(doc, "meta", "sistema_id");
        modo          = getString(doc, "meta", "modo", "sistema");
        path          = getString(doc, "meta", "path");

        dataset_which = getString(doc, "dataset", "which", "real");

        // Acá se dispara toda la lectura de system.toml/group.toml y la
        // validación dura del dataset elegido. Si algo no cierra, tira
        // runtime_error y main.cpp lo escribe como status="error".
        systems= resolveSystems(path, modo, dataset_which);

        params         = getArray (doc, "parametros", "params");
        units          = getString(doc, "parametros", "units",          "kJ/mol");
        output_mode    = getString(doc, "parametros", "output_mode",    "mean");
        save_mol_count = getBool  (doc, "parametros", "save_mol_count", false);

        scope          = getString(doc, "agregacion", "scope",          "all");
        atom_selection = getString(doc, "agregacion", "atom_selection", "");
        all_mode= (scope == "selection") && (atom_selection == "ALL");

        geometry       = getString(doc, "geometria", "type", "cube");
        cube_xmin      = getDouble(doc, "geometria", "xmin");
        cube_xmax      = getDouble(doc, "geometria", "xmax");
        cube_ymin      = getDouble(doc, "geometria", "ymin");
        cube_ymax      = getDouble(doc, "geometria", "ymax");
        cube_zmin      = getDouble(doc, "geometria", "zmin");
        cube_zmax      = getDouble(doc, "geometria", "zmax");

        cyl_axis       = getString(doc, "geometria", "axis",   "Z");
        cyl_c1         = getDouble(doc, "geometria", "c1");
        cyl_c2         = getDouble(doc, "geometria", "c2");
        cyl_radius     = getDouble(doc, "geometria", "radius");
        cyl_hmin       = getDouble(doc, "geometria", "hmin");
        cyl_hmax       = getDouble(doc, "geometria", "hmax");

        sph_cx         = getDouble(doc, "geometria", "cx");
        sph_cy         = getDouble(doc, "geometria", "cy");
        sph_cz         = getDouble(doc, "geometria", "cz");
        sph_radius     = getDouble(doc, "geometria", "radius");
        sph_autocenter = getBool  (doc, "geometria", "autocenter", false);
    }
};
