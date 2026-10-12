"""One inference service per data directory, shared by all entry points."""
from pathlib import Path
import os


class ServiceLock:
    """同一数据目录只允许一个推理服务，避免双重恢复和重复抢占任务。"""
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.file = (root / "service.lock").open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                raise RuntimeError("此数据目录已有语音服务运行。请连接现有服务，或先停止它。")
        else:
            import fcntl
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise RuntimeError("此数据目录已有语音服务运行")

    def close(self):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()
