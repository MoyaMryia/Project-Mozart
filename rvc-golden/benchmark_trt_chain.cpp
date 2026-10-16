#include "rvc/trt_engine.hpp"
#include <nlohmann/json.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <future>
#include <iostream>

using Inputs = std::vector<rvc::OnnxInput>;
using Clock = std::chrono::steady_clock;

Inputs read_inputs(rvc::IEngine& engine, const std::filesystem::path& root,
                   const char* prefix, const std::vector<const char*>& names) {
    Inputs result;
    for (const auto* name : names) {
        const auto shape = engine.input_shape(name);
        size_t size = 1;
        for (auto dimension : shape) {
            if (dimension <= 0) throw std::runtime_error("Benchmark requires fixed shapes");
            size *= static_cast<size_t>(dimension);
        }
        std::vector<float> data(size);
        std::ifstream file(root / (std::string(prefix) + "-" + name + ".bin"), std::ios::binary);
        if (!file.read(reinterpret_cast<char*>(data.data()), static_cast<std::streamsize>(size*sizeof(float)))
            || file.peek() != std::char_traits<char>::eof()) throw std::runtime_error("Invalid captured input length");
        const auto type = engine.input_type(name).value_or(rvc::OnnxInput::Type::Float);
        std::vector<int64_t> integers;
        if (type == rvc::OnnxInput::Type::Int64) {
            integers.assign(data.begin(), data.end());
            data.clear();
        }
        result.push_back({name, shape, type, std::move(data), std::move(integers)});
    }
    return result;
}

nlohmann::json timings(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    const auto percentile = [&](double p) {
        const double index = (values.size()-1)*p;
        const size_t lower = static_cast<size_t>(index);
        return values[lower]+(values[std::min(lower+1, values.size()-1)]-values[lower])*(index-lower);
    };
    return {{"count", values.size()}, {"median_ms", percentile(.5)}, {"p95_ms", percentile(.95)}, {"max_ms", values.back()}};
}

int main(int argc, char** argv) {
    try {
        if (argc != 5) throw std::runtime_error("Usage: benchmark_trt_chain FRONT.engine DECODER.engine TENSORS OUTPUT.json");
        rvc::TrtEngine front, decoder;
        if (!front.load(argv[1]) || !decoder.load(argv[2])) throw std::runtime_error("TensorRT assets unavailable");
        auto front_inputs = read_inputs(front, argv[3], "front", {"feats", "p_len", "pitch", "sid", "latent_noise"});
        auto decoder_inputs = read_inputs(decoder, argv[3], "decoder", {"z", "pitchf", "sid", "source_phase", "source_noise"});
        const auto run_host = [&] {
            decoder_inputs[0].floats = front.run(front_inputs, {"z"});
            return decoder.run(decoder_inputs, {"audio"});
        };
        auto expected = run_host();
        const auto check = [&](const std::vector<float>& actual) {
            if (actual != expected || !std::all_of(actual.begin(), actual.end(), [](float value) { return std::isfinite(value); })) {
                throw std::runtime_error("Chained output differs from the host round-trip reference");
            }
        };
        std::vector<double> host_ms, chain_ms;
        for (int i = 0; i < 60; ++i) {
            const auto measure = [&](bool chained) {
                const auto start = Clock::now();
                const auto output = chained ? front.run_chained(front_inputs, "z", decoder, decoder_inputs, "z", "audio") : run_host();
                const double ms = std::chrono::duration<double, std::milli>(Clock::now()-start).count();
                check(output);
                if (i >= 10) (chained ? chain_ms : host_ms).push_back(ms);
            };
            measure(i % 2 == 0);
            measure(i % 2 != 0);
        }
        auto invalid = decoder_inputs;
        std::swap(invalid[0].shape[1], invalid[0].shape[2]);
        bool rejected = false;
        try { front.run_chained(front_inputs, "z", decoder, invalid, "z", "audio"); }
        catch (const std::invalid_argument&) { rejected = true; }
        if (!rejected) throw std::runtime_error("Invalid tensor layout was accepted");
        check(front.run_chained(front_inputs, "z", decoder, decoder_inputs, "z", "audio"));
        rejected = false;
        try { front.run(front_inputs, {"not-an-output"}); }
        catch (const std::invalid_argument&) { rejected = true; }
        if (!rejected) throw std::runtime_error("Invalid output name was accepted");
        const auto concurrent = [&] {
            auto local = decoder_inputs;
            for (int i = 0; i < 10; ++i) check(front.run_chained(front_inputs, "z", decoder, local, "z", "audio"));
        };
        auto first = std::async(std::launch::async, concurrent);
        auto second = std::async(std::launch::async, concurrent);
        first.get(); second.get();
        nlohmann::json report{{"host_roundtrip", timings(host_ms)}, {"device_chain", timings(chain_ms)},
            {"identical_outputs", 120}, {"concurrent_identical_outputs", 20}, {"invalid_layout_rejected", true},
            {"invalid_output_rejected", true}, {"samples", expected.size()},
            {"clock", "steady_clock; before run through completed output copy; 10 warmup pairs excluded"}};
        std::ofstream(argv[4]) << report.dump(2) << '\n';
        std::cout << report.dump(2) << '\n';
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
    return 0;
}
