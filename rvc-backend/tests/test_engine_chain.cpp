#include "rvc/onnx_engine.hpp"
#include <iostream>

class Engine final : public rvc::IEngine {
public:
    int calls = 0;
    bool load(const std::filesystem::path& path) override { record_runtime("test", path); return true; }
    bool loaded() const override { return true; }
    void unload() override {}
    std::optional<rvc::OnnxInput::Type> input_type(const std::string&) const override { return std::nullopt; }
    std::vector<float> run(const std::vector<const char*>&, const std::vector<std::vector<int64_t>>&,
                          const std::vector<std::vector<float>>&, const std::vector<const char*>&) override { return {}; }
    std::vector<float> run(const std::vector<rvc::OnnxInput>& inputs, const std::vector<const char*>& outputs) override {
        ++calls;
        if (std::string(outputs.at(0)) == "z") return {1, 2, 3};
        return inputs.at(0).floats;
    }
};

int main() {
    try {
        Engine front, decoder;
        std::vector<rvc::OnnxInput> inputs{{"z", {1, 3}, rvc::OnnxInput::Type::Float, {}, {}}};
        if (front.run_chained({}, "z", decoder, inputs, "z", "audio") != std::vector<float>{1, 2, 3}) return 1;
        for (const auto& shape : std::vector<std::vector<int64_t>>{{1, 2}, {1, -1}, {INT64_MAX, 8}}) {
            inputs[0].shape = shape;
            try {
                front.run_chained({}, "z", decoder, inputs, "z", "audio");
                return 1;
            } catch (const std::invalid_argument&) {}
        }
        if (decoder.calls != 1) return 1;
        inputs[0].shape = {1, 3};
        try {
            front.run_chained({}, "z", decoder, inputs, "missing", "audio");
            return 1;
        } catch (const std::invalid_argument& error) {
            if (std::string(error.what()) != "Chained input tensor not found") return 1;
        }
        if (decoder.calls != 1) return 1;
        front.load("selected.engine");
        front.add_fallback_reason("First failure");
        auto snapshot = front.runtime_info();
        front.add_fallback_reason("Second failure");
        if (snapshot.at("fallback_reason") != "First failure" || snapshot.at("asset") != "selected.engine") return 1;
        if (front.runtime_info().at("fallback_reason") != "First failure; Second failure") return 1;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
    return 0;
}
