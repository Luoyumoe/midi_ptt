"""PySide6 dashboard, first-run settings wizard and system tray."""

from __future__ import annotations

import logging
import atexit
import queue
import signal
import threading
from datetime import datetime
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import AppConfig, BackendConfig, ConfigStore, MidiConfig
from .engine import PttEngine, PttStatus

LOG = logging.getLogger(__name__)


def _midi_names() -> list[str]:
    try:
        import mido

        return list(mido.get_input_names())
    except Exception:
        return []


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("MIDI / PTT 设置向导")
        self.setMinimumWidth(460)
        self._original = config
        form = QFormLayout()

        self.device = QComboBox()
        self.device.setEditable(True)
        self.device.addItem("请选择 MIDI 输入设备", "")
        for name in _midi_names():
            self.device.addItem(name, name)
        if config.midi.device_name:
            index = self.device.findData(config.midi.device_name)
            if index < 0:
                self.device.addItem(config.midi.device_name, config.midi.device_name)
                index = self.device.count() - 1
            self.device.setCurrentIndex(index)
        form.addRow("MIDI 设备", self.device)

        self.note = QSpinBox()
        self.note.setRange(0, 127)
        self.note.setValue(config.midi.note if config.midi.note is not None else 60)
        form.addRow("Note 编号", self.note)
        self.channel = QComboBox()
        self.channel.addItem("任意通道", None)
        for channel in range(1, 17):
            self.channel.addItem(str(channel), channel)
        channel_index = self.channel.findData(config.midi.channel)
        self.channel.setCurrentIndex(max(0, channel_index))
        form.addRow("MIDI 通道", self.channel)

        self.backend_kind = QComboBox()
        for value, label in (("serial", "Serial RTS/DTR"), ("rigctld", "rigctld"), ("flrig", "flrig XML-RPC")):
            self.backend_kind.addItem(label, value)
        backend_index = self.backend_kind.findData(config.backend.kind)
        self.backend_kind.setCurrentIndex(max(0, backend_index))
        form.addRow("PTT 驱动", self.backend_kind)

        self.serial_port = QLineEdit(config.backend.serial_port)
        form.addRow("串口", self.serial_port)
        self.serial_line = QComboBox()
        self.serial_line.addItems(["RTS", "DTR"])
        self.serial_line.setCurrentText(config.backend.serial_line)
        form.addRow("PTT 线路", self.serial_line)
        self.serial_active_low = QCheckBox("发射时线路拉低")
        self.serial_active_low.setChecked(config.backend.serial_active_low)
        form.addRow("线路电平", self.serial_active_low)

        self.host = QLineEdit(config.backend.host)
        form.addRow("rigctld 主机", self.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(config.backend.port or 4532)
        form.addRow("rigctld 端口", self.port)
        self.url = QLineEdit(config.backend.url)
        self.url.setPlaceholderText("http(s)://host:port/RPC2")
        form.addRow("flrig XML-RPC URL", self.url)

        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.1, 30.0)
        self.timeout.setSingleStep(0.5)
        self.timeout.setSuffix(" 秒")
        self.timeout.setValue(config.backend.timeout_seconds)
        form.addRow("PTT 命令超时", self.timeout)

        self.tot = QSpinBox()
        self.tot.setRange(1, 3600)
        self.tot.setSuffix(" 秒")
        self.tot.setValue(config.tot_seconds)
        form.addRow("最长连续发射 (TOT)", self.tot)

        self._rows: list[tuple[str, QWidget]] = []
        for label, widget in (
            ("串口", self.serial_port),
            ("PTT 线路", self.serial_line),
            ("线路电平", self.serial_active_low),
            ("rigctld 主机", self.host),
            ("rigctld 端口", self.port),
            ("flrig XML-RPC URL", self.url),
        ):
            self._rows.append((label, widget))
        self._form = form
        self.backend_kind.currentIndexChanged.connect(self._update_backend_fields)
        self._update_backend_fields()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _update_backend_fields(self) -> None:
        kind = self.backend_kind.currentData()
        visible = {
            "serial": {"串口", "PTT 线路", "线路电平"},
            "rigctld": {"rigctld 主机", "rigctld 端口"},
            "flrig": {"flrig XML-RPC URL"},
        }.get(kind, set())
        for label, widget in self._rows:
            self._form.labelForField(widget).setVisible(label in visible)
            widget.setVisible(label in visible)

    def make_config(self) -> AppConfig:
        kind = self.backend_kind.currentData()
        backend = BackendConfig(
            kind=kind,
            serial_port=self.serial_port.text().strip(),
            serial_line=self.serial_line.currentText(),
            serial_active_low=self.serial_active_low.isChecked(),
            host=self.host.text().strip(),
            port=self.port.value() if kind == "rigctld" else None,
            url=self.url.text().strip(),
            timeout_seconds=self.timeout.value(),
        )
        selected_name = self.device.currentData()
        if selected_name:
            device_name = str(selected_name).strip()
        elif self.device.currentIndex() == 0 and self.device.currentText() == "请选择 MIDI 输入设备":
            device_name = ""
        else:
            device_name = self.device.currentText().strip()
        midi = MidiConfig(
            device_name=device_name,
            note=self.note.value(),
            channel=self.channel.currentData(),
        )
        config = AppConfig(
            schema_version=self._original.schema_version,
            setup_complete=True,
            midi=midi,
            backend=backend,
            tot_seconds=self.tot.value(),
        )
        config.validate()
        return config


class MainWindow(QMainWindow):
    def __init__(
        self,
        engine: PttEngine,
        store: ConfigStore,
        config: AppConfig,
        minimized: bool,
        startup_error: str = "",
    ):
        super().__init__()
        self.engine = engine
        self.store = store
        self.config = config
        self._minimized_requested = minimized
        self._really_quit = False
        self._settings_pending = False
        self._settings_results: queue.Queue[tuple[AppConfig, BaseException | None]] = queue.Queue(maxsize=1)
        self._settings_thread: threading.Thread | None = None
        self._shutdown_results: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)
        self._shutdown_pending = False
        self._tray: QSystemTrayIcon | None = None
        self.setWindowTitle("MIDI Pedal PTT Controller")
        self.resize(700, 480)
        self._build_ui()
        if startup_error:
            self._append_log(f"配置读取失败，PTT 保持禁用：{startup_error}")
        self._build_tray()
        self.engine.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(100)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_engine)
        if not config.setup_complete:
            QTimer.singleShot(0, self.open_settings)
        elif minimized and self._tray is not None:
            QTimer.singleShot(0, self.hide)

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        status_row = QHBoxLayout()
        self.midi_status = QLabel("MIDI: waiting")
        self.ptt_status = QLabel("PTT: released")
        self.fault_status = QLabel("状态正常")
        for label in (self.midi_status, self.ptt_status, self.fault_status):
            label.setMinimumWidth(150)
            label.setStyleSheet("padding: 8px; border: 1px solid #888; border-radius: 4px")
            status_row.addWidget(label)
        layout.addLayout(status_row)

        self.tot_label = QLabel("TOT 0 / 60 秒")
        self.tot_bar = QProgressBar()
        self.tot_bar.setRange(0, self.config.tot_seconds)
        self.tot_bar.setValue(0)
        layout.addWidget(self.tot_label)
        layout.addWidget(self.tot_bar)

        buttons = QHBoxLayout()
        self.settings_button = QPushButton("设置向导")
        self.settings_button.clicked.connect(self.open_settings)
        self.test_button = QPushButton("测试 PTT 500 ms")
        self.test_button.clicked.connect(self._test_ptt)
        buttons.addWidget(self.settings_button)
        buttons.addWidget(self.test_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("运行日志")
        layout.addWidget(self.log_view, 1)
        self.setCentralWidget(central)

        settings_action = QAction("设置向导", self)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("安全退出", self)
        quit_action.triggered.connect(self._quit)
        menu = self.menuBar().addMenu("控制")
        menu.addAction(settings_action)
        menu.addSeparator()
        menu.addAction(quit_action)

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._append_log("系统托盘不可用；关闭窗口将退出程序并释放 PTT")
            return
        icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("MIDI Pedal PTT Controller")
        menu = tray.contextMenu() or QMenu()
        show_action = QAction("显示控制面板", self)
        show_action.triggered.connect(self._show_window)
        settings_action = QAction("设置向导", self)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("安全退出", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(show_action)
        menu.addAction(settings_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        self._tray = tray

    def _append_log(self, message: str) -> None:
        self.log_view.append(message)

    def _refresh(self) -> None:
        self._finish_settings_update()
        status = self.engine.snapshot()
        self._display_status(status)
        while True:
            try:
                event = self.engine.events.get_nowait()
            except queue.Empty:
                break
            stamp = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
            self._append_log(f"{stamp} [{event.level.upper()}] {event.message}")

    def _display_status(self, status: PttStatus) -> None:
        self.midi_status.setText(f"MIDI: {'已连接' if status.midi_connected else '未连接'}")
        self.midi_status.setStyleSheet(
            "padding: 8px; border: 1px solid #888; border-radius: 4px; background: #c8f7c5"
            if status.midi_connected
            else "padding: 8px; border: 1px solid #888; border-radius: 4px; background: #eee"
        )
        ptt_text = "发射中" if status.ptt_applied is True else "状态未知" if status.ptt_applied is None else "已释放"
        self.ptt_status.setText(f"PTT: {ptt_text}")
        color = "#ffb0b0" if status.ptt_applied is True else "#ffe08a" if status.ptt_applied is None else "#c8f7c5"
        self.ptt_status.setStyleSheet(f"padding: 8px; border: 1px solid #888; border-radius: 4px; background: {color}")
        self.fault_status.setText(status.fault or ("踏板按下" if status.pedal_down else "状态正常"))
        self.fault_status.setStyleSheet(
            "padding: 8px; border: 1px solid #888; border-radius: 4px; background: #ffb0b0"
            if status.fault
            else "padding: 8px; border: 1px solid #888; border-radius: 4px; background: #eee"
        )
        self.tot_bar.setMaximum(status.tot_limit)
        self.tot_bar.setValue(min(status.tot_limit, int(status.tot_elapsed)))
        self.tot_label.setText(f"TOT {status.tot_elapsed:.1f} / {status.tot_limit} 秒")
        self.test_button.setEnabled(status.setup_complete and not status.pedal_down)
        if self._tray is not None:
            tx_state = "TX" if status.ptt_applied is True else "未知" if status.ptt_applied is None else "RX"
            self._tray.setToolTip(f"MIDI PTT | {tx_state} | {status.fault or '正常'}")

    def open_settings(self) -> None:
        if self._settings_pending:
            return
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            new_config = dialog.make_config()
            old_config = self.config
            self.store.save(new_config)
            self._settings_pending = True
            self.settings_button.setEnabled(False)
            self.test_button.setEnabled(False)
            self._append_log("正在安全释放并应用设置…")

            def apply_settings() -> None:
                error: BaseException | None = None
                try:
                    self.engine.update_config(new_config, timeout=65.0)
                except BaseException as exc:
                    error = exc
                    try:
                        self.store.save(old_config)
                    except Exception as rollback_error:
                        error = RuntimeError(f"{exc}; 配置文件回滚失败：{rollback_error}")
                self._settings_results.put((new_config, error))

            self._settings_thread = threading.Thread(
                target=apply_settings, name="settings-apply", daemon=True
            )
            self._settings_thread.start()
        except Exception as exc:
            QMessageBox.critical(self, "设置失败", str(exc))

    def _finish_settings_update(self) -> None:
        if not self._settings_pending:
            return
        try:
            new_config, error = self._settings_results.get_nowait()
        except queue.Empty:
            return
        self._settings_pending = False
        self.settings_button.setEnabled(True)
        self.test_button.setEnabled(True)
        if error is not None:
            self._append_log(f"设置未应用：{error}")
            QMessageBox.critical(self, "设置失败", str(error))
            return
        self.config = new_config
        self.tot_bar.setMaximum(new_config.tot_seconds)
        self._append_log("设置已保存并应用")
        if self._minimized_requested and self._tray is not None:
            QTimer.singleShot(250, self.hide)

    def _test_ptt(self) -> None:
        answer = QMessageBox.warning(
            self,
            "PTT 发射测试",
            "这会触发 500 毫秒 PTT，电台可能实际发射 RF。确认射频环境安全后继续。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        try:
            self.engine.test_ptt(0.5)
        except Exception as exc:
            QMessageBox.warning(self, "PTT 测试未启动", str(exc))

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick}:
            self._show_window()

    def _quit(self) -> None:
        if self._shutdown_pending:
            return
        self._really_quit = True
        self._shutdown_pending = True
        self.settings_button.setEnabled(False)
        self.test_button.setEnabled(False)
        self._append_log("正在释放 PTT 并停止设备…")

        def stop_engine() -> None:
            error: BaseException | None = None
            try:
                self.engine.stop(timeout=100.0)
            except BaseException as exc:
                error = exc
            self._shutdown_results.put(error)

        threading.Thread(target=stop_engine, name="ptt-shutdown", daemon=True).start()
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.timeout.connect(self._finish_shutdown)
        self._shutdown_timer.start(100)

    def _finish_shutdown(self) -> None:
        if not self._shutdown_pending:
            return
        try:
            error = self._shutdown_results.get_nowait()
        except queue.Empty:
            return
        self._shutdown_pending = False
        if error is not None:
            QMessageBox.critical(self, "PTT 安全退出失败", f"停止时未能确认安全状态：{error}")
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _stop_engine(self) -> None:
        try:
            self.engine.stop(timeout=100.0)
        except Exception:
            LOG.exception("PTT engine did not stop cleanly")

    def closeEvent(self, event: Any) -> None:
        if self._really_quit:
            self._stop_engine()
            event.accept()
        elif self._tray is not None:
            self.hide()
            event.ignore()
        else:
            event.ignore()
            self._quit()


def run_gui(
    engine: PttEngine,
    store: ConfigStore,
    config: AppConfig,
    *,
    minimized: bool = False,
    startup_error: str = "",
) -> int:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(engine, store, config, minimized, startup_error)
    stop_requested = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_requested.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass
    atexit.register(engine.stop)
    signal_timer = QTimer(window)
    signal_timer.timeout.connect(lambda: window._quit() if stop_requested.is_set() else None)
    signal_timer.start(100)
    window.show()
    if minimized and config.setup_complete and window._tray is not None:
        window.hide()
    return app.exec()
