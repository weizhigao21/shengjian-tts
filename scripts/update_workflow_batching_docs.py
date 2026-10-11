"""Stage and safely install the paired workflow's batch integration notes."""
import argparse
from pathlib import Path
import shutil
import time

ROOT = Path(__file__).resolve().parent.parent


def replace(source, old, new):
    if new in source:
        return source
    if source.count(old) != 1:
        raise RuntimeError("说明文件内容与预期不一致，未写入工作流文件。")
    return source.replace(old, new, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, default=Path(r"G:\code\双语音声工作流"))
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    readme = (args.target / "README.md").read_text(encoding="utf-8-sig")
    changelog = (args.target / "CHANGELOG.md").read_text(encoding="utf-8-sig")
    readme = replace(readme, "需要 **声笺 v0.6.0 或更新版本**", "需要 **声笺 v0.7.0 或更新版本**")
    old = "工作流将字幕片段提交给声笺，等待生成进度并下载 WAV；序号、字幕时间戳和原有配音目录规则保持一致，完成后可继续混音。批量大小控制每次提交的文本数量，本机模型仍按片段串行推理。部分片段失败时成功音频会保留，语音任务显示失败及具体片段信息；点击停止会向声笺发送取消请求。"
    new = "工作流将字幕片段提交给声笺，等待生成进度并下载 WAV；序号、字幕时间戳和原有配音目录规则保持一致，完成后可继续混音。同一项配音运行的多个小批次归入一个声笺任务，名称为「双语配音 · 源文件名」；重新执行一轮配音才创建新的任务，全部命中工作流缓存时不创建服务任务。多个 API 入口向同一声笺服务提交时也会合并。每批只查询和下载自己的结果，前一批失败不会导致下一批拿错音频。部分片段失败时成功音频会保留，点击停止只取消该工作流提交的批次；声笺中取消整个任务后拒绝此轮的后续追加。已有历史任务不自动删除。\n\n批量大小控制一次提交的字幕数量；声笺 **设置 → 批量性能** 中的并发数控制同时生成多少条，支持 1–4 个独立统一引擎实例，并可设置每实例 CPU 线程。默认 1 个实例、CPU 线程自动分配；测试 2 个实例时，批量大小需至少为 2。并发会增加内存和显存，显存不足时声笺将当前任务降为单实例并重试一次；普通创作、Windows 系统声音和备用 Python 后端保持串行。\n\n本机 RTX 2080 Ti 的 VoxCPM2 BF16 短文本实测中，相同 4 段、每实例 4 CPU 线程：单实例约 12 秒，双实例约 25 秒，双实例整卡显存约 10.1 GiB，因此该模型默认保持单实例。增加线程或提高资源占用不保证提速，其他模型应比较实际生成时间后调整。性能设置由声笺保存，不改变音色预设和原有音频缓存身份。"
    readme = replace(readme, old, new)
    marker = "| 生成途中提示预设版本变化 |"
    new_marker = "| 每批字幕都创建一个声笺任务 | 更新并重启声笺至 v0.7.0+，同时更新本工作流适配器；旧 EXE 需重新打包 |\n| 增加线程没有提速或显存不足 | 在声笺的「批量性能」降低并发；VoxCPM2 BF16 在本机双实例实测更慢，建议保持 1 |\n" + marker
    readme = replace(readme, marker, new_marker)
    old = "增加声笺音色自动发现、下拉选择与刷新、预设版本同步和异步批量生成。"
    readme = replace(readme, old, "增加声笺音色自动发现、下拉选择与刷新、预设版本同步、批量任务合并和可配置的并发推理接入。")
    marker = "声笺工作流联动（源码已更新，应用版本号仍为 v2.1.21；旧版 EXE 需重新打包）：\n"
    addition = "\n- 修复声笺接入每批字幕都会创建新任务：同一项配音运行共享工作流标识，多批次和同服务的多 API 提交只创建一个任务；返回批次编号与准确片段位置，查询与停止只操作对应批次。取消或删除整个任务后拒绝旧运行的后续追加。\n- 支持声笺 v0.7.0 的批量性能设置（1–4 个独立统一引擎实例、每实例 CPU 线程），批量大小与推理并发分别设置。发现旧声笺服务时提示更新并停止提交，避免产生大量任务。默认并发保持 1；本机 VoxCPM2 BF16 双实例实测更慢，未盲目提高并发。\n- 2026-10-12 联动验证：原有语音桥接与完整性测试 11 项通过，声笺配套项目 92 项测试通过；真实 Kokoro 将 6 条 LRC 分为 3 批生成、下载，声笺仅保留 1 个任务，6 条 WAV 均为 24 kHz。新增任务合并、批次结果、并发限制、取消、显存不足降级与重复安装测试；未重跑工作流全量测试。\n"
    changelog = replace(changelog, marker, marker + addition)
    modifications = [(args.target / "README.md", readme), (args.target / "CHANGELOG.md", changelog)]
    staged = ROOT / "integration/prepared/docs"
    staged.mkdir(parents=True, exist_ok=True)
    for path, source in modifications:
        (staged / path.name).write_text(source, encoding="utf-8", newline="\n")
    if args.prepare_only:
        print(staged)
        return
    backup = ROOT / "integration/backups" / ("batch-docs-" + time.strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True, exist_ok=False)
    for path, source in modifications:
        shutil.copy2(path, backup / path.name)
        path.write_text(source, encoding="utf-8", newline="\n")
    print(backup)


if __name__ == "__main__":
    main()
