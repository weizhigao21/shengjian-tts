"""更新配置：描述「谁在更新谁」——仓库、程序名、要保留的数据目录等。

配置文件是 JSON，默认放在**主程序 exe 同目录**的 `update_config.json`
（更新程序被复制到 %TEMP% 后运行，因此 `launch_updater` 会把该文件的
绝对路径通过 `--config` 显式传给它）。

最简配置只需要 owner/repo/exe_name 三项：

    {
      "owner": "your-name",
      "repo": "your-repo",
      "exe_name": "我的程序.exe"
    }
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "update_config.json"

# 默认要保留的目录名（大小写不敏感）。**这是本组件最重要的安全设定**：
# 更新时这些顶层目录不会被新包覆盖或删除。多数 Windows 桌面应用把配置、
# 下载内容、日志放在 exe 旁边，所以默认沿用这套常见命名；若你的程序不
# 一样，务必在配置里显式覆盖。
DEFAULT_DATA_DIRS: tuple[str, ...] = ("Config", "Download", "logs")

# 下载/查询的代理候选：先直连，失败再试本机常见代理端口（Clash 默认 7897）
DEFAULT_PROXIES: tuple[str | None, ...] = (None, "http://127.0.0.1:7897")


def app_dir() -> Path:
    """当前进程的"程序目录"：打包后为 exe 所在目录，源码运行时为项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """配置缺失或非法；消息可直接展示给用户。"""


@dataclass
class UpdateConfig:
    """一个可更新的应用所需的全部约定。"""

    owner: str                                  # GitHub 用户名/组织
    repo: str                                   # 仓库名
    exe_name: str                               # 主程序文件名（zip 里必须有它）
    app_name: str = "应用"                       # 展示名（对话框/进度窗标题）
    asset_prefix: str = ""                      # 更新包名前缀，如 "MyApp"（用于挑包）
    updater_name: str = "updater.exe"           # 安装目录里更新程序的文件名
    data_dirs: tuple[str, ...] = DEFAULT_DATA_DIRS
    proxies: tuple[str | None, ...] = DEFAULT_PROXIES
    backup_dir_name: str = ".backup-update"     # 备份目录（安装目录内，合并时跳过）
    health_check_seconds: float = 6.0           # 启动新版后观察多久判定"启动成功"
    exit_wait_seconds: float = 60.0             # 等待主程序退出的上限
    api_base: str = "https://api.github.com"    # 支持 GitHub Enterprise
    config_path: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.data_dirs = tuple(self.data_dirs)
        self.proxies = tuple(self.proxies)
        if not self.owner or not self.repo:
            raise ConfigError("配置缺少 owner（仓库所有者）或 repo（仓库名）")
        if not self.exe_name:
            raise ConfigError("配置缺少 exe_name（主程序文件名）")
        if not self.data_dirs:
            logger.warning(
                "data_dirs 为空：更新时将不会保留任何用户数据目录，请确认这是你想要的"
            )

    # ---- 派生量 ----

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def releases_api(self) -> str:
        return f"{self.api_base}/repos/{self.slug}/releases/latest"

    @property
    def releases_page(self) -> str:
        return f"https://github.com/{self.slug}/releases"

    def asset_name_for(self, version: str) -> str:
        """按约定拼出更新包文件名（前缀为空时只有版本号）。"""
        prefix = f"{self.asset_prefix}-" if self.asset_prefix else ""
        return f"{prefix}v{version}.zip"

    def is_data_dir(self, name: str) -> bool:
        """该顶层目录名是否属于"必须保留"的用户数据（大小写不敏感）。"""
        lowered = name.lower()
        return any(lowered == d.lower() for d in self.data_dirs)

    def is_protected(self, name: str) -> bool:
        """合并替换时要跳过的顶层目录名：用户数据 + 备份目录本身。"""
        return self.is_data_dir(name) or name == self.backup_dir_name

    # ---- 读写 ----

    @classmethod
    def load(cls, path: str | Path | None = None) -> UpdateConfig:
        """读取配置；`path` 为空时用当前程序目录下的 `update_config.json`。"""
        config_path = Path(path) if path else app_dir() / CONFIG_FILENAME
        if not config_path.is_file():
            raise ConfigError(
                f"找不到更新配置：{config_path}\n"
                f"请把 {CONFIG_FILENAME} 放在主程序同目录（可参考 example 文件）"
            )
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"更新配置无法解析：{exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("更新配置必须是 JSON 对象")
        unknown = set(data) - {f.name for f in cls.__dataclass_fields__.values()}
        if unknown:
            raise ConfigError(f"更新配置里有无法识别的字段：{'、'.join(sorted(unknown))}")
        for key in ("data_dirs", "proxies"):
            if key in data and not isinstance(data[key], list):
                raise ConfigError(f"更新配置的 {key} 必须是数组")
        data.pop("config_path", None)
        cfg = cls(**data)
        cfg.config_path = config_path
        return cfg

    def save(self, path: str | Path | None = None) -> Path:
        """写出配置（打包发布时可用：把 example 配置生成到 dist 目录）。"""
        target = Path(path) if path else (app_dir() / CONFIG_FILENAME)
        payload = {
            "owner": self.owner,
            "repo": self.repo,
            "exe_name": self.exe_name,
            "app_name": self.app_name,
            "asset_prefix": self.asset_prefix,
            "updater_name": self.updater_name,
            "data_dirs": list(self.data_dirs),
            "proxies": list(self.proxies),
            "health_check_seconds": self.health_check_seconds,
            "exit_wait_seconds": self.exit_wait_seconds,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return target
