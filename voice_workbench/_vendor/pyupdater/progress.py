"""py-updater independent progress window; see SOURCE.md."""
import logging
logger = logging.getLogger(__name__)

class ProgressWindow:
    """最小进度窗；`enabled=False` 或 tkinter 不可用时所有方法退化为空操作。"""

    def __init__(self, app_name: str = "应用", enabled: bool = True):
        self._root = None
        self._status = None
        self._bar = None
        if not enabled:
            return
        try:
            import tkinter as tk
            from tkinter import ttk

            self._root = tk.Tk()
            self._root.title(f"正在更新 {app_name}")
            self._root.geometry("420x130")
            self._root.resizable(False, False)
            self._root.attributes("-topmost", True)
            tk.Label(
                self._root,
                text=f"{app_name} 更新程序",
                font=("Microsoft YaHei UI", 12, "bold"),
            ).pack(pady=(14, 4))
            self._status = tk.Label(self._root, text="准备中…", font=("Microsoft YaHei UI", 9))
            self._status.pack()
            self._bar = ttk.Progressbar(self._root, length=360, mode="determinate", maximum=100)
            self._bar.pack(pady=10)
            self._pump()
        except Exception as exc:  # 无图形环境 / tkinter 缺失：静默运行
            logger.info("进度窗不可用（%s），改为静默更新", exc)
            self._root = None

    def _pump(self) -> None:
        if self._root is not None:
            try:
                self._root.update_idletasks()
                self._root.update()
            except Exception:
                self._root = None

    def status(self, text: str) -> None:
        logger.info("%s", text)
        if self._status is not None and self._root is not None:
            try:
                self._status.config(text=text)
            except Exception:
                pass
        self._pump()

    def progress(self, done: int, total: int) -> None:
        if self._bar is None or not total:
            return
        try:
            self._bar["value"] = min(100, int(done * 100 / total))
        except Exception:
            return
        self._pump()

    def error(self, message: str) -> None:
        logger.error("%s", message)
        if self._root is None:
            return
        try:
            from tkinter import messagebox

            messagebox.showerror("更新失败", message)
        except Exception:
            pass
        self.close()

    def close(self) -> None:
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None
