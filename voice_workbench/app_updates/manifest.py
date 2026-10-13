"""Release payload boundaries and checksums, shared with the build pipeline."""
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from .._vendor.pyupdater.apply import inspect_zip, payload_prefix, ApplyError
from .._vendor.pyupdater.config import UpdateConfig

MANIFEST = "program-files.json"
UPDATER = "声笺更新器.exe"
EXECUTABLES = ("声笺.exe", "声笺服务.exe", UPDATER)
DOCUMENTS = {"README.md", "API.md", "UI_STYLE.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "requirements-release.txt"}


def check_data_boundary(install, data):
    install, data = Path(install).resolve(), Path(data).resolve()
    if any(data.is_relative_to(install / root) for root in ('_internal', 'docs', 'licenses')):
        raise ApplyError("数据目录位于程序文件目录内，请先迁移数据目录后更新")


def digest_file(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def managed_path(value):
    path = PurePosixPath(value)
    return (value in (*EXECUTABLES, MANIFEST, *DOCUMENTS) or
            len(path.parts) > 1 and path.parts[0] in ("_internal", "docs", "licenses"))


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ApplyError("更新包路径无效")
    path = PurePosixPath(value)
    for component in value.split('/'):
        if (component in ('', '.', '..') or component.rstrip(' .') != component or
                any(ord(c) < 32 or c in ':<>"|?*' for c in component) or
                re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", component)):
            raise ApplyError("更新包包含非法 Windows 路径：" + value)
    if path.is_absolute():
        raise ApplyError("更新包路径不能是绝对路径")
    return path


def write_manifest(candidate, version):
    files = {}
    for path in sorted(candidate.rglob('*')):
        if path.is_symlink():
            raise ApplyError("打包目录不能包含符号链接：" + str(path))
        if path.is_file() and path != candidate / MANIFEST:
            relative = path.relative_to(candidate).as_posix()
            if not managed_path(relative):
                raise ApplyError("打包目录包含非程序文件：" + relative)
            files[relative] = digest_file(path)
    (candidate / MANIFEST).write_text(json.dumps({"schema": 1, "version": version, "files": files},
                                                ensure_ascii=False, indent=2), encoding="utf-8")


def validate_archive(archive, cfg, version, cancelled=None):
    names = inspect_zip(archive)
    normalized = [n.replace('\\', '/') for n in names]
    prefixes = [n[:-len(cfg.exe_name)] for n in normalized if n.rsplit('/', 1)[-1] == cfg.exe_name]
    if len(prefixes) != 1:
        raise ApplyError("更新包必须包含且只包含一个程序目录")
    prefix = payload_prefix(names, cfg)
    files = {}
    seen = set()
    with zipfile.ZipFile(archive) as zf:
        if len(zf.infolist()) > 30000 or sum(v.file_size for v in zf.infolist()) > 4 * 1024 ** 3:
            raise ApplyError("更新包展开大小或文件数超出限制")
        for info in zf.infolist():
            name = info.filename.replace('\\', '/')
            safe_path(name.rstrip('/'))
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ApplyError("更新包不能包含符号链接")
            if info.is_dir():
                continue
            if not name.startswith(prefix):
                raise ApplyError("更新包包含程序目录以外的文件")
            relative = name[len(prefix):]
            safe_path(relative)
            identity = relative.casefold()
            if identity in seen:
                raise ApplyError("更新包存在重复文件路径")
            seen.add(identity)
            if cfg.is_protected(relative.split('/')[0]):
                continue
            if not managed_path(relative):
                raise ApplyError("更新包包含非程序文件：" + relative)
            files[relative] = info.filename
        if MANIFEST not in files or zf.getinfo(files[MANIFEST]).file_size > 8 * 1024 * 1024:
            raise ApplyError("更新包缺少程序文件清单")
        manifest = json.loads(zf.read(files[MANIFEST]))
        if manifest.get('schema') != 1 or manifest.get('version') != version or not isinstance(manifest.get('files'), dict):
            raise ApplyError("更新包版本或清单不匹配")
        expected = manifest['files']
        if set(expected) != set(files) - {MANIFEST}:
            raise ApplyError("更新包文件与清单不一致")
        if not all(name in expected for name in EXECUTABLES) or not any(n.startswith('_internal/') for n in expected):
            raise ApplyError("更新包缺少主程序、服务、更新器或运行库")
        for relative, checksum in expected.items():
            if cancelled and cancelled.is_set():
                raise ApplyError("已取消更新")
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ApplyError("文件校验信息无效")
            actual = hashlib.sha256()
            with zf.open(files[relative]) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    if cancelled and cancelled.is_set():
                        raise ApplyError("已取消更新")
                    actual.update(block)
            if actual.hexdigest() != checksum:
                raise ApplyError("程序文件校验失败：" + relative)
        for name in EXECUTABLES:
            with zf.open(files[name]) as stream:
                if stream.read(2) != b'MZ':
                    raise ApplyError("更新包包含无效的 Windows 程序")
    return files


def extract_verified(archive, staging, cfg, version):
    files = validate_archive(archive, cfg, version)
    with zipfile.ZipFile(archive) as zf:
        for relative, name in files.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as source, target.open('wb') as dest:
                import shutil
                shutil.copyfileobj(source, dest, 1024 * 1024)
    return tuple(n for n in files if n.split('/')[0] != '_internal' and n not in ('声笺.exe', '声笺服务.exe'))
