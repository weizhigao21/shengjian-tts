"""A filter-independent queue for model downloads and model updates."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from ...ui import ModelDescriptionDelegate, button, label
from .common import DOWNLOAD_STATES, size_text

TRANSFER_STATES = {"downloading", "pausing", "paused", "verifying", "waiting", "extracting", "failed"}
BUSY_STATES = {"downloading", "pausing", "verifying", "waiting", "extracting"}


def speed_text(speed):
    speed = max(0, speed or 0)
    if speed >= 1024 ** 2:
        return f"{speed / 1024 ** 2:.2f} MB/s"
    if speed >= 1024:
        return f"{speed / 1024:.1f} KB/s"
    return f"{speed:.0f} B/s"


class ModelTransfers:
    def _model_downloads_area(self):
        self.model_downloads = QWidget()
        layout = QVBoxLayout(self.model_downloads)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.model_downloads_title = label("下载任务", "section")
        layout.addWidget(self.model_downloads_title)
        self.downloads_table = self.table(("模型", "下载进度", "速度", "状态", "操作"), whole_page=True)
        self.downloads_table.setItemDelegateForColumn(0, ModelDescriptionDelegate(self.downloads_table))
        for column, width in ((1, 175), (2, 95), (3, 100), (4, 85)):
            self.downloads_table.setColumnWidth(column, width)
        layout.addWidget(self.downloads_table)
        self.model_downloads.hide()
        return self.model_downloads

    def render_model_downloads(self):
        tasks = [m for m in self.model_data["catalog"] if m["status"] in TRANSFER_STATES]
        tasks.sort(key=lambda m: m["status"] not in BUSY_STATES)
        self.model_downloads.setVisible(bool(tasks))
        self.model_downloads_title.setText(f"下载任务 · {len(tasks)}")
        table = self.downloads_table
        table.setUpdatesEnabled(False)
        self.table_rows(table, [m["id"] for m in tasks])
        for row, model in enumerate(tasks):
            title = model["name"] + (" · " + model["precision"] if model.get("precision") else "")
            if model.get("update"):
                title += " · 更新"
            cell = self.cell(table, row, 0, title + "\n" + model["description"])
            cell.setData(Qt.ItemDataRole.UserRole + 1, {"title": title, "description": model["description"]})
            received, total = model.get("received", 0), model.get("size", 0)
            percent = min(100, received * 100 / total) if total else 0
            self.cell(table, row, 1, f"{percent:.1f}%\n{size_text(received)} / {size_text(total)}")
            self.cell(table, row, 2, speed_text(model.get("speed_bps", 0)) if model["status"] == "downloading" else "—")
            status_text = DOWNLOAD_STATES.get(model["status"], model["status"])
            self.cell(table, row, 3, status_text, model.get("error") or status_text)
            key = f"{model['id']}:{model['status']}:{bool(model.get('update'))}"
            action = self.action_cell(table, row, 4, key, lambda m=model: self.model_transfer_action(m))
            action.setEnabled(model["status"] != "pausing")
            table.setRowHeight(row, 66)
        table.fit_rows()
        table.setUpdatesEnabled(True)
        return {m["id"] for m in tasks}

    def model_transfer_action(self, model):
        if model["status"] in BUSY_STATES:
            return button("暂停", lambda: self.pause_download(model), "pause", "ghost")
        caption = "重试" if model["status"] == "failed" else "继续"
        def resume():
            if model.get("update"):
                installed = next((m for m in self.model_data["installed"] if m.get("catalog_id") == model["id"]), None)
                if installed:
                    self.update_model(installed)
                    return
                self.owner.show_error("原模型已移除，请刷新模型列表后重新下载。")
                return
            self.download_model(model)
        return button(caption, resume, "retry", "ghost")
