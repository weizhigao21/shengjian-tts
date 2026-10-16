"""Opt-in isolated Qwen runtime/model setup. No changes unless --install is passed."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def commands(args):
    target = args.target.resolve()
    python = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if (target == Path(target.anchor) or target == Path(sys.prefix).resolve()
            or any(p.name == '_internal' for p in (target, *target.parents))):
        raise ValueError('请选择新的独立环境目录，不能覆盖当前 Python 环境或磁盘根目录。')
    if target.exists() and any(target.iterdir()) and not (target/'pyvenv.cfg').is_file():
        raise ValueError('目标目录已包含其他文件，请选择新的独立环境目录。')
    index = 'https://download.pytorch.org/whl/' + ('cu128' if args.device == 'cuda' else 'cpu')
    result = []
    if not python.is_file():
        result.append([sys.executable, '-m', 'venv', str(target)])
    result.extend([[str(python), '-m', 'pip', 'install', '--upgrade', 'pip'],
                   [str(python), '-m', 'pip', 'install', 'torch==2.9.0', 'torchaudio==2.9.0', '--index-url', index],
                   [str(python), '-m', 'pip', 'install', 'qwen-tts==0.1.1']])
    if args.download_model:
        if not args.model_dir:
            raise ValueError('--download-model 需要同时指定 --model-dir。')
        repository = 'Qwen/Qwen3-TTS-12Hz-' + args.download_model + '-Base'
        code = ('from huggingface_hub import HfApi,snapshot_download; import sys,json; from pathlib import Path; '
                'revision=HfApi().model_info(sys.argv[1]).sha; '
                'snapshot_download(repo_id=sys.argv[1],revision=revision,local_dir=sys.argv[2]); '
                'Path(sys.argv[2],"shengjian-source.json").write_text(json.dumps({"repo":sys.argv[1],"revision":revision}),encoding="utf-8")')
        result.append([str(python), '-c', code, repository, str(args.model_dir.resolve())])
    return python, result


def main():
    parser = argparse.ArgumentParser(description='按需安装独立 Qwen 批处理环境及 Base 模型。默认只显示计划。')
    parser.add_argument('--target', type=Path, required=True, help='专用虚拟环境目录')
    parser.add_argument('--device', choices=('cuda', 'cpu'), default='cuda')
    parser.add_argument('--download-model', choices=('0.6B', '1.7B'))
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    python, plan = commands(args)
    print(json.dumps({'python': str(python), 'model_dir': str(args.model_dir or ''),
                      'install': args.install, 'commands': plan}, ensure_ascii=False, indent=2), flush=True)
    if not args.install:
        return
    for command in plan:
        subprocess.run(command, check=True)
    check = ('import torch,qwen_tts; print("CUDA:",torch.cuda.is_available()); '
             'assert torch.cuda.is_available()' if args.device == 'cuda' else 'import torch,qwen_tts; print("CPU ready")')
    subprocess.run([str(python), '-c', check], check=True)
    print('安装完成。在声笺 设置 → 批量性能 中选择上述 Python、Base 模型目录及对应 GGUF，再点击「检查并应用」。')


if __name__ == '__main__':
    main()
