"""主仆模式：把本机的程序、模型与声音资产经局域网交给另一台电脑。

本包是纯标准库实现（不导入 Qt，也不导入 torch），因此既能被桌面端调用，
也能被服务端在后台线程里驱动，还能被目标机的引导脚本以同样的语义复用。
"""

from .manifest import SCOPES, build, digest, summarize

__all__ = ["SCOPES", "build", "digest", "summarize"]
