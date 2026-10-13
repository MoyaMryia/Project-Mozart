#include "rvc/pipeline.hpp"
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <cstdlib>

namespace rvc {
// 禁止模型选择测试意外进入真实 ONNX 路径。
bool OnnxEngine::load(const std::filesystem::path&) { throw std::runtime_error("unexpected ONNX load"); }
std::optional<OnnxInput::Type> OnnxEngine::input_type(const std::string&) const { return std::nullopt; }
std::vector<int64_t> OnnxEngine::input_shape(const std::string&) const { return {}; }
std::vector<float> OnnxEngine::run(const std::vector<const char*>&, const std::vector<std::vector<int64_t>>&,
                                  const std::vector<std::vector<float>>&, const std::vector<const char*>&) {
    throw std::runtime_error("unexpected ONNX inference");
}
std::vector<float> OnnxEngine::run(const std::vector<OnnxInput>&, const std::vector<const char*>&) {
    throw std::runtime_error("unexpected ONNX inference");
}
void OnnxEngine::unload() {}

// 测试引擎仅验证模型选择，不产生 RVC 推理结果。
class TestEngine final : public IEngine {
public:
    bool load(const std::filesystem::path&) override { return true; }
    bool loaded() const override { return true; }
    std::optional<OnnxInput::Type> input_type(const std::string&) const override { return std::nullopt; }
    std::vector<float> run(const std::vector<const char*>&, const std::vector<std::vector<int64_t>>&,
                           const std::vector<std::vector<float>>&, const std::vector<const char*>&) override { return {}; }
    std::vector<float> run(const std::vector<OnnxInput>&, const std::vector<const char*>&) override { return {}; }
    void unload() override {}
};
std::unique_ptr<IEngine> make_engine(const std::filesystem::path& path) {
    if (path.stem() == "a_bad") return nullptr;
    return std::make_unique<TestEngine>();
}
}

int main() {
    char pattern[] = "/tmp/mozart-model-selection-XXXXXX";
    const char* directory = mkdtemp(pattern);
    if (!directory) return 1;
    const std::filesystem::path root(directory);
    const auto require = [](bool value, const char* message) { if (!value) throw std::runtime_error(message); };
    int result = 0;
    try {
        for (const auto* id : {"c_later", "a_bad", "b_good"}) {
            std::filesystem::create_directory(root / id);
            std::ofstream(root / id / "config.json") << "{}";
            std::ofstream(root / id / (std::string(id) + ".onnx")) << "test engine fixture";
        }
        rvc::ModelManager manager(root, "cpu", false);
        const auto before = manager.list_models();
        require(before[0].at("id") == "a_bad" && before[1].at("id") == "b_good", "model order is not stable");
        require(!manager.load_model("a_bad"), "bad model accepted");
        require(manager.load_model("b_good"), "good fixture rejected");
        require(manager.list_models()[1].at("loaded") == "true", "loaded model is reported as unloaded");
        require(!manager.load_model("a_bad") && manager.current_model()->id() == "b_good", "failed switch lost current model");
        auto pipeline = rvc::RVCPipelineFactory::create({false, true, true}, root, root / "missing", std::nullopt,
                                                       16000, 48000, "cpu", false);
        require(pipeline->current_model_id() == "b_good", "initial model search stopped at a failed model");
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        result = 1;
    }
    std::filesystem::remove_all(root);
    return result;
}
