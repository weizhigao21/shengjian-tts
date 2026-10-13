"""Render real settings controls using simulated release metadata only."""
import os
from pathlib import Path
import sys
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PyQt6.QtWidgets import QApplication, QMainWindow
from voice_workbench import __version__
from voice_workbench.desktop.settings.panel import SettingsPanel
from voice_workbench.desktop.settings import app_updates
from voice_workbench.desktop.window import configure_font, configure_language
from voice_workbench.app_updates.client import Release, PAGE
from voice_workbench.ui import STYLE


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    configure_font(app)
    configure_language(app)
    with tempfile.TemporaryDirectory(prefix='shengjian-update-preview-') as folder:
        owner = QMainWindow()
        owner.port, owner.data_dir = 8765, Path(folder)
        owner.ready, owner.closing = False, False
        owner.workers = set()
        owner.show_error = lambda message: None
        owner.setStyleSheet(STYLE)
        original = app_updates.frozen
        app_updates.frozen = lambda: True
        try:
            panel = SettingsPanel(owner)
        finally:
            app_updates.frozen = original
        owner.setCentralWidget(panel)
        panel.timer.stop()
        control = panel.app_updates
        control.startup.stop()
        panel.set_section(3)
        panel.data_location.setText('应用数据目录 · 配置、声音预设、模型和音频')
        control.release = Release('0.7.34', 'v0.7.34', '', PAGE, '', '', 1, '0' * 64)
        control.status.setText('发现新版本 v0.7.34（演示）')
        control.install_button.setEnabled(True)
        control.notes.setPlainText('v0.7.34 · 更新说明示例\n\n• 更新后的功能与修复将在这里显示。\n• 下载后校验完整包，点击「安装并重启」。\n• 配置、模型与音频保留，程序文件直接覆盖。\n\n此处为界面演示，实际可用版本以发行页为准。')
        control.notes.show()
        owner.resize(1024, 680)
        owner.show()
        app.processEvents()
        assert control.install_button.geometry().right() < control.width()
        assert owner.grab().save(str(ROOT / 'docs/images/app-updates.png'))
        panel.close_management()
        owner.close()
        print('已生成 v' + __version__ + ' 更新设置截图（模拟发行数据）。')


if __name__ == '__main__':
    main()
