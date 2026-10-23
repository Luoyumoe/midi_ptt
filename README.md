# MIDI Pedal PTT Controller

MIDI 脚踏开关控制电台 PTT。支持 Linux、Windows、PySide6 GUI、系统托盘、headless 常驻，以及 Serial RTS/DTR、rigctld、flrig XML-RPC 驱动。

## 安装与启动

```bash
python3 -m venv .venv
. .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.txt
python -m midi_ptt_controller.main
```

可用 `python -m pip install .` 安装并获得 `midi-ptt-controller` 命令。

仓库中的 `run_app.py` 是 PyInstaller 使用的绝对导入启动器，可作为本地打包入口：

```bash
python run_app.py --help
```

首次 GUI 启动会打开设置向导。选 MIDI 输入设备、Note、通道和 PTT 驱动后保存。按住对应 Note 发射，松开停止。Note On 速度为零也视为松开。设置向导可从主窗口或托盘菜单再次打开。

```bash
python -m midi_ptt_controller.main --minimized
python -m midi_ptt_controller.main --headless
python -m midi_ptt_controller.main --simulate
```

`--headless` 在终端运行，直到收到 SIGINT/SIGTERM。首次无配置时创建安全默认配置，不发射；之后可用 GUI 完成配置。`--minimized` 在托盘可用时启动隐藏；首次未完成设置时先显示向导。仿真模式只用内存设备，不连接或操作真实电台，命令为 `press`、`release`、`disconnect`、`reconnect`、`status`、`quit`。

关闭主窗口会隐藏到系统托盘；从托盘选择“安全退出”会停止 MIDI、请求释放 PTT 并退出。没有托盘时，关闭窗口会释放 PTT 后退出。

## 驱动与安全行为

- Serial：选择串口和 RTS/DTR，按接线配置发射时线路电平。打开串口先置空闲电平。
- rigctld：配置 TCP 主机和端口，驱动发送 `T 1` / `T 0` 并验证 `RPRT 0`。
- flrig：配置 XML-RPC URL，驱动调用 `rig.set_ptt(1)` / `rig.set_ptt(0)`。
- PTT 命令超时范围 0.1–30 秒。驱动错误时禁止新发射并尝试释放；恢复后需松开踏板再按。
- MIDI 设备断开时立即请求释放。重新连接后，先收到踏板松开事件才重新允许发射，避免丢失 Note Off 后误保持发射。
- TOT 默认 60 秒，配置范围 1–3600 秒。超时强制释放，松开踏板后复位。
- GUI 的 PTT 测试会在二次确认后触发 500 毫秒脉冲；踏板按下时不允许测试。
- 配置写入用户配置目录并采用原子替换。Linux 使用 `$XDG_CONFIG_HOME/midi_ptt_controller/config.json` 或 `~/.config/midi_ptt_controller/config.json`；Windows 使用 `%APPDATA%\\midi_ptt_controller\\config.json`。日志写入同目录 `logs/controller.log`，按大小轮转，并输出到控制台/GUI。

本程序只能确认控制接口命令是否被接受，不能证明电台实际 RF 状态。首次连接请使用安全负载或确保频率、天线和射频环境适当。

## 验证

```bash
python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

测试以假串口、本地 TCP/XML-RPC 服务和仿真 MIDI 验证状态机，不要求真实电台或射频硬件。

## Windows 可执行文件

仓库包含 `.github/workflows/build-windows-exe.yml`。GitHub Actions 在 `master` 推送、`v*` 标签、Pull Request 和手动触发时执行：

1. 在 `windows-latest` 上安装 Python 3.12、项目依赖和 PyInstaller。
2. 运行标准库单元测试。
3. 使用 PyInstaller 生成单文件、无控制台窗口的 `MIDI-PTT-Controller.exe`。
4. 上传构建产物 `MIDI-PTT-Controller-windows` Artifact。
5. 构建 `v*` 标签时，将同一 `exe` 附加到 GitHub Release。

在 Actions 页面打开 “Build Windows executable”，下载 Artifact，或在 Release 页面下载 `MIDI-PTT-Controller.exe`。首次运行会打开设置向导，完成 MIDI 和电台驱动配置后即可使用。
