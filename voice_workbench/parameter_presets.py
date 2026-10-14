"""Named generation options, scoped to a model and validated against its contract."""
import hashlib
import json
import os
import time
import uuid

from .config import atomic_json
from .errors import VoiceError
from .generation_options import compatible_options, validate_options
from .model_info import model_ui


def model_descriptor(voice, info):
    return {'backend': voice['backend'], 'family': info['family'],
            'model_dir': os.path.abspath(voice.get('model_dir') or '.'),
            'index_version': voice.get('index_version') if voice['backend'] == 'index_tts' else None,
            'variant': info.get('variant')}


def model_key(model):
    # Compute after reading so data-directory migration can rebase model paths.
    value = {**model, 'model_dir': os.path.normcase(os.path.abspath(model['model_dir']))}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def checked_options(voice, info, values):
    options, notes = compatible_options({**voice, 'request_options': values}, info)
    validate_options({**voice, 'request_options': options}, info)
    if not options:
        raise VoiceError('EMPTY_PARAMETER_PRESET', '当前模型没有可保存的高级生成参数', stage='参数预设')
    return options, notes


class ParameterPresets:
    def __init__(self, config):
        self.config = config

    def listing(self):
        return [dict(p, model_key=model_key(p['model'])) for p in self.config.read().get('parameter_presets', [])]

    @staticmethod
    def name(value):
        value = value.strip()
        if not 1 <= len(value) <= 100:
            raise VoiceError('INVALID_PARAMETER_PRESET_NAME', '请填写 1–100 字的参数预设名称', stage='参数预设')
        return value

    def save(self, name, voice, values, identity=None):
        name = self.name(name)
        info = model_ui(voice)
        model = model_descriptor(voice, info)
        key = model_key(model)
        options, _ = checked_options(voice, info, values)
        with self.config.lock:
            data = self.config.read()
            items = data.get('parameter_presets', [])
            previous = next((p for p in items if p['id'] == identity), None)
            if identity and previous is None:
                raise VoiceError('PARAMETER_PRESET_NOT_FOUND', '参数预设不存在', stage='参数预设')
            if previous and model_key(previous['model']) != key:
                raise VoiceError('PARAMETER_MODEL_MISMATCH', '不能用其他模型覆盖此参数预设', stage='参数预设')
            self.unique(items, key, name, identity)
            if not previous and len(items) >= 1024:
                raise VoiceError('PARAMETER_PRESET_LIMIT', '参数预设已达上限，请先移除不再使用的预设', stage='参数预设')
            now = time.time()
            record = {'id': identity or 'param-' + uuid.uuid4().hex, 'name': name, 'model': model,
                      'request_options': options, 'created': previous['created'] if previous else now, 'updated': now}
            atomic_json(self.config.path, {**data, 'parameter_presets': [p for p in items if p['id'] != record['id']] + [record]})
        return dict(record, model_key=key)

    @staticmethod
    def unique(items, key, name, identity):
        if any(p['id'] != identity and model_key(p['model']) == key and p['name'].casefold() == name.casefold() for p in items):
            raise VoiceError('PARAMETER_PRESET_NAME_EXISTS', '当前模型已有同名参数预设，请使用“更新所选预设”或另取名称', stage='参数预设')

    def rename(self, identity, name):
        name = self.name(name)
        with self.config.lock:
            data = self.config.read()
            items = data.get('parameter_presets', [])
            previous = next((p for p in items if p['id'] == identity), None)
            if previous is None:
                raise VoiceError('PARAMETER_PRESET_NOT_FOUND', '参数预设不存在', stage='参数预设')
            self.unique(items, model_key(previous['model']), name, identity)
            record = dict(previous, name=name, updated=time.time())
            atomic_json(self.config.path, {**data, 'parameter_presets': [record if p['id'] == identity else p for p in items]})
        return dict(record, model_key=model_key(record['model']))

    def delete(self, identity):
        with self.config.lock:
            data = self.config.read()
            items = data.get('parameter_presets', [])
            if not any(p['id'] == identity for p in items):
                raise VoiceError('PARAMETER_PRESET_NOT_FOUND', '参数预设不存在', stage='参数预设')
            atomic_json(self.config.path, {**data, 'parameter_presets': [p for p in items if p['id'] != identity]})

