"""Give legacy Windows native loaders ASCII paths without copying model weights."""
import os
from pathlib import Path
import tempfile

from .errors import VoiceError


class NativePaths:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="shengjian-native-"))
        self.roots = [self.root]
        self.directories = []
        self.files = []
        if not str(self.root).isascii():
            self.root.rmdir()
            raise VoiceError("NATIVE_PATH_ENCODING", "系统临时目录含非 ASCII 字符",
                             "设置 TMP 为可写的英文路径后重启程序。", "模型路径准备")

    def model(self, path):
        source = Path(path).absolute()
        if os.name != "nt" or str(source).isascii():
            return str(source)
        try:
            folder = self.root
            filename = source.name if source.name.isascii() else "model.gguf"
            alias = folder / filename
            try:
                os.link(source, alias)
            except OSError:
                # Hard links must be on the model's volume. Directory junctions
                # cannot help: this engine canonicalizes them back to the Unicode path.
                for ancestor in source.parents:
                    if not str(ancestor).isascii():
                        continue
                    candidate = None
                    try:
                        candidate = Path(tempfile.mkdtemp(prefix="shengjian-native-", dir=ancestor))
                        alias = candidate / filename
                        os.link(source, alias)
                        folder = candidate
                        self.roots.append(candidate)
                        break
                    except OSError:
                        if candidate:
                            candidate.rmdir()
                else:
                    raise
            self.files.append(alias)
            # Legacy GGUF packages may refer to sidecars relative to the model.
            for name in ("config.json", "config.yaml", "voices.json", "vocab.json", "merges.txt", "tokenizer.json",
                         "tokenizer_config.json", "generation_config.json", "speech_tokenizer/config.json",
                         "speech_tokenizer/model.gguf", "speech_tokenizer/model.safetensors"):
                original = source.parent / name
                if original.is_file():
                    target = folder / name
                    if not target.parent.is_dir():
                        target.parent.mkdir()
                        self.directories.append(target.parent)
                    os.link(original, target)
                    self.files.append(target)
            return str(alias)
        except OSError as exc:
            raise VoiceError("NATIVE_PATH_ENCODING", f"无法准备模型兼容路径：{exc}",
                             "检查临时目录权限和磁盘是否支持硬链接，或将模型放在英文路径中。", "模型路径准备") from exc

    def config(self, value):
        from .config import atomic_json
        path = self.root / "engine-server.json"
        self.files.append(path)
        atomic_json(path, value)
        return path

    def close(self):
        # Unlink only our files. Never recursively traverse or delete a model directory.
        for file in self.files:
            file.unlink(missing_ok=True)
        self.files.clear()
        for directory in reversed(self.directories):
            directory.rmdir()
        self.directories.clear()
        for root in self.roots:
            root.rmdir()
        self.roots.clear()
