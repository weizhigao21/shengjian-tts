"""Record the build environment and copy its unmodified dependency licenses."""
from importlib.metadata import distributions
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench import __version__


def main():
    destination = ROOT / 'licenses'
    destination.mkdir(exist_ok=True)
    packages = sorted(distributions(), key=lambda item: item.metadata['Name'].lower())
    requirements, rows, missing = [], [], []
    for package in packages:
        name = package.metadata['Name']
        if name == 'local-voice-workbench':
            continue
        requirements.append(f'{name}=={package.version}')
        paths = []
        for file in package.files or ():
            label = file.name.lower()
            if '.dist-info' not in str(file) or not label.startswith(('license', 'copying', 'notice')):
                continue
            origin = Path(package.locate_file(file))
            if not origin.is_file():
                continue
            folder = name.lower().replace('_', '-')
            target = destination / folder / file.name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(origin, target)
            paths.append(f'[{file.name}]({folder}/{file.name})')
        if not paths:
            missing.append(name)
        source = f'https://pypi.org/project/{name}/{package.version}/#files'
        rows.append(f'| {name} | {package.version} | {", ".join(paths) or "见来源包"} | [来源]({source}) |')
    shutil.copyfile(Path(sys.base_prefix) / 'LICENSE.txt', destination / 'Python-LICENSE.txt')
    shutil.copyfile(ROOT / 'voice_workbench/assets/YOVOICE_LICENSE.txt', destination / 'YOVOICE-LICENSE.txt')
    (ROOT / 'requirements-release.txt').write_text(
        f'# Windows x64 / CPython {sys.version.split()[0]}; build environment for v{__version__}\n' + '\n'.join(requirements) + '\n', encoding='utf-8')
    header = '''# 分发依赖与许可证

声笺源代码使用 [GPLv3](../LICENSE)。下表记录首个发行包的构建环境，
包含运行依赖与构建工具；后者不一定进入 EXE。许可文本从该版本已安装的分发包原样复制。

- 对应源代码：[声笺 v0.7.28](https://github.com/weizhigao21/shengjian-tts/tree/v0.7.28)，发行页提供源码 ZIP。
- 可复建环境：[requirements-release.txt](../requirements-release.txt)，构建入口 `打包.bat` 与 `声笺.spec`。
- CPython 3.10.11：[完整声明](Python-LICENSE.txt)、[源代码](https://www.python.org/downloads/release/python-31011/)。
- Qt 6.11.2：LGPLv3，动态链接；来源为 PyQt6-Qt6 wheel，
  [Qt 源代码](https://download.qt.io/archive/qt/6.11/6.11.2/single/)。程序未修改 Qt / PyQt；Qt DLL 位于 `_internal/PyQt6/Qt6/`。
- yovoice：仅改编的目录与情绪预设，Apache-2.0；[完整许可](YOVOICE-LICENSE.txt)，详见[第三方声明](../THIRD_PARTY_NOTICES.md)。
- 引擎和模型由用户另行下载，各自保留其上游许可。
- CPython 附带的 OpenSSL 1.1.1t：[许可](OpenSSL-LICENSE.txt)、[源代码](https://github.com/openssl/openssl/tree/OpenSSL_1_1_1t)。
- libffi：[许可](libffi-LICENSE.txt)；SQLite：[公有领域声明](https://www.sqlite.org/copyright.html)。

| 依赖 | 版本 | 包内许可 | 上游源码分发 |
| :--- | :--- | :--- | :--- |
'''
    (destination / 'README.md').write_text(header.replace('v0.7.28', f'v{__version__}') + '\n'.join(rows) + '\n', encoding='utf-8')
    print(f'已收集 {len(rows)} 个包的许可声明。无内附许可的包：{", ".join(missing) or "无"}')


if __name__ == '__main__':
    main()
