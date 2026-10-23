# AGENTS.md

## 语言

- 始终使用中文回复。
- 技术说明保持简洁，明确说明修改、验证结果和未验证事项。

## 项目结构

- `config.py`、`backends.py`、`engine.py` 为核心无界面模块，禁止导入 `PySide6`。
- `gui.py` 负责 PySide6 图形界面和系统托盘。
- `main.py` 负责命令行参数、信号处理和程序入口。
- `tests/` 使用 Python 标准库 `unittest`。

## 安全行为

- 默认状态必须禁止 PTT 发射。
- 设备断开、驱动故障、TOT 超时和程序退出时必须释放 PTT。
- 配置损坏或版本不兼容时不得自动发射。

## 验证

提交前运行：

```bash
python -m unittest discover -s tests -v
python -m compileall -q midi_ptt_controller tests
git diff --check
```

涉及 GUI 时，使用 `QT_QPA_PLATFORM=offscreen` 执行测试。真实 MIDI 设备和电台硬件验证属于可选人工验证，不得声称已完成。

## 提交

- 提交信息使用简短、明确的 Conventional Commits 风格标题。
- 只提交与当前任务相关的文件，不提交虚拟环境、构建缓存或本地配置。
- 未经明确要求不执行 push、发布或部署。
