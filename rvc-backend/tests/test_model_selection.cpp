#include "rvc/pipeline.hpp"
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <cstdlib>
#include <future>

namespace rvc {
std::promise<void>* loading_started = nullptr;
std::shared_future<void> loading_release;
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
    inline static int loads = 0;
    TestEngine() { ++loads; }
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
    if (path.stem() == "slow" && loading_started) {
        loading_started->set_value();
        loading_release.wait();
    }
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
        for (const auto* id : {"c_later", "a_bad", "b_good", "d_new", "slow"}) {
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
        const int loads = rvc::TestEngine::loads;
        require(manager.load_model("b_good") && rvc::TestEngine::loads == loads, "current model reloaded");
        auto pinned = manager.current_model();
        const std::weak_ptr<rvc::RVCModel> old = pinned;
        require(manager.load_model("c_later"), "second model rejected");
        auto second_pinned = manager.current_model();
        require(manager.load_model("d_new"), "third model rejected");
        require(!old.expired() && pinned->loaded(), "cache evicted an in-use model");
        pinned.reset();
        second_pinned.reset();
        manager.trim_cache();
        require(manager.cached_model_count() <= 2, "idle model cache grew past its limit");
        require(old.expired(), "least recently used model was not released");
        auto warm = manager.current_model();
        require(manager.load_model("c_later"), "cached model switch failed");
        const int warm_loads = rvc::TestEngine::loads;
        require(manager.load_model("d_new") && manager.current_model() == warm, "cached model identity changed");
        require(rvc::TestEngine::loads == warm_loads, "cached model weights reloaded");
        require(!manager.load_model("a_bad") && manager.current_model() == warm, "failed switch changed active model");
        warm.reset();
        manager.trim_cache();
        require(manager.cached_model_count() <= manager.cache_limit(), "failed model escaped cache limit");
        rvc::ModelManager single(root, "cpu", false, 1);
        for (int i = 0; i < 12; ++i) {
            require(single.load_model(i % 2 ? "b_good" : "c_later"), "repeated switch failed");
            require(single.cached_model_count() == 1, "single-model cache retained idle weights");
        }
        rvc::FeatureExtractor deferred(root/"b_good/b_good.onnx", root/"c_later/c_later.onnx",
            "cpu", false, false, false, std::nullopt, std::nullopt, true);
        require(!deferred.is_hubert_loaded() && !deferred.is_rmvpe_loaded(), "deferred quality engines loaded eagerly");
        require(deferred.runtime_info().at("hubert_backend") == "deferred", "deferred runtime is not observable");
        (void)deferred.extract_features({0.0f, 0.1f});
        require(deferred.is_hubert_loaded() && deferred.is_rmvpe_loaded(), "quality engines did not load on first use");
        const int quality_loads = rvc::TestEngine::loads;
        (void)deferred.extract_features({0.0f, 0.1f});
        require(rvc::TestEngine::loads == quality_loads, "quality engines loaded more than once");
        std::promise<void> started, release;
        rvc::loading_started = &started;
        rvc::loading_release = release.get_future().share();
        const auto active_before = manager.current_model()->id();
        auto loading = std::async(std::launch::async, [&] { return manager.load_model("slow"); });
        started.get_future().wait();
        auto snapshot = std::async(std::launch::async, [&] {
            manager.list_models();
            return manager.current_model()->id();
        });
        const bool responsive = snapshot.wait_for(std::chrono::seconds(1)) == std::future_status::ready;
        release.set_value();
        require(snapshot.get() == active_before, "loading published an incomplete model");
        require(loading.get(), "slow model fixture failed");
        rvc::loading_started = nullptr;
        require(responsive, "model initialization blocked status reads");
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
