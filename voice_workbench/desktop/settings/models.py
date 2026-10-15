"""ModelSettings responsibilities, composed by the owning Qt widget."""
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QMenu, QMessageBox, QWidget,
)
from ...ui import ElidedLabel, ModelDescriptionDelegate, button, label
from .common import size_text, publication_text, DOWNLOAD_STATES


def recognition_usage(model):
    purposes = [name for key, name in (("recognition_adapted", "语音识别"), ("align_adapted", "时间对齐")) if model.get(key)]
    return " / ".join(purposes)


def adapted_model(model):
    return bool(model.get("adapted", True) or model.get("edit_adapted") or recognition_usage(model))


class ModelSettings:
    def _models_page(self):
        self.models_scroll, layout = self.scroll_page("modelManagementContent")
        self.engine_panel = self._engine_panel()
        self.engine_panel.hide()
        self.source_combo = QComboBox()
        for title, value in (("HF 镜像", "mirror"), ("Hugging Face", "huggingface"), ("ModelScope 魔搭", "modelscope")):
            self.source_combo.addItem(title, value)
        self.source_combo.setFixedWidth(200)
        self.source_combo.setToolTip("下载源；更换源后可继续未完成的下载。更新检查读取 Hugging Face 的固定版本。")
        self.model_directory = ElidedLabel(str(self.owner.data_dir / "models"))
        self.model_directory.setToolTip(self.model_directory.text())
        open_models = button("打开文件夹", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.model_directory.text())), "folder")
        layout.addWidget(label("下载", "hint"))
        layout.addWidget(self.settings_group([
            self.settings_row("下载来源", controls=(self.source_combo,)),
            self.settings_row("模型保存位置", self.model_directory, (open_models,)),
            self.settings_row("导入本地模型", "已有 GGUF 文件可直接导入，无需下载。", (button("导入 GGUF", self.import_model, "import"),))]))
        scan_head = QHBoxLayout()
        self.model_scan_notice = label("首次打开或更换引擎后自动扫描支持的模型。", "hint")
        self.model_scan_notice.setWordWrap(True)
        scan_head.addWidget(self.model_scan_notice, 1)
        self.model_scan_details = button("支持详情", self.show_model_support, "info", "ghost")
        scan_head.addWidget(self.model_scan_details)
        self.model_scan_button = button("扫描支持模型", self.scan_models, "retry", "ghost")
        scan_head.addWidget(self.model_scan_button)
        layout.addLayout(scan_head)
        installed_head = QHBoxLayout()
        self.installed_label = label("已下载 · 正在读取", "hint")
        installed_head.addWidget(self.installed_label)
        installed_head.addStretch()
        self.model_update_check = button("检查模型更新", self.check_model_updates, "retry", "ghost")
        installed_head.addWidget(self.model_update_check)
        layout.addLayout(installed_head)
        self.model_update_notice = label("", "hint")
        self.model_update_notice.setWordWrap(True)
        self.model_update_notice.hide()
        layout.addWidget(self.model_update_notice)
        self.installed_table = self.table(("模型 / 功能", "大小", "发布时间", "状态", "操作"), whole_page=True)
        self.installed_table.setItemDelegateForColumn(0, ModelDescriptionDelegate(self.installed_table))
        self.installed_table.horizontalHeader().hide()
        self.installed_table.hide()
        for col, width in ((1, 84), (2, 110), (3, 105), (4, 190)):
            self.installed_table.setColumnWidth(col, width)
        layout.addWidget(self.installed_table)
        available_head = QHBoxLayout()
        self.catalog_label = label("可下载 · 正在读取", "hint")
        available_head.addWidget(self.catalog_label)
        available_head.addStretch()
        self.model_filter = QComboBox()
        for title, value in (("全部模型", "all"), ("已适配", "adapted"), ("语音生成", "tts"), ("音频编辑 / 换声", "edit"), ("语音识别 / 对齐", "stt"), ("其他音频模型", "other")):
            self.model_filter.addItem(title, value)
        self.model_filter.setMaximumWidth(155)
        self.model_filter.currentIndexChanged.connect(self.render_models)
        available_head.addWidget(self.model_filter)
        self.model_search = QLineEdit()
        self.model_search.setPlaceholderText("搜索名称、精度或用途")
        self.model_search.setMaximumWidth(240)
        self.model_search.textChanged.connect(self.render_models)
        available_head.addWidget(self.model_search)
        layout.addLayout(available_head)
        self.catalog_table = self.table(("模型 / 功能", "精度", "文件大小", "发布时间", "状态", "操作"), whole_page=True)
        self.catalog_table.setItemDelegateForColumn(0, ModelDescriptionDelegate(self.catalog_table))
        self.catalog_table.horizontalHeader().hide()
        self.catalog_table.hide()
        for col, width in ((1, 65), (2, 105), (3, 110), (4, 105), (5, 100)):
            self.catalog_table.setColumnWidth(col, width)
        self.catalog_table.cellDoubleClicked.connect(self.download_details)
        layout.addWidget(self.catalog_table)
        foot = QHBoxLayout()
        foot.addWidget(label("发布时间取 GGUF 文件版本的仓库提交日期；Q8 通常更省内存。", "muted"))
        foot.addStretch()
        foot.addWidget(button("模型来源", lambda: QDesktopServices.openUrl(QUrl("https://github.com/leemysw/yovoice")), "link", "ghost"))
        layout.addLayout(foot)
        layout.addStretch()
        self.pages.addWidget(self.models_scroll)

    def render_models(self, *_):
        if self.model_data is None:
            return
        text = self.model_search.text().strip().casefold()
        matches = lambda m: not text or text in " ".join(str(m.get(k, "")) for k in ("name", "description", "precision", "family")).casefold()
        installed = [m for m in self.model_data["installed"] if matches(m)]
        choice = self.model_filter.currentData()
        speech = {"tts", "clone", "design", "vdes"}
        recognition = {"stt", "asr", "align", "forced_align"}
        def included(m):
            tasks = set(m.get("tasks", ["tts"] if m.get("adapted", True) else []))
            return (choice == "all" or choice == "adapted" and adapted_model(m) or
                    choice == "tts" and bool(tasks & speech) or choice == "stt" and bool(tasks & recognition) or
                    choice == "edit" and bool(tasks & {"edit", "vc", "svc", "s2s"}) or
                    choice == "other" and not bool(tasks & (speech | recognition)))
        catalog = [m for m in self.model_data["catalog"] if matches(m) and not m["installed"] and included(m)]
        scan = self.model_data.get("scan", {})
        scanning = scan.get("scanning", False)
        self.model_scan_button.setEnabled(not scanning)
        self.model_scan_button.setText("扫描中…" if scanning else "扫描支持模型")
        families = scan.get("families", [])
        self.model_scan_details.setEnabled(bool(families))
        if scanning:
            notice = "正在扫描引擎模型声明和官方仓库，无需下载模型文件…"
        elif scan.get("error"):
            notice = scan["error"]
        elif scan.get("checked"):
            total = self.model_data["catalog"]
            notice = f"识别 {len(families)} 个模型家族 · {sum(m['downloadable'] for m in total)} 个可下载 · {sum(adapted_model(m) for m in total)} 个已适配"
            if scan.get("warnings"):
                notice += " · 部分仓库未连接，保留已知目录"
        else:
            notice = "首次打开或更换引擎后自动扫描支持的模型。"
        self.model_scan_notice.setText(notice)
        self.model_scan_notice.setToolTip("\n".join(scan.get("warnings", [])) or notice)
        self.render_engine()
        self.installed_table.horizontalHeaderItem(0).setText(f"已添加的模型 · {len(installed)}")
        self.catalog_table.horizontalHeaderItem(0).setText(f"下载其他模型 · {len(catalog)}")
        self.installed_label.setText(f"已下载 · {len(installed)}" + (" · 没有匹配的模型" if text and not installed else ""))
        self.catalog_label.setText(f"模型目录 · {len(catalog)}" + (" · 没有匹配的模型" if text and not catalog else ""))
        self.installed_label.show()
        self.catalog_label.show()
        table = self.installed_table
        table.setUpdatesEnabled(False)
        self.table_rows(table, [m["id"] for m in installed])
        for row, m in enumerate(installed):
            title = m["name"] + (" · " + m["precision"] if m["precision"] else "")
            cell = self.cell(table, row, 0, title + "\n" + m["description"], title + "\n" + m["description"] + "\n" + m["path"] + f"\n关联预设：{m['preset_count']} 个")
            cell.setData(Qt.ItemDataRole.UserRole + 1, {"title": title, "description": m["description"]})
            self.cell(table, row, 1, "目录模型" if m.get("is_directory") else size_text(m["size"]) if m["exists"] else "文件缺失")
            self.cell(table, row, 2, *publication_text(m))
            status = m.get("update_status")
            summary = DOWNLOAD_STATES.get(status, status) if status else recognition_usage(m) or ("音频编辑" if m.get("edit_adapted") else "待适配" if not m.get("adapted", True) else "可更新" if m.get("update_available") else "已更新" if m.get("can_rollback") else "本地模型")
            if status in ("downloading", "paused", "verifying", "pausing"):
                summary += f" · {min(100,m.get('update_received',0)*100/(m.get('update_size') or 1)):.0f}%"
            self.cell(table, row, 3, summary, m.get("update_error") or m["path"])
            key = f"{m['id']}:{bool(m.get('update_available'))}:{status or ''}"
            self.action_cell(table, row, 4, key, lambda v=m: self.model_actions(v))
            table.setRowHeight(row, 66)
        table.fit_rows()
        table.setUpdatesEnabled(True)
        table = self.catalog_table
        table.setUpdatesEnabled(False)
        self.table_rows(table, [m["id"] for m in catalog])
        self.visible_catalog = catalog
        for row, m in enumerate(catalog):
            cell = self.cell(table, row, 0, m["name"] + "\n" + m["description"])
            cell.setData(Qt.ItemDataRole.UserRole + 1, {"title": m["name"], "description": m["description"]})
            self.cell(table, row, 1, m["precision"])
            self.cell(table, row, 2, size_text(m["size"]))
            self.cell(table, row, 3, *publication_text(m))
            status = m["status"]
            summary = DOWNLOAD_STATES.get(status, status)
            if not m.get("adapted", True) and status == "available":
                summary = recognition_usage(m) or ("音频编辑" if m.get("edit_adapted") else "待适配")
            if status in ("downloading", "paused", "pausing", "verifying"):
                summary += f" · {min(100, m.get('received', 0) * 100 / m['size']):.0f}%"
            if m.get("error"):
                summary += "\n" + m["error"]
            self.cell(table, row, 4, summary, m.get("error") or summary)
            if status in ("downloading", "verifying", "pausing"):
                action = self.action_cell(table, row, 5, m["id"] + "-pause", lambda v=m: button("暂停", lambda: self.pause_download(v), "pause", "ghost"))
                action.setEnabled(status != "pausing")
            else:
                caption = "继续" if status == "paused" else "重试" if status == "failed" else "下载" if m["downloadable"] else "多文件" if m.get("discovered") else "仅导入"
                action = self.action_cell(table, row, 5, m["id"] + "-download", lambda v=m: button("下载", lambda: self.download_model(v), "import", "ghost"))
                action.setText(caption)
                action.setEnabled(m["downloadable"])
                action.setToolTip("下载后可用于" + recognition_usage(m) + "。" if recognition_usage(m) else "下载后可在音频编辑模式使用。" if m.get("edit_adapted") else "可以下载保存；声笺尚未适配，暂不能用于配音。" if m["downloadable"] and not m.get("adapted", True) else "此模型需要配套文件，暂不支持自动安装。" if m.get("discovered") and not m["downloadable"] else caption)
            table.setRowHeight(row, 66)
        table.fit_rows()
        table.setUpdatesEnabled(True)

    def scan_models(self):
        from ..api_client import checked
        self.model_scan_button.setEnabled(False)
        source = self.source_combo.currentData()
        def complete(value):
            if self.model_data is not None:
                self.model_data["scan"] = value
                self.render_models()
            self.refresh()
        def failed(message):
            self.model_scan_button.setEnabled(True)
            self.model_scan_notice.setText(str(message))
        self.owner.call(lambda c: checked(c.post("/models/scan", json={"source": source})), complete, failed)

    def show_model_support(self):
        from ...model_scan import TASK_NAMES
        families = (self.model_data or {}).get("scan", {}).get("families", [])
        states = {"supported": "已支持", "community": "社区支持", "experimental": "实验性", "testing": "测试中", "wip": "开发中"}
        rows = ["当前引擎随附的模型声明；‘已适配’筛选中的模型可用于配音。其他单文件 GGUF 可下载保存，多文件包暂不支持自动安装。"]
        for f in families:
            tasks = "、".join(dict.fromkeys(TASK_NAMES.get(t, t) for t in f["tasks"]))
            rows.append(f"{f['name']} · {tasks or '音频处理'} · {states.get(f['status'], f['status'])} ({f['family']})")
        self.owner.show_details("\n".join(rows))

    def model_actions(self, model):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(4)
        status = model.get("update_status")
        if model.get("update_available") or status:
            busy = status in ("downloading", "verifying", "waiting", "pausing")
            action = button("暂停" if busy else "继续" if status == "paused" else "重试" if status == "failed" else "更新",
                lambda: self.pause_download({"id": model["catalog_id"]}) if busy else self.update_model(model),
                "pause" if busy else "retry", "ghost")
            action.setEnabled(status != "pausing")
            layout.addWidget(action)
        else:
            layout.addStretch()
        layout.addWidget(button("删除", lambda: self.remove_model(model), "trash", "ghost"))
        more = button("", lambda: None, "info", "icon")
        menu = QMenu(more)
        menu.addAction("模型详情", lambda: self.owner.show_details(model["name"] + "\n" + model["description"] + "\n" + model["path"] + "\n版本：" + (model.get("revision") or "未记录") + "\n" + publication_text(model)[1]))
        if model.get("can_rollback"):
            menu.addAction("回退到上个版本", lambda: self.mutate(lambda c: c.post(f"/models/{model['id']}/rollback"), "模型已回退，预设参数保留。"))
        more.setMenu(menu)
        more.setToolTip("模型详情与版本回退")
        layout.addWidget(more)
        return row

    def check_model_updates(self):
        from ..api_client import checked
        self.model_update_check.setEnabled(False)
        self.model_update_check.setText("检查中…")
        source = self.source_combo.currentData()
        def complete(value):
            self.model_update_check.setEnabled(True)
            self.model_update_check.setText("检查模型更新")
            self.model_data = value
            count = sum(bool(m.get("update_available")) for m in value["installed"])
            self.model_update_notice.setText(f"发现 {count} 个模型有更新。新版校验后切换，旧文件保留。" if count else "已检查，目录中的已安装模型没有新的文件版本。")
            self.model_update_notice.show()
            self.render_models()
        def failed(message):
            self.model_update_check.setEnabled(True)
            self.model_update_check.setText("检查模型更新")
            self.model_update_notice.setText(str(message))
            self.model_update_notice.show()
        self.owner.call(lambda c: checked(c.post("/models/check-updates", json={"source": source}, timeout=30)), complete, failed)

    def update_model(self, model):
        source = self.source_combo.currentData()
        self.mutate(lambda c: c.post(f"/models/{model['id']}/update", json={"source": source}), "新版开始下载；校验通过并结束当前生成后切换，旧文件保留。")

    def download_model(self, model):
        source = self.source_combo.currentData()
        message = "模型开始下载，可在列表中暂停和查看进度。"
        if recognition_usage(model):
            message += " 下载后可在语音识别模式使用。"
        elif model.get("edit_adapted"):
            message += " 下载后可在音频编辑模式选择 Vevo2。"
        elif not model.get("adapted", True):
            message += " 此模型尚未适配，下载后仅保存文件，暂不能用于配音。"
        self.mutate(lambda c: c.post(f"/models/{model['id']}/download", json={"source": source}), message)

    def pause_download(self, model):
        self.mutate(lambda c: c.post(f"/models/{model['id']}/pause"))

    def download_details(self, row, _):
        m = self.visible_catalog[row]
        support = "可用于" + recognition_usage(m) if recognition_usage(m) else "可用于音频编辑：换声、修改台词" if m.get("edit_adapted") else "可用于配音" if m.get("adapted", True) else "尚未适配，下载后仅保存文件"
        self.owner.show_details(m["name"] + "\n" + m["description"] +
            "\n" + publication_text(m)[1] +
            f"\n适配：{support}\n精度：{m['precision']}\n大小：{size_text(m['size'])}" +
            f"\n仓库：{m.get('repo') or 'audio-cpp/audio.cpp-gguf'}\n文件：{m.get('remotePath', '')}" +
            f"\n提交：{m.get('revision', '')}\nSHA-256：{m['sha256']}\n" + m.get("error", ""))

    def import_model(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入本地模型", "", "语音模型 (*.gguf)")
        if path:
            self.mutate(lambda c: c.post("/models/import", json={"path": path}), "模型已添加，语音创作中可以直接选择。")

    def remove_model(self, model):
        model = next((m for m in self.model_data["installed"] if m["id"] == model["id"]), model)
        box = QMessageBox(self)
        box.setWindowTitle("删除模型")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("移除模型：" + model["name"])
        box.setInformativeText(f"关联的 {model['preset_count']} 个声音预设会一起移除。\n{model['path']}")
        checkbox = QCheckBox("同时删除模型文件（无法撤销）")
        checkbox.setChecked(model["managed"] and model["can_delete_file"])
        checkbox.setEnabled(model["can_delete_file"])
        box.setCheckBox(checkbox)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText("删除")
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Yes:
            delete_file = checkbox.isChecked()
            self.mutate(lambda c: c.post(f"/models/{model['id']}/remove", json={"delete_file": delete_file}), "模型已移除。")
