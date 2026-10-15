# 程序更新

v0.7.33 起，Windows 完整包提供独立的 `声笺更新器.exe`。在「设置 → 常规」检查、下载和安装正式发行版本。

![设置页的程序更新入口](images/app-updates.png)

截图使用模拟的新版本说明展示更新入口；实际版本以 GitHub 公开发行页为准。

## 使用

1. 打开「设置 → 常规」，点击「检查更新」。打包版启动后也会检查一次，只提示，不自动下载或安装。
2. 发现较新的正式版本后，查看说明，点击「下载更新」。下载有进度和取消操作。
3. 下载后验证完整包的大小、SHA-256、版本和内部文件清单。通过后点击「安装并重启」。
4. 程序保存草稿并关闭本地服务，独立更新器等待进程退出，然后覆盖程序文件并使用原来的数据目录与端口重新启动。

文章、对话或音频编辑仍在生成时，先完成或取消任务。独立启动的服务需要自行退出后更新。更新器准备期间暂时禁用操作，准备失败则恢复界面。

源码运行可以检查发行版本、打开发行页；自动安装只用于 Windows 打包版。v0.7.28 等旧包没有更新器，需要先手动安装含更新器的完整包，之后才能使用这个功能。

## 文件保留规则

- 直接覆盖主程序、服务、更新器、`_internal` 运行库以及发行文档；移除旧运行库中不再需要的文件。
- 用户目录和程序旁的 `data / Config / Download / logs / models / voices / references / audio / audio-editor` 不被更新包覆盖；其他未列入程序清单的文件同样保留。
- 不创建旧程序备份，不自动回滚。若写入失败或新版启动后立即退出，从发行页重新解压完整包到原程序目录即可修复，保留原有数据目录。
- 自定义数据目录若放在 `_internal / docs / licenses` 内，会阻止更新；先迁移到独立数据目录。
- ZIP 下载和更新器副本使用系统临时目录。成功后删除下载 ZIP；失败日志为该临时目录下的 `updater.log`，结果为 `result.json`。

## 发布完整包

运行 `打包.bat`，同时构建 GUI / 服务和独立的一文件更新器。构建目录内生成 `program-files.json`，记录版本与每个程序文件的 SHA-256，随后输出：

```text
dist/shengjian-v0.7.39-windows-x64.zip
dist/SHA256SUMS.txt
dist/声笺/声笺.exe
dist/声笺/声笺服务.exe
dist/声笺/声笺更新器.exe
dist/声笺/program-files.json
```

正式发布时使用 `v主版本.次版本.修订号` 标签，将同版本 Windows ZIP 和 `SHA256SUMS.txt` 上传到 `weizhigao21/shengjian-tts` 的 GitHub Release。不要把源码 ZIP、其他架构或调试包命名为 Windows 完整包。预发行和草稿不会被当作正式更新。

更新检查读取 `/releases/latest`。只选择名称完全匹配的 `shengjian-v{版本}-windows-x64.zip`，优先使用 GitHub 提供的 SHA-256；没有摘要时读取同一发行中的 `SHA256SUMS.txt`。没有有效校验信息时拒绝自动安装。直连失败后尝试本机 `127.0.0.1:7897` 代理；检查失败不会显示「已是最新版」。

## 实现

- `app_updates/client.py`：公开版本解析、下载、进度与 SHA-256。
- `app_updates/manifest.py`：发行清单、ZIP 路径与文件校验、用户目录隔离。
- `app_updates/apply.py`：独立进程等待、覆盖、重启与结果记录。
- `desktop/settings/app_updates.py`：Qt 后台检查与设置页操作。
- `program_files.py`：打包与更新共用的程序文件覆盖策略。
- `_vendor/pyupdater/`：用户提供的 `G:/code/py-updater` 部分配置 / 进程 / 进度窗代码，改编来源见该目录的 `SOURCE.md` 和第三方声明。

独立更新器不依赖 PyQt6、HTTP 服务或安装机器上的 Python。它先验证下载包再通知主界面退出，随后等待 GUI 与服务均退出，预检已有程序文件的占用状态，再开始复制。
