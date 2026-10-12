"""Inspect Windows engine binaries and dependencies without starting inference."""
from functools import lru_cache
import os
from pathlib import Path
import struct

CUDA_DLLS = ("cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll", "cufft64_11.dll")


def clear_binary_cache():
    _imports.cache_clear()


@lru_cache(maxsize=256)
def _imports(path, size, modified):
    with Path(path).open("rb") as stream:
        def read(offset, count):
            if offset < 0 or offset + count > size or count > 65536:
                raise ValueError("程序文件已损坏或不完整")
            stream.seek(offset)
            value = stream.read(count)
            if len(value) != count:
                raise ValueError("程序文件读取不完整")
            return value
        dos = read(0, 64)
        if dos[:2] != b"MZ":
            raise ValueError("不是有效的 Windows 程序")
        pe = struct.unpack_from("<I", dos, 60)[0]
        header = read(pe, 24)
        if header[:4] != b"PE\0\0" or struct.unpack_from("<H", header, 4)[0] != 0x8664:
            raise ValueError("需要 Windows x64 程序")
        sections, optional_size = struct.unpack_from("<H", header, 6)[0], struct.unpack_from("<H", header, 20)[0]
        if not 0 < sections <= 96 or optional_size < 128:
            raise ValueError("程序头信息无效")
        optional = read(pe + 24, optional_size)
        if struct.unpack_from("<H", optional)[0] != 0x20b:
            raise ValueError("需要 Windows x64 程序")
        table = read(pe + 24 + optional_size, sections * 40)
        def offset(rva):
            for pos in range(0, len(table), 40):
                virtual_size, start, raw_size, raw_start = struct.unpack_from("<IIII", table, pos + 8)
                if start <= rva < start + max(virtual_size, raw_size) and rva - start < raw_size:
                    return raw_start + rva - start
            raise ValueError("程序依赖表无效")
        imports, import_size = struct.unpack_from("<II", optional, 120)
        if not imports:
            return ()
        names = []
        for index in range(min(import_size // 20, 512)):
            record = read(offset(imports + index * 20), 20)
            if not any(record):
                return tuple(names)
            name_offset = offset(struct.unpack_from("<I", record, 12)[0])
            name = read(name_offset, min(256, size - name_offset)).split(b"\0", 1)[0].decode("ascii")
            if not name or "/" in name or "\\" in name or ":" in name:
                raise ValueError("程序依赖名称无效")
            names.append(name)
        raise ValueError("程序依赖表过长或不完整")


def inspect_engine(path, backend, system_dir=None):
    """Return independently validated program/runtime components for a directory."""
    executable = Path(path or ".")
    folder = executable.parent
    try:
        files = {p.name.lower(): p for p in folder.iterdir() if p.is_file()} if executable.is_file() else {}
    except OSError:
        files = {}
    system = Path(system_dir) if system_dir else Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"

    def validate(roots, separate_cuda=False):
        issues, checked = [], set()
        def visit(name):
            key = name.lower()
            if key in checked:
                return
            checked.add(key)
            if separate_cuda and key in {name.lower() for name in CUDA_DLLS}:
                return
            binary = files.get(key)
            if binary is None:
                # Windows API-set contracts are resolved by the OS loader.
                if key.startswith(("api-ms-", "ext-ms-")) or (system / name).is_file():
                    return
                issues.append("缺少 " + name)
                return
            try:
                stat = binary.stat()
                for dependency in _imports(str(binary), stat.st_size, stat.st_mtime_ns):
                    visit(dependency)
            except (OSError, ValueError, UnicodeError, struct.error) as exc:
                issues.append(f"{binary.name}：{exc}")
        for name in roots:
            # Required engine/CUDA components must be local, not a same-named system DLL.
            if name.lower() not in files:
                issues.append("缺少 " + name)
            else:
                visit(name)
        return {"valid": not issues, "issues": list(dict.fromkeys(issues))}

    cpu = next((p.name for key, p in files.items() if key == "ggml-cpu.dll" or key.startswith("ggml-cpu-")), "ggml-cpu.dll")
    program = validate([executable.name, "ggml.dll", "ggml-base.dll", cpu] + (["ggml-cuda.dll"] if backend == "cuda" else []), separate_cuda=backend == "cuda")
    runtime = validate(list(CUDA_DLLS)) if backend == "cuda" else {"valid": program["valid"], "issues": program["issues"]}
    return {"path": str(executable), "backend": backend, "valid": program["valid"] and runtime["valid"],
            "components": [program, runtime] if backend == "cuda" else [program],
            "issues": list(dict.fromkeys(program["issues"] + runtime["issues"]))}
