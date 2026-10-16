"""Bounded, local-only discovery of stock Qwen Base pairs and managed Python."""
import json
import os
import struct
from pathlib import Path

from ..model_info import _read_package


def catalog():
    return json.loads((Path(__file__).parent.parent / 'assets/qwen_batch_catalog.json').read_text(encoding='utf-8'))


def base_size(info):
    if not isinstance(info, dict):
        return None
    if info.get('tts_model_type') != 'base':
        return None
    talker = info.get('talker_config', {})
    if not isinstance(talker, dict):
        return None
    if talker.get('num_hidden_layers') != 28 or talker.get('num_attention_heads') != 16:
        return None
    return {1024: '0.6B', 2048: '1.7B'}.get(talker.get('hidden_size'))


def source_size(path):
    try:
        path = Path(path)
        stat = path.stat()
        package = _read_package(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        return base_size(package.get('files', {}).get('config.json', {})) if package.get('family') == 'qwen3_tts' else None
    except (OSError, ValueError, TypeError, KeyError, struct.error):
        return None


def sources(manager):
    result = []
    for model in manager.models.installed():
        if model.get('family') == 'qwen3_tts' and model.get('exists') and (size := source_size(model['path'])):
            result.append({'path': model['path'], 'name': model['name'], 'size': size})
    return result


def python_path(folder):
    return Path(folder) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def model_path(root, size):
    return Path(root) / 'models/qwen-batch' / size / catalog()['models'][size]['revision']


def complete_model(path, size):
    try:
        root = Path(path)
        info = json.loads((root / 'config.json').read_text(encoding='utf-8'))
        return (isinstance(info, dict) and info.get('model_type') == 'qwen3_tts' and base_size(info) == size
                and any(root.glob('*.safetensors')) and any((root / 'speech_tokenizer').glob('*.safetensors')))
    except (OSError, ValueError, TypeError):
        return False


def discover(backend, source='', device=None):
    """No PATH/registry execution, disk-wide scan, network or weight loading."""
    settings = backend.settings
    available = sources(backend.manager)
    selected = next((m for m in available if m['path'] == (source or settings.source_model)), None)
    if not source and selected is None:
        selected = next(iter(available), None)
    device = device or settings.device
    interpreters = [settings.python if device == settings.device else '',
                    str(python_path(backend.manager.root / 'runtime/qwen-batch' / ('env-' + device)))]
    python = next((str(Path(p).resolve()) for p in interpreters if p and Path(p).is_file()
                   and Path(p).name.lower() not in ('声笺.exe', '声笺服务.exe')), '')
    if selected is None:
        return settings.model_copy(update={'device': device, 'python': python}), available, None
    size = selected['size']
    model = catalog()['models'][size]
    hub = Path(os.environ.get('HF_HUB_CACHE') or Path(os.environ.get('HF_HOME') or Path.home() / '.cache/huggingface') / 'hub')
    cache = hub / ('models--' + model['repo'].replace('/', '--')) / 'snapshots'
    managed = model_path(backend.manager.root, size)
    candidates = [settings.model_dir, managed,
                  Path(selected['path']).parent / model['repo'].split('/')[-1]]
    if cache.is_dir():
        try:
            candidates += sorted(cache.iterdir(), reverse=True)[:20]
        except OSError:
            pass
    directory = next((str(Path(p).resolve()) for p in candidates if p and complete_model(p, size)
                      and (Path(p).resolve() != managed.resolve() or (managed / 'shengjian-source.json').is_file())), '')
    return settings.model_copy(update={'python': python, 'model_dir': directory,
                                      'source_model': selected['path'], 'device': device}), available, selected
