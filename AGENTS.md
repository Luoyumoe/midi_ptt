# AGENTS.md

本文件是 `midi_ptt_controller` 项目的协作与维护说明。修改代码、运行验证或准备提交时，遵循以下约定。

## 语言与沟通

- 始终使用中文回复。
- 说明结果时先给结论，再给关键证据、文件路径和命令。
- 明确区分“已在本机验证”“仅完成静态检查”和“需要真实硬件人工验证”。
- 不把本地提交、推送、发布或部署混为一谈；只有实际执行成功后才能声称完成。

## 项目目标

本项目是 Python 3.10+ 的 MIDI 脚踏开关电台 PTT 控制器，提供：

- PySide6 图形配置和调试界面；
- 系统托盘常驻，关闭窗口默认隐藏到托盘；
- `--minimized` 最小化启动；
- `--headless` 无图形环境常驻运行；
- `--simulate` 无 MIDI、电台和串口硬件的终端仿真；
- Serial RTS/DTR、rigctld TCP 和 flrig XML-RPC 三种 PTT 驱动。

项目只能确认控制接口是否接受命令，不能证明电台真实 RF 发射状态。真实 MIDI、串口、电台和射频环境属于可选人工验证，不得在没有硬件证据时声称已验证。

## 目录与职责

```text
midi_ptt_controller/
├── config.py       # 配置 dataclass、版本化 JSON、路径和原子写入
├── backends.py     # PttBackend 抽象接口及 Serial/rigctld/flrig 实现
├── engine.py       # MIDI、热插拔、单槽状态收敛、TOT 和安全释放
├── gui.py          # PySide6 窗口、向导、托盘、日志和调试控件
├── main.py         # CLI 参数、信号、日志初始化和程序生命周期
└── __main__.py     # python -m midi_ptt_controller 入口
tests/              # 标准库 unittest 测试
```

`config.py`、`backends.py`、`engine.py` 是核心无界面层，禁止导入任何 `PySide6` 模块。这样 `--headless` 和 `--simulate` 能在没有图形环境时启动。只有需要 GUI 时，`main.py` 才应延迟导入 `gui.py`。

保持依赖方向单向：配置和后端不依赖 GUI；引擎依赖配置、后端和 MIDI 适配；GUI 通过引擎公开接口工作；入口负责选择 GUI、headless 或仿真模式。不要在核心层加入 Qt 信号、QObject 或 Qt 定时器。

## 配置行为

- 首次启动使用安全默认值：未完成配置、禁止发射、没有已连接电台。
- Linux 配置路径为 `$XDG_CONFIG_HOME/midi_ptt_controller/config.json`，未设置时使用 `~/.config/midi_ptt_controller/config.json`。
- Windows 配置路径为 `%APPDATA%\\midi_ptt_controller\\config.json`。
- 配置文件采用版本字段和原子替换写入；Linux 文件权限应限制为用户可读写（通常为 `0600`）。
- 未知版本、JSON 损坏或字段无效时，保留禁止发射的安全状态并记录可见错误，不自动猜测配置。
- 日志写入配置目录下的 `logs/controller.log`，按大小轮转，同时输出到控制台和 GUI 日志面板。
- 修改配置结构时同步更新 dataclass、版本迁移/拒绝逻辑、GUI 向导和配置测试。

## MIDI 与状态机

- 只处理配置的单个 Note 和 MIDI 通道。
- Note On 且 velocity 大于零表示按下。
- Note Off，或 velocity 为零的 Note On，表示松开。
- MIDI 控件模式支持 `note` 和 `cc`。CC 模式使用单个控制器编号；CC 值大于零按下，值为零松开。
- 设置向导的“按下检测按键”必须监听下一条 Note/CC 事件，并自动选择设备、通道、模式和编号；检测等待不能阻塞 Qt UI。
- MIDI 设备断开必须立即请求释放 PTT；按设备名周期性重连。
- 重连、后端故障或 TOT 触发后，必须先收到松开事件，再允许下一次按下发射，避免丢失 Note Off 导致持续发射。
- 引擎使用单一 I/O 线程和单槽目标状态串行收敛 PTT。不要把每个按下/松开事件无限排队；过期目标应被新目标覆盖。
- PTT 命令、连接、断开和退出路径必须可重复调用，异常不能让线程或程序卡死。

## PTT 后端

所有驱动实现 `PttBackend` 的连接、`set_ptt` 和关闭接口，并为连接与命令设置超时。

- Serial：支持 RTS、DTR 及反相；打开串口时先设置空闲电平，防止连接瞬间误发射。
- rigctld：连接配置的 TCP 主机和端口，发送 `T 1` / `T 0`，并检查 `RPRT 0` 响应。
- flrig：连接配置的 XML-RPC URL，调用 `rig.set_ptt(1)` / `rig.set_ptt(0)`。
- 任何驱动失败都禁止新的按下发射，并尽力发送释放命令；恢复后必须松开踏板再按。
- 切换驱动前必须先让旧驱动释放 PTT；释放失败时中止切换，不得悄悄丢弃故障。
- 退出、SIGINT、SIGTERM 和异常清理路径都要先请求释放 PTT，再关闭 MIDI 和驱动。

## TOT 与调试脉冲

- TOT 默认启用且为 60 秒，可配置范围为 1–3600 秒。
- TOT 到期时强制释放 PTT，并锁定到踏板松开；松开后才复位。
- GUI 的 PTT 调试按钮必须二次确认，默认产生 500 毫秒脉冲。
- 踏板已经按下时拒绝调试脉冲，防止测试操作与真实状态竞争。

## GUI 与 CLI

GUI 应显示 MIDI 连接、PTT、故障状态灯、TOT 进度、实时日志和设置向导。首次配置未完成时，即使传入 `--minimized` 也应先显示向导。关闭窗口默认隐藏到托盘；托盘菜单提供显示窗口、打开设置和安全退出。没有系统托盘时，关闭窗口应释放 PTT 后退出。

启动示例：

```bash
python -m midi_ptt_controller.main
python -m midi_ptt_controller.main --minimized
python -m midi_ptt_controller.main --headless
python -m midi_ptt_controller.main --simulate
```

`--headless` 与 `--minimized` 互斥。headless 在没有完整配置时只记录状态并等待，不应连接或发射。仿真模式只使用内存设备，支持 `press`、`release`、`disconnect`、`reconnect`、`status`、`quit`，不得触碰真实 MIDI 或电台。

## 测试与验证

提交前至少运行：

```bash
python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
python -m compileall -q midi_ptt_controller tests
git diff --check
```

测试应覆盖配置读写和安全默认值、Note 解析、热插拔释放/重连、单槽状态收敛、TOT 锁定、故障恢复、驱动切换和退出释放。后端测试使用假串口、本地 TCP 服务和本地 XML-RPC 服务；不要要求真实电台才能通过单元测试。

涉及 GUI 的改动必须至少执行 offscreen 测试，必要时检查窗口尺寸、控件可见性和托盘不可用时的退出路径。headless 改动必须确认导入模块不会加载 PySide6，例如：

```bash
python -m midi_ptt_controller.main --help
python -m midi_ptt_controller.main --headless
```

测试失败时报告实际失败命令和环境限制，不要用“应该通过”替代结果。测试、编译或构建产生的 `__pycache__/`、`*.pyc`、`build/`、`dist/`、`*.egg-info/` 不应提交。

## 修改与提交

- 修改前先查看 `git status`，保留其他工作，不覆盖用户已有改动。
- 使用 `apply_patch` 进行手工编辑；保持改动聚焦，不做无关重构。
- 提交信息使用简短明确的 Conventional Commits 风格，例如 `feat: ...`、`fix: ...`、`docs: ...`、`test: ...`。
- 提交前检查 `git diff --check`，只暂存当前任务相关文件。
- 未经明确要求不执行 `git reset --hard`、强制覆盖、push、发布或部署。
- 最终说明应包含修改文件、验证命令和结果、提交 hash，以及是否 push；没有真实硬件验证时明确说明。
