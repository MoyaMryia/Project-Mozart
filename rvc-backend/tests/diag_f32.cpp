#include "rvc/feature_extractor.hpp"
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 6) {
        std::cerr << "usage: diag_f32 <input.f32> <hubert.onnx> <rmvpe.onnx> <f0_out.f32> [mel_out.f32]\n";
        return 1;
    }

    std::ifstream input(argv[1], std::ios::binary | std::ios::ate);
    if (!input) { std::cerr << "cannot open input\n"; return 1; }
    const auto bytes = static_cast<size_t>(input.tellg());
    input.seekg(0);
    std::vector<float> audio(bytes / sizeof(float));
    input.read(reinterpret_cast<char*>(audio.data()),
               static_cast<std::streamsize>(audio.size() * sizeof(float)));
    std::cout << "audio samples: " << audio.size() << "\n";

    rvc::FeatureExtractor fe(argv[2], argv[3], "cpu", false, true, false);
    if (!fe.is_rmvpe_loaded()) { std::cerr << "rmvpe not loaded\n"; return 1; }

    const auto f0 = fe.extract_f0(audio, 16000, "rmvpe");
    std::cout << "f0 frames: " << f0.size() << "\n";

    std::ofstream output(argv[4], std::ios::binary);
    output.write(reinterpret_cast<const char*>(f0.data()),
                 static_cast<std::streamsize>(f0.size() * sizeof(float)));

    if (argc >= 6) {
        const auto mel = fe.extract_mel(audio, 16000);
        std::ofstream mel_output(argv[5], std::ios::binary);
        mel_output.write(reinterpret_cast<const char*>(mel.data()),
                         static_cast<std::streamsize>(mel.size() * sizeof(float)));
        std::cout << "mel values: " << mel.size() << "\n";
    }
    return 0;
}
