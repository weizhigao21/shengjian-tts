"""EngineSettings responsibilities, composed by the owning Qt widget."""
from pathlib import Path
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QMenu, QVBoxLayout, QWidget, QProgressBar,
    QSizePolicy,
)
from ...ui import ElidedLabel, button, icon, label
from ...engine_sources import SOURCE_NAMES
from .common import size_text, DOWNLOAD_STATES, ARCHIVE_STATES


class EngineSettings:
    def _engine_page(self):
        self.engine_scroll, layout = self.scroll_page("engineSettingsContent")
        layout.addWidget(self.engine_panel)
        self.engine_panel.show()
        layout.addStretch()
        self.pages.addWidget(self.engine_scroll)

    def _engine_panel(self):
        panel = QWidget()
        panel.setObjectName("engineManagement")
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.engine_version = label("audio.cpp", "hint")
        layout.addWidget(self.engine_version)
        self.engine_backend = QComboBox()
        self.engine_backend.addItem("NVIDIA GPU · CUDA 12.4", "cuda")
        self.engine_backend.addItem("CPU 便携版", "cpu")
        self.engine_backend.setFixedWidth(240)
        self.engine_backend.currentIndexChanged.connect(self.render_engine)
        self.engine_source = QComboBox()
        for key, name in SOURCE_NAMES.items():
            self.engine_source.addItem(name, key)
        self.engine_source.setFixedWidth(240)
        self.engine_source.setToolTip('自动模式依次尝试 ghfast、GH-Proxy 和官方直连。第三方仅用于下载，文件仍按官方 SHA-256 校验。暂停后可更换来源继续下载。')
        self.engine_source_dirty = False
        self.engine_source.currentIndexChanged.connect(self._engine_source_changed)
        self.engine_status = ElidedLabel("等待服务连接")
        self.engine_status.setProperty("role", "muted")
        self.engine_verify = button("验证", self.verify_engine, "check", "ghost")
        self.engine_verify.setEnabled(False)
        self.engine_download = button("下载并安装", self.toggle_engine_install, "import")
        self.engine_download.setFixedWidth(150)
        self.engine_download.setEnabled(False)
        self.engine_more = button("更多", lambda: None, kind="ghost")
        self.engine_menu = QMenu(self.engine_more)
        self.local_engine_action = self.engine_menu.addAction("使用本地引擎…", self.choose_runtime)
        self.local_engine_action.setEnabled(False)
        self.engine_release_action = self.engine_menu.addAction("官方下载", lambda: QDesktopServices.openUrl(QUrl(self.engine_release_url)))
        self.engine_more.setMenu(self.engine_menu)
        self.engine_description = ElidedLabel()
        self.engine_description.setProperty("role", "hint")
        self.runtime_label = ElidedLabel()
        self.runtime_label.setProperty("role", "muted")
        self.engine_check = button("检查更新", self.check_engine_updates, "retry", "ghost")
        self.engine_rollback = button("回退", self.rollback_engine, kind="ghost")
        self.engine_rollback.setEnabled(False)
        layout.addWidget(self.settings_group([
            self.settings_row("计算设备", controls=(self.engine_backend,)),
            self.settings_row("下载来源", controls=(self.engine_source,)),
            self.settings_row("推理内核", self.engine_description, (self.engine_verify, self.engine_download, self.engine_more)),
            self.settings_row("版本维护", "新版本单独安装，旧版本保留供回退。", (self.engine_check, self.engine_rollback))]))
        state = QHBoxLayout()
        state.addWidget(self.engine_status, 1)
        state.addWidget(self.runtime_label, 1)
        layout.addLayout(state)
        self.engine_details_toggle = button("安装详情", lambda: None, "chevron", "disclosure")
        self.engine_details_toggle.setCheckable(True)
        layout.addWidget(self.engine_details_toggle)
        self.engine_assets = QWidget()
        self.engine_assets.hide()
        self.engine_details_toggle.toggled.connect(self.engine_assets.setVisible)
        self.engine_details_toggle.toggled.connect(lambda opened: self.engine_details_toggle.setIcon(icon("chevron-down" if opened else "chevron")))
        grid = QGridLayout(self.engine_assets)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(2, 1)
        self.engine_asset_rows = []
        for index in range(max(len(e["archives"]) for e in self.engine_catalog["backends"])):
            title, amount, state = (ElidedLabel() for _ in range(3))
            for text in (title, amount, state):
                text.setProperty("role", "muted")
                text.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            progress = QProgressBar()
            progress.setTextVisible(False)
            progress.setFixedHeight(3)
            progress.setObjectName("engineAssetProgress")
            amount.setMinimumWidth(156)
            amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            title.setFixedWidth(84)
            state.setFixedWidth(100)
            state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            for col, widget in enumerate((title, amount, progress, state)):
                grid.addWidget(widget, index, col)
            self.engine_asset_rows.append((title, amount, progress, state))
        layout.addWidget(self.engine_assets)
        self.engine_progress = QProgressBar()
        self.engine_progress.setTextVisible(False)
        self.engine_progress.setFixedHeight(3)
        policy = self.engine_progress.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.engine_progress.setSizePolicy(policy)
        self.engine_progress.hide()
        layout.addWidget(self.engine_progress)
        self.engine_error = label("", "hint")
        self.engine_error.setObjectName("engineError")
        self.engine_error.setWordWrap(True)
        self.engine_error.hide()
        layout.addWidget(self.engine_error)
        self.render_engine()
        return panel

    def check_engine_updates(self):
        from ..api_client import checked
        self.engine_check.setEnabled(False)
        self.engine_check.setText("检查中…")
        def complete(value):
            self.engine_check.setEnabled(True)
            self.engine_check.setText("检查更新")
            self.model_data = {**(self.model_data or {}), "engine": value}
            self.render_engine()
            self.owner.statusBar().showMessage("引擎版本信息已更新", 5000)
        def failed(message):
            self.engine_check.setEnabled(True)
            self.engine_check.setText("检查更新")
            self.engine_error.setText(str(message))
            self.engine_error.show()
        self.owner.call(lambda c: checked(c.post("/models/runtime/check-updates", timeout=30)), complete, failed)

    def rollback_engine(self):
        backend = self.engine_backend.currentData()
        self.mutate(lambda c: c.post("/models/runtime/rollback", json={"backend": backend}), "已回退到旧引擎，模型与声音预设保留。")

    def render_engine(self, *_):
        data = self.model_data or {}
        engine = data.get("engine", {})
        runtime = engine.get("path") or data.get("runtime")
        ready = engine.get("ready", bool(runtime and Path(runtime).is_file()))
        self.runtime_label.setText("当前 · " + Path(runtime).name if ready and runtime else "当前未配置引擎")
        self.runtime_label.setToolTip(runtime or engine.get("directory") or str(self.owner.data_dir / "runtime"))
        backend = self.engine_backend.currentData()
        entry = next(e for e in self.engine_catalog["backends"] if e["id"] == backend)
        option = next((e for e in engine.get("options", []) if e["backend"] == backend), {})
        version = engine.get("version") or (option.get("installed_version") if option.get("path") == runtime else None)
        self.engine_version.setText("audio.cpp · " + (version or "本地版本未记录" if ready else "尚未安装"))
        upgrade = option.get("update_available", False)
        recommended = option.get("recommended_version", "")
        installed = "audio.cpp " + (version or "版本未知")
        self.engine_description.setText(installed + " · 可以生成" + (" · 可升级到 " + recommended if upgrade else "")
                                        if ready else "安装后即可使用本地模型")
        self.engine_description.setToolTip(self.engine_description.text() + "\n" + (runtime or ""))
        self.engine_release_url = engine.get("release") or self.engine_catalog["release"]
        status = option.get("status", "available")
        busy = status in ("downloading", "verifying", "extracting", "waiting", "pausing")
        any_busy = any(e.get("status") in ("downloading", "verifying", "extracting", "waiting", "pausing") for e in engine.get("options", []))
        active = bool(ready and option.get("path") == runtime)
        caption = ("正在暂停" if status == "pausing" else "暂停下载" if status == "downloading" else
                   "暂停安装" if busy else "继续安装" if status == "paused" else "升级到 " + recommended if upgrade else "已拥有" if active else
                   "启用引擎" if option.get("installed") else "重试安装" if status == "failed" else
                   "补全缺失组件" if any(a.get("verified") for a in option.get("archives", [])) else "下载并安装")
        self.engine_download.setText(caption)
        self.engine_download.setIcon(icon("pause" if busy else "retry" if upgrade else "check" if active else "import"))
        self.engine_download.setEnabled(self.owner.ready and engine.get("supported", True) and status != "pausing"
                                        and not engine.get("checking_updates") and (busy or (not any_busy and (not active or upgrade))))
        self.engine_check.setEnabled(self.owner.ready and not any_busy and self.engine_check.text() != "检查中…")
        self.engine_source.setEnabled(self.owner.ready and not any_busy)
        if not self.engine_source_dirty:
            index = self.engine_source.findData(engine.get('download_source', 'auto'))
            if index >= 0:
                self.engine_source.blockSignals(True)
                self.engine_source.setCurrentIndex(index)
                self.engine_source.blockSignals(False)
        self.engine_rollback.setEnabled(self.owner.ready and not any_busy and option.get("can_rollback", False))
        self.engine_rollback.setToolTip("回退到 " + (option.get("rollback_version") or "旧引擎"))
        self.local_engine_action.setEnabled(self.owner.ready and not any_busy)
        self.engine_verify.setEnabled(self.owner.ready and not any_busy)
        self.engine_download.setToolTip(option.get("description", "完成后自动更新模型的引擎路径。") +
                                       "\n保存目录：" + str(self.owner.data_dir / "runtime"))
        self.engine_status.setText("等待服务连接" if not self.owner.ready else DOWNLOAD_STATES[status] if busy else
                                   "已拥有 · 当前使用" if active else "已拥有" if option.get("installed") else DOWNLOAD_STATES.get(status, status))
        source_detail = '下载来源：' + SOURCE_NAMES.get(option.get('active_source'), SOURCE_NAMES.get(option.get('source'), '未开始下载'))
        source_detail += ''.join(f"\n{SOURCE_NAMES.get(e['source'], e['source'])}：{e['message']}" for e in option.get('source_errors', []))
        self.engine_status.setToolTip(self.engine_status.text() + "\n" + option.get("archive", "") + "\n" +
                                     option.get("description", entry["description"]) + "\n" + source_detail)
        archives = option.get("archives") or [{**a, "received": a["size"] if option.get("installed") else 0,
                                               "status": "done" if option.get("installed") else "available"}
                                              for a in entry["archives"]]
        for index, widgets in enumerate(self.engine_asset_rows):
            for widget in widgets:
                widget.setVisible(index < len(archives))
            if index >= len(archives):
                continue
            archive = archives[index]
            title, amount, progress, state = widgets
            title.setText("推理程序" if index == 0 else "CUDA 运行库")
            title.setToolTip(archive["name"])
            received, total = archive.get("received", 0), archive["size"]
            amount.setText(size_text(total) if archive["status"] == "done" else f"{size_text(received)} / {size_text(total)}")
            progress.setRange(0, 1000)
            progress.setValue(min(1000, int(received * 1000 / (total or 1))))
            state.setText(ARCHIVE_STATES.get(archive["status"], archive["status"]))
            state.setToolTip(archive["name"] + "\n" + state.text() + "\n" + "\n".join(archive.get("issues", [])))
        self.engine_progress.setVisible(busy or status == "paused")
        self.engine_progress.setToolTip(f"整体进度：{size_text(option.get('received', 0))} / {size_text(option.get('size', sum(a['size'] for a in archives)))}")
        if status in ("extracting", "waiting"):
            self.engine_progress.setRange(0, 0)
        else:
            self.engine_progress.setRange(0, 1000)
            self.engine_progress.setValue(min(1000, int(option.get("received", 0) * 1000 / (option.get("size") or 1))))
        validation = option.get("verification", {})
        issues = validation.get("issues", []) if validation.get("path") and Path(validation["path"]).is_file() else []
        error = option.get("error") or engine.get("update_error") or ("验证未通过：" + "；".join(issues) if issues and not busy else "")
        self.engine_error.setText(error)
        self.engine_error.setVisible(bool(error))

    def verify_engine(self):
        from ..api_client import checked
        backend = self.engine_backend.currentData()
        self.engine_verify.setEnabled(False)
        def done(value):
            self.model_data = {**(self.model_data or {}), "engine": value["engine"]}
            self.render_engine()
            result = value["verification"]
            self.owner.statusBar().showMessage("验证通过，推理引擎和运行库已拥有。" if result["valid"] else "验证未通过：" + "；".join(result["issues"]), 6000)
        self.owner.call(lambda c: checked(c.post("/models/runtime/verify", json={"backend": backend})), done,
                        lambda message: (self.render_engine(), self.owner.statusBar().showMessage(str(message), 6000)))

    def toggle_engine_install(self):
        backend = self.engine_backend.currentData()
        option = next((e for e in (self.model_data or {}).get("engine", {}).get("options", []) if e["backend"] == backend), {})
        if option.get("status") in ("downloading", "verifying", "extracting", "waiting"):
            self.pause_engine()
        else:
            self.download_engine()

    def download_engine(self, _checked=False, *, backend=None):
        backend = backend or self.engine_backend.currentData()
        self.engine_backend.setCurrentIndex(self.engine_backend.findData(backend))
        self.set_section(4)
        engine = (self.model_data or {}).get("engine", {})
        option = next((e for e in engine.get("options", []) if e["backend"] == backend), {})
        action = ("upgrade" if option.get("update_available") or option.get("upgrade") else
                  "use" if option.get("installed") and option.get("status") not in ("paused", "failed") else "download")
        payload = {'backend': backend}
        if action != 'use':
            payload['source'] = self.engine_source.currentData()
        self.mutate(lambda c: c.post(f"/models/runtime/{action}", json=payload),
                    "已启用推理引擎。" if action == "use" else "引擎开始下载，完成后自动配置；设置中可查看进度。")

    def _engine_source_changed(self, *_):
        self.engine_source_dirty = True

    def pause_engine(self):
        backend = self.engine_backend.currentData()
        self.mutate(lambda c: c.post("/models/runtime/pause", json={"backend": backend}))

    def choose_runtime(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 audio.cpp 推理引擎", "", "推理引擎 (audiocpp_server.exe);;程序 (*.exe)")
        if path:
            self.mutate(lambda c: c.put("/models/runtime", json={"path": path}), "已更新统一推理引擎。")
