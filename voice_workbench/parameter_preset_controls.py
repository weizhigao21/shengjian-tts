"""Advanced-options presets for both the inspector and the voice-preset editor."""
import sys
from PyQt6 import sip
from PyQt6.QtWidgets import QHBoxLayout, QInputDialog, QMenu, QMessageBox, QSizePolicy, QVBoxLayout, QWidget
from .desktop.api_client import checked
from .generation_options import compatible_options
from .parameter_presets import checked_options, model_descriptor, model_key
from .schemas import VoiceConfig
from .ui import NoWheelComboBox, button, icon, label


class ParameterPresetControls(QWidget):
    def __init__(self, controls):
        super().__init__(controls)
        self.controls, self.owner = controls, None
        self.entries, self.key, self.selected = [], None, None
        self.loaded, self.attempted, self.busy, self.serial = False, False, False, 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(label('参数预设', 'muted'))
        row = QHBoxLayout()
        row.setSpacing(6)
        self.combo = NoWheelComboBox()
        self.combo.setMinimumWidth(0)
        self.combo.setMinimumContentsLength(8)
        self.combo.setSizeAdjustPolicy(NoWheelComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo.setAccessibleName('选择高级参数预设')
        self.combo.setToolTip('仅显示当前模型的参数预设；选择后应用到当前声音或对白。')
        self.save_button = button('保存', self.save_new)
        self.save_button.setFixedWidth(60)
        self.save_button.setToolTip('把当前高级参数另存为新的参数预设')
        self.manage_button = button('', self.manage, 'settings', 'icon')
        self.manage_button.setFixedWidth(28)
        self.manage_button.setToolTip('管理参数预设：更新、重命名、删除和刷新')
        row.addWidget(self.combo, 1)
        row.addWidget(self.save_button)
        row.addWidget(self.manage_button)
        layout.addLayout(row)
        self.hint = label('只保存高级生成参数，不改变音色与参考声音。', 'hint')
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.combo.activated.connect(self.choose)
        controls.generation_controls.changed.connect(self.mark_changed)
        controls.advanced_toggle.toggled.connect(self.opened)
        self.hide()

    def set_owner(self, owner):
        self.owner = owner
        self.bind()

    def source(self):
        voice = {'id': 'parameter-model', 'python': sys.executable, 'name': '当前模型',
                 **self.controls.voice, **self.controls.overrides()}
        return VoiceConfig(**voice).model_dump()

    def bind(self):
        visible = bool(self.controls.info and self.controls.generation_controls.specs)
        self.setVisible(visible)
        if not visible:
            self.key, self.selected = None, None
            self.bound_state = None
            return
        key = model_key(model_descriptor(self.source(), self.controls.info))
        if key != self.key:
            self.key, self.selected = key, None
        state = (self.key, bool(self.owner and self.owner.ready), self.loaded, self.busy)
        if state != getattr(self, 'bound_state', None):
            self.render()
        else:
            self.mark_changed()
        if self.owner and not self.owner.ready:
            self.attempted = False
        elif self.owner and not self.loaded and not self.attempted and not self.busy:
            self.refresh()

    def opened(self, checked):
        if checked and self.loaded:
            self.refresh()

    def alive(self):
        return not sip.isdeleted(self) and self.owner and not self.owner.closing

    def refresh(self):
        if not self.owner or not self.owner.ready or self.busy:
            return
        self.attempted = True
        self.serial += 1
        serial = self.serial
        def fetch(client):
            response = client.get('/parameter-presets')
            if response.status_code == 404:
                raise RuntimeError('当前服务不支持参数预设，请退出旧声笺及其服务后重启。')
            return checked(response)
        def done(entries):
            if self.alive() and serial == self.serial:
                self.entries, self.loaded = entries, True
                self.hint.setText('只保存高级生成参数，不改变音色与参考声音。')
                self.render()
        def failed(message):
            if self.alive() and serial == self.serial:
                self.hint.setText(str(message))
                self.render()
        self.owner.call(fetch, done, failed)

    def render(self):
        self.bound_state = (self.key, bool(self.owner and self.owner.ready), self.loaded, self.busy)
        selected = self.selected
        items = sorted((p for p in self.entries if p['model_key'] == self.key), key=lambda p: p['updated'], reverse=True)
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem('当前参数（未选择预设）', None)
        for item in items:
            self.combo.addItem(item['name'], item['id'])
        index = self.combo.findData(selected)
        self.combo.setCurrentIndex(max(0, index))
        if index < 0:
            self.selected = None
        self.combo.blockSignals(False)
        ready = bool(self.owner and self.owner.ready and self.loaded and self.key and not self.busy)
        self.combo.setEnabled(ready)
        self.save_button.setEnabled(ready)
        self.manage_button.setEnabled(bool(self.owner and self.owner.ready and not self.busy))
        self.mark_changed()

    def current(self):
        return next((p for p in self.entries if p['id'] == self.selected and p['model_key'] == self.key), None)

    def values(self):
        values = self.controls.generation_controls.values()
        return {s['name']: values[s['name']] for s in self.controls.generation_controls.specs}

    def mark_changed(self):
        if self.current() and self.combo.currentIndex() > 0:
            item = self.current()
            options, _ = compatible_options({**self.controls.voice, 'request_options': item['request_options']}, self.controls.info)
            expected = {s['name']: options.get(s['name'], s['default']) for s in self.controls.generation_controls.specs}
            adjusted = expected != self.values()
            self.combo.setItemText(self.combo.currentIndex(), item['name'] + (' · 已调整' if adjusted else ''))

    def choose(self, index):
        identity = self.combo.itemData(index)
        if identity is None:
            self.selected = None
            return
        item = next((p for p in self.entries if p['id'] == identity and p['model_key'] == self.key), None)
        if item is None:
            self.render()
            return
        try:
            values, notes = checked_options(self.source(), self.controls.info, item['request_options'])
        except Exception as exc:
            self.render()
            self.owner.show_error(str(exc))
            return
        self.selected = identity
        self.controls.generation_controls.bind(self.controls.info['generation_options'], values)
        self.controls.generation_controls.changed.emit()
        self.hint.setText('已应用参数预设。' + (' '.join(notes) if notes else ''))
        self.render()

    def ask_name(self, title, current=''):
        name, accepted = QInputDialog.getText(self, title, '参数预设名称（最多 100 字）：', text=current)
        if not accepted:
            return None
        name = name.strip()
        if not 1 <= len(name) <= 100:
            self.owner.show_error('请填写 1–100 字的参数预设名称。')
            return None
        return name

    def save_new(self):
        if not self.save_button.isEnabled():
            return
        name = self.ask_name('保存当前高级参数')
        if name:
            self.save(name)

    def save(self, name, identity=None):
        try:
            source = self.source()
            values, _ = checked_options(source, self.controls.info, self.values())
        except Exception as exc:
            self.owner.show_error(str(exc))
            return
        key = self.key
        body = {'name': name, 'model': source, 'request_options': values}
        def done(record):
            self.entries = [p for p in self.entries if p['id'] != record['id']] + [record]
            if self.key == key:
                self.selected = record['id']
            self.hint.setText('参数预设已保存。')
        self.mutate(lambda c: checked(c.put('/parameter-presets/' + identity, json=body) if identity else
                                      c.post('/parameter-presets', json=body)), done)

    def mutate(self, action, success):
        if self.busy or not self.owner.ready:
            return
        self.busy = True
        self.serial += 1  # Ignore a list fetched before this mutation.
        self.render()
        def done(value):
            if self.alive():
                self.busy = False
                success(value)
                self.render()
        def failed(message):
            if self.alive():
                self.busy = False
                self.render()
                self.owner.show_error(str(message))
        self.owner.call(action, done, failed)

    def manage(self):
        item = self.current()
        menu = QMenu(self)
        update = menu.addAction(icon('check'), '更新所选预设')
        rename = menu.addAction(icon('pen'), '重命名…')
        delete = menu.addAction(icon('trash'), '删除预设…')
        for action in (update, rename, delete):
            action.setEnabled(item is not None)
        menu.addSeparator()
        refresh = menu.addAction(icon('retry'), '刷新预设列表')
        chosen = menu.exec(self.manage_button.mapToGlobal(self.manage_button.rect().bottomLeft()))
        menu.deleteLater()
        if chosen is refresh:
            self.refresh()
        elif item and self.current() and self.current()['id'] == item['id']:
            if chosen is update:
                self.save(item['name'], item['id'])
            elif chosen is rename:
                name = self.ask_name('重命名参数预设', item['name'])
                if name:
                    self.rename(item, name)
            elif chosen is delete:
                self.delete(item)

    def rename(self, item, name):
        def done(record):
            self.entries = [record if p['id'] == item['id'] else p for p in self.entries]
        self.mutate(lambda c: checked(c.patch('/parameter-presets/' + item['id'], json={'name': name})), done)

    def delete(self, item):
        answer = QMessageBox.question(self, '删除参数预设', '删除「' + item['name'] + '」？\n当前已应用的参数与历史音频会保留。',
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            def done(value):
                self.entries = [p for p in self.entries if p['id'] != value['deleted']]
            self.mutate(lambda c: checked(c.delete('/parameter-presets/' + item['id'])), done)
