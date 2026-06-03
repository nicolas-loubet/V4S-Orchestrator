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

static TomlDoc parseTOML(const fs::path& path) {
    std::ifstream file(path);
    if(!file.is_open()) throw std::runtime_error("No se pudo abrir: " + path.string());

    TomlDoc doc;
    std::string currentSection;
    std::string line;

    while(std::getline(file, line)) {
        line= trim(line);
        if(line.empty() || line[0] == '#') continue;

        if(line[0] == '[' && line.back() == ']') {
            currentSection= trim(line.substr(1, line.size()-2));
            continue;
        }

        size_t eq= line.find('=');
        if(eq == std::string::npos) continue;

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
        doc[currentSection][key]= tv;
    }
    return doc;
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


struct RunConfig {
    fs::path    run_dir;
    int         pid;

    std::string run_id;
    std::string sistema_id;
    fs::path    sistema_path;

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


    void init(fs::path dir) {
        run_dir= dir;
        pid= getInt(parseTOML(dir / "status.toml"), "status", "pid");


        TomlDoc  doc= parseTOML(run_dir / "run.toml");

        run_id         = getString(doc, "meta", "run_id");
        sistema_id     = getString(doc, "meta", "sistema_id");
        sistema_path   = getString(doc, "meta", "sistema_path");

        params         = getArray (doc, "parametros", "params");
        units          = getString(doc, "parametros", "units",          "kJ/mol");
        output_mode    = getString(doc, "parametros", "output_mode",    "mean");
        save_mol_count = getBool  (doc, "parametros", "save_mol_count", false);

        scope          = getString(doc, "agregacion", "scope",          "all");
        atom_selection = getString(doc, "agregacion", "atom_selection", "");

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
