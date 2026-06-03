#include <iostream>
#include <filesystem>
#include <string>
#include <vector>
#include <exception>

namespace fs= std::filesystem;

// Inclusiones de tus módulos
#include "config_manager.cpp"
#include "geometry_filter.cpp"
#include "writer.cpp"
#include "calculator.cpp"


void writeStatus(const fs::path& dir, const std::string& state, double progress, int eta_sec, int pid,
                 const std::string& message, const std::vector<std::string>& results) {
    
    std::ofstream f(dir / "status.toml");
    if(!f.is_open()) throw std::runtime_error("No se pudo abrir el archivo para escribir el status: " + dir.string() + "/status.toml");

    f << "[status]\n";
    f << "state   = \"" << state    << "\"\n";
    f << "progress= "   << progress  << "\n";
    f << "eta_sec = "   << eta_sec   << "\n";
    f << "pid     = "   << pid       << "\n";
    f << "message = \"" << message  << "\"\n";

    if(results.empty()) return;
    f << "\n[results]\n";
    f << "files= [";
    for(size_t i= 0; i < results.size(); i++) {
        f << "\"" << results[i] << "\"";
        if(i+1 < results.size()) f << ", ";
    }
    f << "]\n";
}

void runCalculation(RunConfig& cfg); // Declaración adelantada para delegar

int main(int argc, char* argv[]) {
    if(argc < 2) { std::cerr << "Uso: v4s <ruta/al/run-N/>" << std::endl; return 1; }

    fs::path run_dir= argv[1];
    RunConfig cfg;
    int pid= -1;

    try {
        cfg.init(run_dir);
        pid= cfg.pid;

        writeStatus(run_dir, "running", 0.0, 0, pid, "Iniciando");
        runCalculation(cfg);
    } catch(const std::exception& e) {
        std::cerr << "Error crítico: " << e.what() << std::endl;
        try {
            writeStatus(run_dir, "error", 0.0, 0, pid, e.what());
        } catch (...) {}
        return 1;
    }

    return 0;
}
