"""Minimal PE64 metadata fixtures; never executed."""
import struct


def pe_binary(imports=()):
    data = bytearray(2048)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<HH", data, 132, 0x8664, 1)
    struct.pack_into("<H", data, 148, 240)
    struct.pack_into("<H", data, 152, 0x20b)
    struct.pack_into("<I", data, 152 + 108, 16)
    struct.pack_into("<IIII", data, 392 + 8, 1536, 4096, 1536, 512)
    if imports:
        struct.pack_into("<II", data, 152 + 120, 4096, (len(imports) + 1) * 20)
        name_pos = 1024
        for index, name in enumerate(imports):
            struct.pack_into("<I", data, 512 + index * 20 + 12, name_pos - 512 + 4096)
            raw = name.encode("ascii") + b"\0"
            data[name_pos:name_pos + len(raw)] = raw
            name_pos += len(raw)
    return bytes(data)


def engine_files(cuda=False):
    files = {"bin/audiocpp_server.exe": pe_binary(["ggml.dll"]), "bin/ggml.dll": pe_binary(["ggml-base.dll"]),
             "bin/ggml-base.dll": pe_binary(), "bin/ggml-cpu-x64.dll": pe_binary(), "LICENSE": b"Apache-2.0"}
    if cuda:
        files["bin/ggml-cuda.dll"] = pe_binary(["cublas64_12.dll", "cudart64_12.dll"])
    return files
