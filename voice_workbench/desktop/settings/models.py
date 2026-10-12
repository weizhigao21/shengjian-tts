"""ModelSettings responsibilities, composed by the owning Qt widget."""
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QMenu, QMessageBox, QWidget,
)
from ...ui import ElidedLabel, ModelDescriptionDelegate, button, label
from .common import size_text, DOWNLOAD_STATES


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
        self.installed_table = self.table(("模型 / 功能", "大小", "状态", "操作"), whole_page=True)
        self.installed_table.setItemDelegateForColumn(0, ModelDescriptionDelegate(self.installed_table))
        self.installed_table.horizontalHeader().hide()
        self.installed_table.hide()
        for col, width in ((1, 84), (2, 105), (3, 190)):
            self.installed_table.setColumnWidth(col, width)
        layout.addWidget(self.installed_table)
        available_head = QHBoxLayout()
        self.catalog_label = label("可下载 · 正在读取", "hint")
        available_head.addWidget(self.catalog_label)
        available_head.addStretch()
        self.model_search = QLineEdit()
        self.model_search.setPlaceholderText("搜索名称、精度或用途")
        self.model_search.setMaximumWidth(280)
        self.model_search.textChanged.connect(self.render_models)
        available_head.addWidget(self.model_search)
        layout.addLayout(available_head)
        self.catalog_table = self.table(("模型 / 功能", "精度", "文件大小", "状态", "操作"), whole_page=True)
        self.catalog_table.setItemDelegateForColumn(0, ModelDescriptionDelegate(self.catalog_table))
        self.catalog_table.horizontalHeader().hide()
        self.catalog_table.hide()
        for col, width in ((1, 65), (2, 82), (3, 135), (4, 100)):
            self.catalog_table.setColumnWidth(col, width)
        self.catalog_table.cellDoubleClicked.connect(self.download_details)
        layout.addWidget(self.catalog_table)
        foot = QHBoxLayout()
        foot.addWidget(label("模型功能以文件配置为准；Q8 通常更省内存。", "muted"))
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
        catalog = [m for m in self.model_data["catalog"] if matches(m) and not m["installed"]]
        self.render_engine()
        self.installed_table.horizontalHeaderItem(0).setText(f"已添加的模型 · {len(installed)}")
        self.catalog_table.horizontalHeaderItem(0).setText(f"下载其他模型 · {len(catalog)}")
        self.installed_label.setText(f"已下载 · {len(installed)}" + (" · 没有匹配的模型" if text and not installed else ""))
        self.catalog_label.setText(f"可下载 · {len(catalog)}" + (" · 没有匹配的模型" if text and not catalog else ""))
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
            status = m.get("update_status")
            summary = DOWNLOAD_STATES.get(status, status) if status else "可更新" if m.get("update_available") else "已更新" if m.get("can_rollback") else "本地模型"
            if status in ("downloading", "paused", "verifying", "pausing"):
                summary += f" · {min(100,m.get('update_received',0)*100/(m.get('update_size') or 1)):.0f}%"
            self.cell(table, row, 2, summary, m.get("update_error") or m["path"])
            key = f"{m['id']}:{bool(m.get('update_available'))}:{status or ''}"
            self.action_cell(table, row, 3, key, lambda v=m: self.model_actions(v))
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
            status = m["status"]
            summary = DOWNLOAD_STATES.get(status, status)
            if status in ("downloading", "paused", "pausing", "verifying"):
                summary += f" · {min(100, m.get('received', 0) * 100 / m['size']):.0f}%"
            if m.get("error"):
                summary += "\n" + m["error"]
            self.cell(table, row, 3, summary, m.get("error") or summary)
            if status in ("downloading", "verifying", "pausing"):
                action = self.action_cell(table, row, 4, m["id"] + "-pause", lambda v=m: button("暂停", lambda: self.pause_download(v), "pause", "ghost"))
                action.setEnabled(status != "pausing")
            else:
                caption = "继续" if status == "paused" else "重试" if status == "failed" else "下载" if m["downloadable"] else "仅导入"
                action = self.action_cell(table, row, 4, m["id"] + "-download", lambda v=m: button("下载", lambda: self.download_model(v), "import", "ghost"))
                action.setText(caption)
                action.setEnabled(m["downloadable"])
            table.setRowHeight(row, 66)
        table.fit_rows()
        table.setUpdatesEnabled(True)

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
        menu.addAction("模型详情", lambda: self.owner.show_details(model["name"] + "\n" + model["description"] + "\n" + model["path"] + "\n版本：" + (model.get("revision") or "未记录")))
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
        self.mutate(lambda c: c.post(f"/models/{model['id']}/download", json={"source": source}), "模型开始下载，可在列表中暂停和查看进度。")

    def pause_download(self, model):
        self.mutate(lambda c: c.post(f"/models/{model['id']}/pause"))

    def download_details(self, row, _):
        m = self.visible_catalog[row]
        self.owner.show_details(m["name"] + "\n" + m["description"] + f"\n精度：{m['precision']}\n大小：{size_text(m['size'])}\nSHA-256：{m['sha256']}\n" + m.get("error", ""))

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
