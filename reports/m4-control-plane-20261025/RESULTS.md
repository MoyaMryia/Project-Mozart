# M4：控制面迁移第一步（二进制协议服务并存）

日期：2026-10-25。对应总计划 v1 的 M4 里程碑（三步走的第 1 步）。

## 交付物

| 模块 | 内容 |
| --- | --- |
| `IO/include/mozart/ctlmsg.h` | 二进制控制消息契约：8 B 定长头（version/msg_type/flags/req_id）+ 按类型定宽命令体；PING/PONG/STATUS/MODE_SET/MODE_EVT/SUBTITLE_EVT/ERR |
| `tools/gen_ctlmsg.py` + `tools/ctlmsg_generated.py` | C 头为唯一定义源，Python 常量生成器；CI 由单测比对生成物防三端漂移 |
| `api/src/control_api.cpp`（+头） | Unix 数据报控制服务：同一 ControlPlane 表面的协议化子集；未知消息回 ERR 不断链；版本拒绝 |
| `state/src/daemon.cpp` 接线 | `network.control.socket` 配置非空时与 HTTP 并存启动；停止顺序先控制后 HTTP |
| `api/tests/test_control.py` + `control_fixture.cpp` | 6 项协议测试：编解码往返、STATUS 映射（mode/state/计数器）、MODE_SET 生效、未知消息错误应答、版本拒绝、生成物一致性 |
| `tools/protodump.py` | 控制数据报可读渲染（调试工具） |

## 验收状态

- 原生全量 29/29 + 状态 14/14 + 新增 test_control 6/6（三次重复运行稳定）。
- 协议三定律落实：未知类型回错不断链（单测钉死）、版本前置（单测钉死）、
  req_id 关联（单测钉死）。

## 后续步骤（M4 计划内，按三步走推进）

本报告交付第 1 步（并存）。后续：
1. 控制台 WS 化 + 主机桥客户端 WS 网关（第 2 步）。
2. FILE 子协议与文件端点（与 M3 端点配合）。
3. 内部 Python 服务 Unix 化 + llama-server 白名单（第 3 步 b 档）。
4. HTTP 编译出局与 serve-console 退役（第 3 步收编）。

阻塞：第 2-3 步依赖板端部署验证（同 M2/M3 的 sudo 阻塞项）。

## 适用范围

- 配置：本机 x86_64 构建；Unix 数据报；桩 ControlPlane 夹具。
- 样本：6 项协议测试 ×3 次重复；原生回归 29/29。
- 未覆盖：真实 ModeController 下的 STATUS 字段完整性（桩只含 4 字段）。阻塞：夹具无完整控制器。解除条件：板端接真实控制器后核对字段。
