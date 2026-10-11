from __future__ import annotations


class VoiceError(Exception):
    def __init__(self, code: str, message: str, hint: str = "", stage: str = "生成"):
        super().__init__(message)
        self.code, self.message, self.hint, self.stage = code, message, hint, stage

    def as_dict(self):
        return {"code": self.code, "message": self.message, "hint": self.hint, "stage": self.stage}


def describe_error(exc: Exception, stage: str = "生成") -> dict:
    if isinstance(exc, VoiceError):
        return exc.as_dict()
    message = str(exc) or type(exc).__name__
    lower = message.lower()
    if "windows speech voice not installed" in lower:
        return VoiceError("SYSTEM_VOICE_MISSING", "Windows 未安装所选语言的系统声音", "安装对应的 Windows 语音包，或选择已配置的统一引擎模型。", stage).as_dict()
    if "out of memory" in lower or "cuda error: memory" in lower:
        return VoiceError("GPU_OUT_OF_MEMORY", "GPU 显存不足", "关闭其他 GPU 任务，或减小文本长度后重试。", stage).as_dict()
    if "no module named" in lower or isinstance(exc, ImportError):
        return VoiceError("DEPENDENCY_MISSING", message, "检查所选模型的 Python 环境及依赖。", stage).as_dict()
    if isinstance(exc, FileNotFoundError):
        return VoiceError("FILE_MISSING", message, "检查提示中的文件路径。", stage).as_dict()
    return VoiceError("UNKNOWN", message, "原因尚未确定；打开该片段的完整日志查看原始异常。", stage).as_dict()
