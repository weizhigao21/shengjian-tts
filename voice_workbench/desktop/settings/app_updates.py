"""Qt update controls; networking and archive checks run outside the UI thread."""
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from PyQt6.QtCore import QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QProgressBar, QTextEdit, QVBoxLayout, QWidget
from ... import __version__
from ...app_updates.client import PAGE, check_release, configuration, download, version_tuple
from ...app_updates.manifest import validate_archive, UPDATER, check_data_boundary
from ..._vendor.pyupdater.apply import launch_updater
from ...runtime import frozen, project_root, external_environment
from ...ui import button, label
from ..api_client import checked


class UpdateWorker(QThread):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int)

    def __init__(self, release=None):
        super().__init__()
        self.release = release
        self.cancelled = threading.Event()

    def run(self):
        try:
            if self.release is None:
                value = check_release(cancelled=self.cancelled)
            else:
                directory = Path(tempfile.mkdtemp(prefix='shengjian-update-'))
                value = download(self.release, directory, progress=lambda done, total: self.progress.emit(done, total), cancelled=self.cancelled)
                validate_archive(value, configuration(), self.release.version, cancelled=self.cancelled)
            if not self.cancelled.is_set():
                self.result.emit(value)
        except Exception as exc:
            if not self.cancelled.is_set():
                self.error.emit(str(exc))


class AppUpdateControls(QWidget):
    def __init__(self, settings):
        super().__init__(settings)
        self.settings, self.owner = settings, settings.owner
        self.worker = None
        self.release = None
        self.archive = None
        self.helper = None
        self.work = None
        self.watch_started = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.status = label('检查 GitHub 正式发行版，下载后校验再安装。', 'hint')
        self.status.setWordWrap(True)
        self.check_button = button('检查更新', self.check, 'retry')
        self.install_button = button('下载更新', self.start_download, 'import')
        self.install_button.setEnabled(False)
        self.cancel_button = button('取消下载', self.cancel, 'close', 'ghost')
        self.cancel_button.hide()
        page = button('发行页', lambda: QDesktopServices.openUrl(QUrl(self.release.page if self.release else PAGE)), 'link', 'ghost')
        layout.addWidget(settings.settings_group([
            settings.settings_row('声笺 v' + __version__, self.status, (self.check_button, page)),
            settings.settings_row('程序更新', '直接覆盖程序文件，保留配置、模型与音频。', (self.install_button, self.cancel_button))]))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(200)
        self.notes.setPlaceholderText('发现新版本后显示更新说明。')
        self.notes.hide()
        layout.addWidget(self.notes)
        if not frozen():
            layout.addWidget(label('源码模式可检查版本；自动安装在 Windows 打包版启用。', 'muted'))
        self.watch = QTimer(self)
        self.watch.setInterval(200)
        self.watch.timeout.connect(self.helper_ready)
        self.startup = QTimer(self)
        self.startup.setSingleShot(True)
        self.startup.timeout.connect(lambda: self.check(silent=True))
        if frozen():
            self.startup.start(6000)

    def run_worker(self, worker, success):
        self.worker = worker
        self.owner.workers.add(worker)
        self.check_button.setEnabled(False)
        self.install_button.setEnabled(False)
        def result(value):
            if not self.owner.closing:
                success(value)
        def failed(message):
            if not self.owner.closing:
                self.status.setText(message)
                self.progress.hide()
                self.cancel_button.hide()
        def finished():
            self.owner.workers.discard(worker)
            if self.worker is worker:
                self.worker = None
            if not self.owner.closing:
                self.check_button.setEnabled(True)
                self.install_button.setEnabled(bool(self.release and frozen()))
        worker.result.connect(result)
        worker.error.connect(failed)
        worker.finished.connect(finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def check(self, checked=False, silent=False):
        if self.worker or self.helper or self.owner.closing:
            return
        self.status.setText('正在检查公开发行版…')
        def found(release):
            remote, local = version_tuple(release.version), version_tuple(__version__)
            self.release = release if remote > local else None
            self.archive = None
            if remote > local:
                self.status.setText('发现新版本 v' + release.version)
                self.install_button.setText('下载更新')
                self.notes.setPlainText(release.notes or '本次发行未填写更新说明。')
                self.notes.show()
                if silent:
                    self.owner.statusBar().showMessage('发现声笺新版本 v' + release.version + '，可在设置 → 常规中更新。', 8000)
            else:
                self.status.setText('当前已是最新公开版。' if remote == local else '当前版本高于最新公开版 v' + release.version + '。')
                self.notes.hide()
        self.run_worker(UpdateWorker(), found)

    def start_download(self):
        if not self.release or not frozen() or self.worker:
            return
        if self.archive:
            self.install()
            return
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.show()
        self.status.setText('正在下载 v' + self.release.version + '…')
        worker = UpdateWorker(self.release)
        worker.progress.connect(lambda done, total: self.progress.setValue(min(100, done * 100 // max(1, total))))
        def downloaded(path):
            self.archive = path
            self.progress.setValue(100)
            self.cancel_button.hide()
            self.status.setText('完整更新包已校验，安装时会退出并重新启动声笺。')
            self.install_button.setText('安装并重启')
        self.run_worker(worker, downloaded)

    def cancel(self):
        if self.worker:
            self.worker.cancelled.set()
            self.status.setText('已取消，等待当前下载请求结束…')
            self.cancel_button.hide()
            self.progress.hide()

    def install(self):
        if not self.archive or not self.release or not frozen() or self.helper:
            return
        if self.owner.submitting:
            self.status.setText('请先完成当前生成提交，再安装更新。')
            return
        if self.owner.ready and not self.owner.owns_service:
            self.status.setText('本地服务由其他进程启动，请先退出独立服务再安装更新。')
            return
        self.install_button.setEnabled(False)
        def prepare(health):
            if health and (health.get('current') or health.get('queue') or health.get('audio_editing')):
                self.status.setText('请先完成或取消生成 / 音频编辑任务，再安装更新。')
                self.install_button.setEnabled(True)
                return
            try:
                install = project_root().resolve()
                check_data_boundary(install, self.owner.data_dir)
                if not (install / UPDATER).is_file():
                    raise RuntimeError('程序目录缺少独立更新器，请从发行页安装完整版本。')
                self.work = self.archive.parent
                for name in ('ready', 'result.json'):
                    (self.work / name).unlink(missing_ok=True)
                service_pid = self.owner.process.pid if self.owner.owns_service and self.owner.process and self.owner.process.poll() is None else 0
                cfg = configuration()
                cfg.config_path = cfg.save(self.work / 'updater-config.json')
                job = {'install': str(install), 'archive': str(self.archive.resolve()), 'sha256': self.release.sha256,
                       'version': self.release.version, 'data_dir': str(self.owner.data_dir.resolve()),
                       'port': self.owner.port, 'service_pid': service_pid}
                (self.work / 'job.json').write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
                with external_environment() as environment:
                    self.helper = launch_updater(install, self.archive, cfg, work_dir=self.work, environment=environment)
                self.watch_started = time.monotonic()
                self.status.setText('正在准备独立更新器，准备完成后退出并重启…')
                self.owner.setEnabled(False)
                self.watch.start()
            except Exception as exc:
                self.install_failed(exc)
        if self.owner.ready:
            self.owner.call(lambda c: checked(c.get('/health')), prepare, lambda message: self.install_failed(message))
        else:
            prepare(None)

    def install_failed(self, message):
        self.owner.setEnabled(True)
        self.status.setText(str(message))
        self.install_button.setEnabled(True)

    def helper_ready(self):
        if self.work and (self.work / 'ready').is_file():
            self.watch.stop()
            self.owner.close()
        elif (self.helper and self.helper.poll() is not None) or time.monotonic() - self.watch_started > 120:
            self.watch.stop()
            if self.helper and self.helper.poll() is None:
                self.helper.terminate()
            self.helper = None
            self.install_failed('独立更新器未准备完成，程序继续运行；请查看下载缓存中的 updater.log。')

    def close_updates(self):
        self.startup.stop()
        self.watch.stop()
        if self.worker:
            self.worker.cancelled.set()
