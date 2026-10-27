// test_pre_control.cpp — 预处理设备切换的设备校验与脚本调用
// ============================================================================
// 设备标识来自 HTTP 请求。这里验证两件事：非法字符在进入脚本前被拒绝，
// 合法设备按固定参数顺序传给 pre-restart。
#include "state/pre_control.hpp"

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>

#include <sys/stat.h>
#include <unistd.h>

#include <nlohmann/json.hpp>

namespace {

int failures = 0;

void check(bool condition, const char* what, int line)
{
    if (!condition) {
        std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, line, what);
        ++failures;
    }
}

#define CHECK(cond) check((cond), #cond, __LINE__)

std::string write_script(const std::string& body)
{
    const std::string path = "/tmp/test_pre_control_" + std::to_string(getpid()) + ".sh";
    std::ofstream out(path);
    out << "#!/bin/sh\n" << body;
    out.close();
    chmod(path.c_str(), 0755);
    return path;
}

void test_device_id_syntax()
{
    CHECK(rvc::PreControl::valid_device_id("plughw:CARD=MIC,DEV=0"));
    CHECK(rvc::PreControl::valid_device_id("plughw:CARD=Device,DEV=1"));
    CHECK(rvc::PreControl::valid_device_id("hw:0,0"));
    CHECK(rvc::PreControl::valid_device_id("default"));
    CHECK(rvc::PreControl::valid_device_id("plughw:CARD=APE,DEV=17"));

    CHECK(!rvc::PreControl::valid_device_id(""));
    CHECK(!rvc::PreControl::valid_device_id("plughw:CARD=MIC,DEV=0; rm -rf /"));
    CHECK(!rvc::PreControl::valid_device_id("plughw:CARD=MIC DEV=0"));
    CHECK(!rvc::PreControl::valid_device_id("$(id)"));
    CHECK(!rvc::PreControl::valid_device_id("plughw:CARD=MIC,DEV=0|cat"));
    CHECK(!rvc::PreControl::valid_device_id("`id`"));
    CHECK(!rvc::PreControl::valid_device_id(std::string(200, 'a')));
}

void test_restart_argv()
{
    const auto argv = rvc::PreControl::restart_argv("/opt/tools/run-realtime-voice.sh",
                                                    "plughw:CARD=MIC,DEV=0",
                                                    "plughw:CARD=Device,DEV=0");
    CHECK(argv.size() == 6);
    CHECK(argv[0] == "/opt/tools/run-realtime-voice.sh");
    CHECK(argv[1] == "pre-restart");
    CHECK(argv[2] == "--capture");
    CHECK(argv[3] == "plughw:CARD=MIC,DEV=0");
    CHECK(argv[4] == "--playback");
    CHECK(argv[5] == "plughw:CARD=Device,DEV=0");
}

void test_disabled_configuration()
{
    rvc::PreControl control({});
    CHECK(!control.enabled());

    const auto status = control.status();
    CHECK(status.value("available", true) == false);

    const auto result = control.set_devices("plughw:CARD=MIC,DEV=0", "plughw:CARD=Device,DEV=0");
    CHECK(result.value("status", "") == "failed");
}

void test_restart_with_fake_script()
{
    const std::string path = write_script(
        "if [ \"$1\" = \"devices\" ]; then\n"
        "  echo '{\"capture\":[{\"id\":\"plughw:CARD=MIC,DEV=0\"}],"
        "\"playback\":[{\"id\":\"plughw:CARD=Device,DEV=0\"}]}'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"pre-restart\" ]; then\n"
        "  echo '已停止 预处理 (pid 1234)'\n"
        "  echo '启动预处理 plughw:CARD=MIC,DEV=0 -> 18101 -> plughw:CARD=Device,DEV=0'\n"
        "  echo \"{\\\"status\\\":\\\"active\\\",\\\"argv\\\":\\\"$*\\\"}\"\n"
        "  exit 0\n"
        "fi\n"
        "echo '{\"error\":\"unexpected\"}'\n"
        "exit 1\n");

    rvc::PreControl::Config config;
    config.script = path;
    config.timeout_ms = 5000;
    rvc::PreControl control(config);

    const auto status = control.status();
    CHECK(status.value("available", false) == true);
    CHECK(status["devices"]["capture"].is_array());
    CHECK(status["devices"]["capture"].size() == 1);

    const auto missing = control.set_devices("plughw:CARD=NOPE,DEV=0", "plughw:CARD=Device,DEV=0");
    CHECK(missing.value("status", "") == "invalid");

    const auto missing_playback = control.set_devices("plughw:CARD=MIC,DEV=0", "plughw:CARD=NOPE,DEV=0");
    CHECK(missing_playback.value("status", "") == "invalid");

    const auto injection = control.set_devices("plughw:CARD=MIC,DEV=0; rm -rf /", "plughw:CARD=Device,DEV=0");
    CHECK(injection.value("status", "") == "invalid");

    const auto applied = control.set_devices("plughw:CARD=MIC,DEV=0", "plughw:CARD=Device,DEV=0");
    CHECK(applied.value("status", "") == "active");
    const std::string argv = applied.value("argv", "");
    CHECK(argv.find("pre-restart --capture plughw:CARD=MIC,DEV=0 --playback plughw:CARD=Device,DEV=0") != std::string::npos);

    std::remove(path.c_str());
}

void test_failing_script_reports_error()
{
    const std::string path = write_script(
        "if [ \"$1\" = \"devices\" ]; then\n"
        "  echo '{\"capture\":[{\"id\":\"plughw:CARD=MIC,DEV=0\"}],"
        "\"playback\":[{\"id\":\"plughw:CARD=Device,DEV=0\"}]}'\n"
        "  exit 0\n"
        "fi\n"
        "echo '错误: 采集设备不存在'\n"
        "exit 1\n");

    rvc::PreControl::Config config;
    config.script = path;
    config.timeout_ms = 5000;
    rvc::PreControl control(config);

    const auto result = control.set_devices("plughw:CARD=MIC,DEV=0", "plughw:CARD=Device,DEV=0");
    CHECK(result.value("status", "") == "failed");
    CHECK(result.value("error", "").find("采集设备不存在") != std::string::npos);

    std::remove(path.c_str());
}

} // namespace

int main()
{
    test_device_id_syntax();
    test_restart_argv();
    test_disabled_configuration();
    test_restart_with_fake_script();
    test_failing_script_reports_error();

    if (failures) {
        std::fprintf(stderr, "test_pre_control: %d failure(s)\n", failures);
        return 1;
    }
    std::printf("test_pre_control ok\n");
    return 0;
}
