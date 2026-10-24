# 测试

`tests/` 覆盖服务、模型、任务、播放、界面、音频编辑、语音识别、文件管理与打包，当前基线为 **824 passed, 1 skipped**（2026-10-23）。

## 运行

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
```

界面用例需要离屏后端，否则在没有桌面会话时会因为没有显示设备而失败：

```powershell
$env:QT_QPA_PLATFORM="offscreen"
python -m pytest -q
```

单独跑某个文件、某个用例与平时一致：`python -m pytest tests/test_workflow_retention.py -q -k acknowledges`。

## 临时目录：为什么根目录有一个 `conftest.py`

所有 `tmp_path` / `tmpdir` 夹具都落在 `%LOCALAPPDATA%\Temp\pytest-of-<用户>\pytest-<编号>\` 下，每次运行新建一个编号目录。

pytest 原本只保留最近三轮：开新一轮时把最老的一轮改名成 `garbage-<uuid>` 再删除。**在 Windows 上这一步经常静默失败**——只要还有 sqlite 连接、Qt 组件、`audiocpp_server.exe` 子进程或杀毒软件的过滤驱动持有句柄，`shutil.rmtree` 就删不掉，pytest 记一笔就过去了，于是墓碑目录一直留着，下一轮再堆一层。

实测累积结果：620 轮之后 `%TEMP%` 里留下 **35,369 个文件 / 609 MB**，其中 92 个是删不掉的 `garbage-*` 墓碑，占 85%。全部文件里只读属性为 0 个，所以这不是权限问题，是句柄占用。

这不只是占盘。Windows 的用户配置文件服务（ProfSvc）在每次登录时会遍历整个 `%TEMP%` 目录树，每个文件要经过 open → 查询 → 写回 → 关闭约 3.4 次。这台机器的文件系统过滤链较长（FLTMGR + 火绒 `sysdiag` + `fileinfo` + `luafv` + `Wof`），单次 open 约 0.3–0.55 ms（正常机器约 10 µs），于是：

| `%TEMP%` 文件数 | 登录到桌面出现的耗时 |
| :--- | :--- |
| 42,010 | 23.1 秒 |
| 6,643 | 2.0 秒 |

所以测试残留会直接变成开机黑屏时间，必须从源头掐住。

根目录的 `conftest.py` 在 `pytest_sessionstart`（任何用例执行之前）做两件事：

- 删除 `%TEMP%\pytest-of-*\garbage-*` 墓碑目录——它们存在的唯一原因就是上一次删除失败；
- 按编号排序，只保留最新 `WB_PYTEST_KEEP_RUNS` 轮，更早的删掉。

删除失败会清只读位重试 3 次；整个过程 best-effort，任何异常都不会影响测试结果。被清理的目录数会以 `[conftest] swept N stale pytest temp dir(s), M could not be removed` 打印到 stderr。

稳定之后 `%TEMP%` 的占用是「最多两轮，约 100–150 MB」，与运行次数无关。

### 环境变量

| 变量 | 默认 | 作用 |
| :--- | :--- | :--- |
| `WB_PYTEST_KEEP_RUNS` | `2` | 保留最近多少轮编号目录 |
| `WB_PYTEST_SWEEP_MIN_AGE` | `3600` | 只清理 N 秒以前创建的目录 |

`MIN_AGE` 的存在是为了同时跑第二个 pytest 时不误伤。验证清扫逻辑本身时把它设成 0，并把保留轮数设成 1：

```powershell
$env:WB_PYTEST_KEEP_RUNS="1"; $env:WB_PYTEST_SWEEP_MIN_AGE="0"
python -m pytest tests/test_data_paths.py -q
```

### 为什么不用 `--basetemp` 把临时根挪走

试过，已撤回。两种写法（`config.option.basetemp = 新路径`、覆盖 `tempfile.tempdir`）都能把占用压到几百 KB，但都会打破 `tests/test_workflow_retention.py`：

```
assert [r[0] for r in results] == [True, False, False]
E  assert [False, True, False] == [True, False, False]
```

原因是两件事叠加。其一，这个用例用「第 2 次 `download` 调用就抛错」制造失败，而 `download` 跑在 `ThreadPoolExecutor` 里，谁是第 2 次取决于线程完成顺序，本身就有竞态。其二，`%TEMP%` 是杀软实时防护的豁免区，挪出去之后同一批文件操作从 4.5 秒变成 6.5–11 秒，把竞态推到了失败那一侧。

交替对照（ABAB×3）：原位 6/6 通过，新位置 4/6 失败。**为了治清理问题去改测试的运行位置，不值得**——位置、编号、目录结构现在全部保持原样，用例看到的世界和以前一模一样。

顺带记一个坑：`--basetemp` 指向不存在的父目录会直接 `FileNotFoundError`（`WinError 3`），pytest 不替你建父目录。

## 两个容易误判的现象

**一、在外部工具（AI 助手、IDE 集成终端）里跑测试可能出现成片假 ERROR。**

某些工具会往 `PYTHONPATH` 里注入一个 `sitecustomize.py` 拦截 `shutil.rmtree`，命中就终止进程。表现是几秒内几百个用例连续报同一个栈：

```
.../cli/vendor/shim/sitecustomize.py:866
    result = subprocess.run([node_bin, guard_path, "check", "--target", abs_path], ...)
```

实测一次全量出了 625 个 ERROR，全部由此而来，与被测代码无关。在普通终端里跑不受影响；如果必须在被注入的环境里跑，先清掉这些变量：

```powershell
python -m pytest -q
# 若出现上述栈，改用：
$env:PYTHONPATH=$null
python -m pytest -q
```

**二、`test_workflow_retention.py::test_adapter_acknowledges_only_valid_successful_downloads_and_keeps_partial_results` 本身带竞态。**

它断言 `[True, False, False]`，实际依赖线程完成顺序，改动任何影响文件操作速度的东西（临时目录位置、杀软设置、磁盘负载）都可能让它在同一台机器上翻转。清理目录不属于此类改动（不动位置），但如果哪天它开始不稳，先怀疑这个用例的构造方式，而不是环境。

## 相关文档

- [代码架构](ARCHITECTURE.md)：模块职责与依赖规则
- [优化记录](OPTIMIZATION.md)：性能实测数据
