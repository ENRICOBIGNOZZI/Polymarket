#include "pm/v7_exact_arb_ws_replay.hpp"
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>

int main(int argc, char** argv) {
    try {
        if (argc < 4) throw std::runtime_error("usage: exact_arb_ws_replay MODEL_SHA SESSION_MANIFEST SEALED_FRAME_SEGMENT...");
        std::ifstream manifest(argv[2], std::ios::binary);
        if (!manifest) throw std::runtime_error("session_manifest_open");
        manifest.seekg(0,std::ios::end);
        if (manifest.tellg() < 0 || manifest.tellg() > 1024*1024) throw std::runtime_error("session_manifest_size");
        manifest.seekg(0);
        std::ostringstream bytes; bytes << manifest.rdbuf();
        pm::v7::exact_arb_graph::NativeWsReplay replay(bytes.str(),argv[1]);
        for (int i = 3; i < argc; ++i) {
            std::ifstream frames(argv[i],std::ios::binary);
            if (!frames) throw std::runtime_error("frame_segment_open");
            // Bounded line assembly: a corrupt unterminated segment cannot
            // allocate arbitrary memory before the parser's size guard runs.
            std::string row; row.reserve(65536);
            for (char c; frames.get(c);) {
                if (c == '\n') { std::cout << replay.ingest(row) << '\n'; row.clear(); }
                else { if (row.size() >= 8*1024*1024) throw std::runtime_error("frame_line_size"); row += c; }
            }
            if (!frames.eof() || !row.empty()) throw std::runtime_error("unsealed_or_unreadable_segment");
        }
        std::cout << replay.receipt() << '\n';
        if (!std::cout) throw std::runtime_error("replay_output_failure");
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 2; }
}
