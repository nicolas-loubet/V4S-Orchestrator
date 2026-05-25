#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <map>
#include <stdexcept>
#include <filesystem>

namespace fs = std::filesystem;

// ============================================================
// Minimal TOML reader
// Soporta: string, double, bool, arrays de strings
// ============================================================

struct TomlValue {
    enum class Type { String, Double, Bool, StringArray };
    Type        type;
    std::string str;
    double      num  = 0.0;
    bool        flag = false;
    std::vector<std::string> arr;
};

using TomlSection = std::map<std::string, TomlValue>;
using TomlDoc     = std::map<std::string, TomlSection>;

static std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    size_t b = s.find_last_not_of(" \t\r\n");
    return (a == std::string::npos) ? "" : s.substr(a, b - a + 1);
}

static std::string stripQuotes(const std::string& s) {
    if (s.size() >= 2 && s.front() == '"' && s.back() == '"')
        return s.substr(1, s.size() - 2);
    return s;
}

TomlDoc parseTOML(const fs::path& path) {
    std::ifstream file(path);
    if (!file.is_open())
        throw std::runtime_error("No se pudo abrir: " + path.string());

    TomlDoc     doc;
    std::string currentSection;
    std::string line;

    while (std::getline(file, line)) {
        line = trim(line);
        if (line.empty() || line[0] == '#') continue;

        // Seccion: [nombre]
        if (line[0] == '[' && line.back() == ']') {
            currentSection = trim(line.substr(1, line.size() - 2));
            continue;
        }

        // Clave = valor
        size_t eq = line.find('=');
        if (eq == std::string::npos) continue;

        std::string key = trim(line.substr(0, eq));
        std::string val = trim(line.substr(eq + 1));

        // Remover comentario inline
        size_t comment = val.find('#');
        if (comment != std::string::npos && val[0] != '"')
            val = trim(val.substr(0, comment));

        TomlValue tv;

        if (!val.empty() && val[0] == '[') {
            // Array de strings: ["a", "b", ...]
            tv.type = TomlValue::Type::StringArray;
            std::string inner = val.substr(1, val.rfind(']') - 1);
            std::istringstream ss(inner);
            std::string token;
            while (std::getline(ss, token, ','))
                tv.arr.push_back(stripQuotes(trim(token)));
        } else if (val == "true" || val == "false") {
            tv.type = TomlValue::Type::Bool;
            tv.flag = (val == "true");
        } else if (!val.empty() && val[0] == '"') {
            tv.type = TomlValue::Type::String;
            tv.str  = stripQuotes(val);
        } else {
            try {
                tv.num  = std::stod(val);
                tv.type = TomlValue::Type::Double;
            } catch (...) {
                tv.type = TomlValue::Type::String;
                tv.str  = val;
            }
        }

        doc[currentSection][key] = tv;
    }

    return doc;
}

// ============================================================
// Helpers de acceso seguro
// ============================================================

static std::string getString(const TomlDoc& doc,
                             const std::string& sec,
                             const std::string& key,
                             const std::string& def = "") {
    auto s = doc.find(sec);
    if (s == doc.end()) return def;
    auto k = s->second.find(key);
    if (k == s->second.end()) return def;
    return k->second.str.empty() ? def : k->second.str;
}

static double getDouble(const TomlDoc& doc,
                        const std::string& sec,
                        const std::string& key,
                        double def = 0.0) {
    auto s = doc.find(sec);
    if (s == doc.end()) return def;
    auto k = s->second.find(key);
    if (k == s->second.end()) return def;
    return (k->second.type == TomlValue::Type::Double) ? k->second.num : def;
}

static bool getBool(const TomlDoc& doc,
                    const std::string& sec,
                    const std::string& key,
                    bool def = false) {
    auto s = doc.find(sec);
    if (s == doc.end()) return def;
    auto k = s->second.find(key);
    if (k == s->second.end()) return def;
    return (k->second.type == TomlValue::Type::Bool) ? k->second.flag : def;
}

static std::vector<std::string> getArray(const TomlDoc& doc,
                                          const std::string& sec,
                                          const std::string& key) {
    auto s = doc.find(sec);
    if (s == doc.end()) return {};
    auto k = s->second.find(key);
    if (k == s->second.end()) return {};
    return k->second.arr;
}

// ============================================================
// Estructura de configuracion
// ============================================================

struct RunConfig {
    // [meta]
    std::string run_id;
    std::string sistema_id;
    std::string sistema_path;

    // [parametros]
    std::vector<std::string> params;      // ["V1S", "V4S", ...]
    std::string              units;        // "kJ/mol" | "adimensional"
    std::string              output_mode;  // "mean" | "time_series"
    bool                     save_mol_count = false;

    // [agregacion]
    std::string scope;          // "all" | "monolayer" | "selection"
    std::string atom_selection; // nombres de atomos, vacio si scope != selection

    // [geometria]
    std::string geometry;  // "cube" | "cylinder" | "sphere"

    // Cubo
    double cube_xmin, cube_xmax;
    double cube_ymin, cube_ymax;
    double cube_zmin, cube_zmax;

    // Cilindro
    std::string cyl_axis;
    double      cyl_c1, cyl_c2;
    double      cyl_radius;
    double      cyl_hmin, cyl_hmax;

    // Esfera
    double sph_cx, sph_cy, sph_cz;
    double sph_radius;
    bool   sph_autocenter = false;
};

RunConfig loadRunConfig(const fs::path& toml_path) {
    TomlDoc doc = parseTOML(toml_path);
    RunConfig cfg;

    cfg.run_id        = getString(doc, "meta", "run_id");
    cfg.sistema_id    = getString(doc, "meta", "sistema_id");
    cfg.sistema_path  = getString(doc, "meta", "sistema_path");

    cfg.params        = getArray (doc, "parametros", "params");
    cfg.units         = getString(doc, "parametros", "units", "kJ/mol");
    cfg.output_mode   = getString(doc, "parametros", "output_mode", "mean");
    cfg.save_mol_count= getBool  (doc, "parametros", "save_mol_count", false);

    cfg.scope          = getString(doc, "agregacion", "scope", "all");
    cfg.atom_selection = getString(doc, "agregacion", "atom_selection", "");

    cfg.geometry  = getString(doc, "geometria", "type", "cube");

    cfg.cube_xmin = getDouble(doc, "geometria", "xmin");
    cfg.cube_xmax = getDouble(doc, "geometria", "xmax");
    cfg.cube_ymin = getDouble(doc, "geometria", "ymin");
    cfg.cube_ymax = getDouble(doc, "geometria", "ymax");
    cfg.cube_zmin = getDouble(doc, "geometria", "zmin");
    cfg.cube_zmax = getDouble(doc, "geometria", "zmax");

    cfg.cyl_axis   = getString(doc, "geometria", "axis",   "Z");
    cfg.cyl_c1     = getDouble(doc, "geometria", "c1");
    cfg.cyl_c2     = getDouble(doc, "geometria", "c2");
    cfg.cyl_radius = getDouble(doc, "geometria", "radius");
    cfg.cyl_hmin   = getDouble(doc, "geometria", "hmin");
    cfg.cyl_hmax   = getDouble(doc, "geometria", "hmax");

    cfg.sph_cx         = getDouble(doc, "geometria", "cx");
    cfg.sph_cy         = getDouble(doc, "geometria", "cy");
    cfg.sph_cz         = getDouble(doc, "geometria", "cz");
    cfg.sph_radius     = getDouble(doc, "geometria", "radius");
    cfg.sph_autocenter = getBool  (doc, "geometria", "autocenter", false);

    return cfg;
}

// ============================================================
// Main
// ============================================================

int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::cerr << "Uso: v4s <ruta/al/run.toml>" << std::endl;
        return 1;
    }

    fs::path toml_path = argv[1];
    if (!fs::exists(toml_path)) {
        std::cerr << "Error: no existe " << toml_path << std::endl;
        return 1;
    }

    RunConfig cfg;
    try {
        cfg = loadRunConfig(toml_path);
    } catch (const std::exception& e) {
        std::cerr << "Error leyendo configuracion: " << e.what() << std::endl;
        return 1;
    }

    std::cout << "[V4S] run_id:      " << cfg.run_id       << std::endl;
    std::cout << "[V4S] sistema:     " << cfg.sistema_id   << std::endl;
    std::cout << "[V4S] geometria:   " << cfg.geometry     << std::endl;
    std::cout << "[V4S] output_mode: " << cfg.output_mode  << std::endl;
    std::cout << "[V4S] params:      ";
    for (auto& p : cfg.params) std::cout << p << " ";
    std::cout << std::endl;

    // --------------------------------------------------------
    // Logica de calculo — implementar con libreria propia
    // --------------------------------------------------------

    return 0;
}
