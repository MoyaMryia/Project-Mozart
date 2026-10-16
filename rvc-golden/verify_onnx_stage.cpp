// 用捕获的相同张量验证实际 C++ ONNX 运行时。
#include "rvc/onnx_engine.hpp"
#include <nlohmann/json.hpp>
#include <chrono>
#include <algorithm>
#include <cmath>
#include <fstream>
#include <iostream>

using Json = nlohmann::json;
template<class T> std::vector<T> read_values(const std::filesystem::path& path, size_t count) {
    std::vector<T> values(count);
    std::ifstream file(path, std::ios::binary);
    if (!file.read(reinterpret_cast<char*>(values.data()), count*sizeof(T))
        || file.peek() != std::char_traits<char>::eof())
        throw std::runtime_error("Captured tensor size differs");
    return values;
}

int main(int argc, char** argv) {
    try {
        if (argc != 3) throw std::runtime_error("Usage: verify_onnx_stage MANIFEST OUTPUT.json");
        const std::filesystem::path root = std::filesystem::path(argv[1]).parent_path();
        Json manifest;
        std::ifstream(argv[1]) >> manifest;
        Json report = Json::array();
        for (const auto& stage : manifest) {
            const auto required = stage.value("backend", std::string{});
            std::unique_ptr<rvc::IEngine> selected;
            if (required == "tensorrt") selected = rvc::make_engine(stage.at("model").get<std::string>());
            else {
                auto onnx = std::make_unique<rvc::OnnxEngine>();
                if (!onnx->load(stage.at("model").get<std::string>())) throw std::runtime_error("ONNX stage load failed");
                selected = std::move(onnx);
            }
            if (!selected) throw std::runtime_error("Stage engine is unavailable");
            auto& engine = *selected;
            for (const auto& item : stage.at("cases")) {
                std::vector<rvc::OnnxInput> inputs;
                std::vector<std::string> names;
                for (const auto& input : item.at("inputs")) names.push_back(input.at("name"));
                size_t index = 0;
                for (const auto& input : item.at("inputs")) {
                    auto shape = input.at("shape").get<std::vector<int64_t>>();
                    size_t count = 1;
                    for (const auto dimension : shape) count *= dimension;
                    rvc::OnnxInput value;
                    value.name = names[index++].c_str();
                    value.shape = shape;
                    value.type = input.value("dtype", std::string{}) == "int64"
                        ? rvc::OnnxInput::Type::Int64 : rvc::OnnxInput::Type::Float;
                    const auto path = root/input.at("path").get<std::string>();
                    if (value.type == rvc::OnnxInput::Type::Int64) value.int64s = read_values<int64_t>(path, count);
                    else value.floats = read_values<float>(path, count);
                    inputs.push_back(std::move(value));
                }
                const auto expected = read_values<float>(root/item.at("expected").get<std::string>(), item.at("elements"));
                const std::string output_name = stage.at("output");
                std::vector<double> times;
                double relative = 0, cosine = 0, maximum = 0;
                for (int repeat = 0; repeat < 6; ++repeat) {
                    const auto began = std::chrono::steady_clock::now();
                    const auto actual = engine.run(inputs, {output_name.c_str()});
                    if (!required.empty() && engine.runtime_info().at("backend") != required)
                        throw std::runtime_error("Actual backend differs from required backend");
                    const double elapsed = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now()-began).count();
                    if (repeat) times.push_back(elapsed);
                    if (actual.size() != expected.size()) throw std::runtime_error("Output element count differs");
                    double error = 0, reference = 0, norm = 0, dot = 0;
                    for (size_t i = 0; i < actual.size(); ++i) {
                        if (!std::isfinite(actual[i]) || !std::isfinite(expected[i]))
                            throw std::runtime_error("Output or reference is not finite");
                        const double delta = double(actual[i])-expected[i];
                        error += delta*delta; reference += double(expected[i])*expected[i];
                        norm += double(actual[i])*actual[i]; dot += double(actual[i])*expected[i];
                        maximum = std::max(maximum, std::abs(delta));
                    }
                    relative = std::sqrt(error/std::max(reference, 1e-30));
                    cosine = dot/std::max(std::sqrt(reference*norm), 1e-30);
                    if (relative > .001 || cosine < .99999) throw std::runtime_error("Captured PyTorch comparison failed");
                }
                report.push_back({{"stage",stage.at("name")},{"case",item.at("name")},
                    {"runtime",engine.runtime_info()},{"relative_rmse",relative},{"cosine",cosine},
                    {"max_abs",maximum},{"elapsed_ms",times},{"elements",expected.size()}});
            }
        }
        std::ofstream(argv[2]) << report.dump(2) << '\n';
        std::cout << report.dump(2) << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
