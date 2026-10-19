"""Explicit domestic and official download plans for the isolated Qwen runtime."""
from typing import Literal

RuntimeSource = Literal['mirror', 'official']
RUNTIME_SOURCES = {'mirror': '国内加速（推荐）', 'official': '官方直连'}
MODEL_SOURCES = {'modelscope': 'ModelScope 魔搭（国内）', 'mirror': 'HF 镜像', 'huggingface': 'Hugging Face'}
TUNA = 'https://pypi.tuna.tsinghua.edu.cn/simple'
ALIYUN = 'https://mirrors.aliyun.com/pypi/simple/'
PYPI = 'https://pypi.org/simple'


def pip_plans(prefix, packages, source, device=None):
    """Keep CUDA wheels explicit while general dependencies use the chosen PyPI."""
    common = ['--timeout', '20', '--retries', '2']
    if not device:
        indexes = [('清华 PyPI 镜像', TUNA), ('阿里云 PyPI 镜像', ALIYUN)] if source == 'mirror' else []
        indexes.append(('PyPI 官方', PYPI))
        return [(name, prefix + common + packages + ['--index-url', index]) for name, index in indexes]
    variant = 'cu128' if device == 'cuda' else 'cpu'
    wheels = [f'torch==2.9.0+{variant}', f'torchaudio==2.9.0+{variant}']
    plans = []
    if source == 'mirror':
        plans.append(('上海交大 PyTorch / 清华 PyPI', prefix + common + wheels +
            ['--index-url', f'https://mirror.sjtu.edu.cn/pytorch-wheels/{variant}/', '--extra-index-url', TUNA]))
        # Aliyun's root contains direct wheel links; it has no /torch/ simple index.
        plans.append(('阿里云 PyTorch / PyPI', prefix + common + wheels +
            ['--index-url', ALIYUN, '--find-links', f'https://mirrors.aliyun.com/pytorch-wheels/{variant}/']))
    plans.append(('PyTorch / PyPI 官方', prefix + common + wheels +
        ['--index-url', f'https://download.pytorch.org/whl/{variant}', '--extra-index-url', PYPI]))
    return plans


def model_url(entry, file, source):
    if source == 'modelscope':
        # ModelScope uses its own revision namespace. Accept only files matching
        # the immutable HF catalog's sizes and SHA-256, regardless of branch drift.
        return f"https://modelscope.cn/models/{entry['repo']}/resolve/master/{file['path']}"
    host = 'https://hf-mirror.com' if source == 'mirror' else 'https://huggingface.co'
    return f"{host}/{entry['repo']}/resolve/{entry['revision']}/{file['path']}"
