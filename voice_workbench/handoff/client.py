"""目标机：按主电脑给的清单接收文件，再把写死的路径改成本机的。

接收本身不难，难的是「过来之后还能跑」。三类东西里含有主电脑的绝对路径：
venv 的 pyvenv.cfg、config.json（含批量后端设置）、数据库里的声音快照。
漏掉任何一类，界面看起来正常，直到按下生成才报错——所以在接收结束时
就一次性全部改写，并把结果写进回执。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request

CHUNK = 1024 * 1024
USER_AGENT = "ShengjianHandoff/1"


class HandoffError(RuntimeError):
    pass


def _opener():
    # A system proxy would make two machines on the same LAN fail to see each other.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return _opener().open(request, timeout=timeout)


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def fetch_manifest(base: str):
    try:
        with _get(base.rstrip("/") + "/manifest.json", {"Accept-Encoding": "gzip"}) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            value = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise HandoffError("配对码不正确，或主电脑已结束交接。") from exc
        raise HandoffError(f"主电脑返回 {exc.code}：{exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise HandoffError(f"连不上主电脑：{exc}") from exc
    if not isinstance(value.get("entries"), list):
        raise HandoffError("清单格式无法识别，请更新主电脑上的声笺。")
    return value


def plan(entries, target_data: Path, target_install: Path, rewritten=None):
    """Split the manifest into what is missing and what is already correct.

    ``rewritten`` holds hashes of files this machine intentionally changed after
    receiving them (rebased paths). Without it every re-sync would re-send the
    whole library database, because its digest no longer matches the source.
    """
    rewritten = rewritten or {}
    missing, present = [], []
    for entry in entries:
        root = target_install if entry["target"] == "install" else target_data
        path = root / entry["rel"]
        # A rebased file has a different size by definition, so the cheap size
        # gate must be skipped for it — otherwise every re-sync re-downloads it.
        expected_local = rewritten.get(entry["id"])
        if path.is_file() and (expected_local or path.stat().st_size == entry["size"]):
            actual = _sha256(path)
            if actual and (actual == entry.get("sha256") or actual == expected_local):
                present.append(entry)
                continue
        missing.append(entry)
    return missing, present


def _receipt_path(data_dir) -> Path:
    return Path(data_dir) / "handoff" / "receipt.json"


def _rewritten_hashes(data_dir) -> dict:
    try:
        value = json.loads(_receipt_path(data_dir).read_text(encoding="utf-8"))
        recorded = value.get("rewritten")
        return recorded if isinstance(recorded, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_rewritten(data_dir, manifest) -> dict:
    """Remember the post-rebase digest of everything relocation touched."""
    data_dir = Path(data_dir)
    recorded = {}
    for entry in manifest.get("entries", []):
        if entry["rel"] in ("config.json", "tasks.sqlite3") or entry["rel"].endswith("pyvenv.cfg"):
            path = data_dir / entry["rel"]
            if path.is_file():
                recorded[entry["id"]] = _sha256(path)
    return recorded


class Receiver:
    def __init__(self, base: str, data_dir, install_dir, log=None, progress=None):
        self.base = base.rstrip("/")
        self.data_dir = Path(data_dir)
        self.install_dir = Path(install_dir)
        self.log = log or (lambda text: None)
        self.progress = progress or (lambda done, total, name: None)

    def _target(self, entry) -> Path:
        root = self.install_dir if entry["target"] == "install" else self.data_dir
        return root / entry["rel"]

    def download(self, entry, index):
        target = self._target(entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        url = f"{self.base}/file/{index}"
        for attempt in (1, 2):
            offset = partial.stat().st_size if partial.is_file() else 0
            if offset > entry["size"]:
                partial.unlink(missing_ok=True)
                offset = 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with _get(url, headers, timeout=120) as response, partial.open("ab") as out:
                    while True:
                        block = response.read(CHUNK)
                        if not block:
                            break
                        out.write(block)
                        offset += len(block)
                        self.progress(offset, entry["size"], entry["rel"])
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # 主电脑上的临时文件已经过期；少它一个不影响整体。
                    partial.unlink(missing_ok=True)
                    self.log(f"文件已在主电脑上过期，跳过：{entry['rel']}")
                    return False
                if attempt == 2:
                    raise HandoffError(f"{entry['rel']} 下载失败：HTTP {exc.code}") from exc
                self.log(f"重试 {entry['rel']}（HTTP {exc.code}）")
                time.sleep(1)
                continue
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                if attempt == 2:
                    raise HandoffError(f"{entry['rel']} 下载中断：{exc}") from exc
                self.log(f"重试 {entry['rel']}（{exc}）")
                time.sleep(1)
                continue
            if not entry.get("sha256") or _sha256(partial) == entry["sha256"]:
                os.replace(partial, target)
                return True
            partial.unlink(missing_ok=True)
            self.log(f"校验不一致，重新接收 {entry['rel']}")
        raise HandoffError(f"{entry['rel']} 校验失败，请重新开始交接。")

    def run(self, manifest, resume=True):
        entries = manifest["entries"]
        if resume:
            missing, present = plan(entries, self.data_dir, self.install_dir, _rewritten_hashes(self.data_dir))
        else:
            missing, present = list(entries), []
        index_of = {id(entry): position for position, entry in enumerate(entries)}
        total = sum(entry["size"] for entry in missing)
        self.log(f"需要接收 {len(missing)} 个文件 / {total / 1024 ** 3:.1f} GB，已存在 {len(present)} 个。")
        done = 0
        skipped_expired = 0
        for entry in missing:
            if not self.download(entry, index_of[id(entry)]):
                skipped_expired += 1
                continue
            done += entry["size"]
            self.log(f"[{done / max(total, 1) * 100:5.1f}%] {entry['rel']}")
        return {"downloaded": len(missing) - skipped_expired, "skipped": len(present),
                "expired": skipped_expired, "bytes": total}


# -- 路径改写 -------------------------------------------------------------

def _swap(text: str, before: str, after: str) -> str:
    if not before:
        return text
    return re.sub(re.escape(before), lambda _: after, text, flags=re.IGNORECASE)


def rewrite_venvs(data_dir: Path, source: str):
    """Point every managed venv at the interpreter that travelled with it."""
    data_dir = Path(data_dir)
    changed = []
    for config in sorted(data_dir.glob("runtime/qwen-batch/env-*/pyvenv.cfg")):
        try:
            text = config.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # pyvenv.cfg records Windows paths with backslashes; rebase both spellings.
        updated = _swap(_swap(text, source, str(data_dir)), source.replace("\\", "/"), str(data_dir).replace("\\", "/"))
        if updated != text:
            config.write_text(updated, encoding="utf-8")
            changed.append(str(config))
    return changed


def rewrite_config(data_dir: Path, source: str):
    from ..config import atomic_json
    from ..data_paths import _rebase_config

    data_dir = Path(data_dir)
    path = data_dir / "config.json"
    if not path.is_file() or not source:
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    updated = _rebase_config(value, Path(source), data_dir)
    if updated != value:
        atomic_json(path, updated)
        return True
    return False


def _batch_model_dir(data_dir: Path):
    """The HF weight directory the batch backend will actually load."""
    try:
        value = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    root = Path(str((value.get("batch_inference") or {}).get("model_dir") or ""))
    candidates = [root] if root else []
    candidates += sorted((data_dir / "models" / "qwen-batch").glob("*/*"))
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "config.json").is_file() and any(candidate.glob("*.safetensors")):
            return candidate
    return None


def rewrite_batch_only(data_dir: Path):
    """把纯批量交接的预设改指 HF 目录。

    批量准入要求 ``voice.model_dir`` 与 ``batch_inference.source_model`` 是同一路径；
    只带批量资产时 GGUF 与原生引擎都没过来，因此两者一起改指本地 HF 权重目录。
    GGUF 仍然存在时（全量镜像）什么都不动，原生路径保持可用。
    """
    from ..config import atomic_json

    data_dir = Path(data_dir)
    path = data_dir / "config.json"
    if not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    batch = value.get("batch_inference")
    if not isinstance(batch, dict):
        return False
    source = str(batch.get("source_model") or "")
    if not source or Path(source).is_file():
        return False
    model = _batch_model_dir(data_dir)
    if model is None:
        return False
    changed = False

    def retarget(voice):
        nonlocal changed
        raw = str(voice.get("model_dir") or "") if isinstance(voice, dict) else ""
        if not raw or Path(raw).suffix.lower() != ".gguf":
            return
        voice["model_dir"] = str(model)
        voice["engine_executable"] = ""
        changed = True

    for shelf in (value.get("voices") or [], value.get("model_templates") or []):
        for voice in shelf:
            retarget(voice)
    for voice in (value.get("deleted_voices") or {}).values():
        retarget(voice)
    if Path(source) != model:
        batch["source_model"] = str(model)
        changed = True
    if changed:
        atomic_json(path, value)
    return changed


def rewrite_database(data_dir: Path, source: str):
    """Rebase the voice snapshots stored inside finished jobs and segments."""
    from contextlib import closing

    from ..data_paths import _rebase_config, rebase_paths

    data_dir = Path(data_dir)
    path = data_dir / "tasks.sqlite3"
    if not path.is_file() or not source:
        return 0
    rows = 0
    # closing() matters: `with conn` only commits, it never closes, and a leaked
    # handle makes the next attempt to replace this file fail with WinError 5.
    with closing(sqlite3.connect(path)) as db, db:
        for table, column in (("jobs", "voice"), ("segments", "voice"), ("segments", "provenance")):
            columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                continue
            for rowid, payload in db.execute(f"SELECT rowid,{column} FROM {table} WHERE {column} IS NOT NULL").fetchall():
                try:
                    value = json.loads(payload)
                except ValueError:
                    continue
                updated = json.dumps(_rebase_config(value, Path(source), data_dir), ensure_ascii=False)
                if updated != payload:
                    db.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (updated, rowid))
                    rows += 1
        if db.execute("SELECT name FROM sqlite_master WHERE name='cache'").fetchone():
            for key, value in db.execute("SELECT key,path FROM cache").fetchall():
                rebased = rebase_paths(value, Path(source), data_dir)
                if rebased != value:
                    db.execute("UPDATE cache SET path=? WHERE key=?", (rebased, key))
                    rows += 1
    return rows


def relocate(data_dir, source: str):
    """Run all rewrites and report what changed."""
    data_dir = Path(data_dir)
    config = rewrite_config(data_dir, source)
    batch_only = rewrite_batch_only(data_dir)
    return {"venv": rewrite_venvs(data_dir, source), "config": bool(config or batch_only),
            "batch_only": batch_only, "database_rows": rewrite_database(data_dir, source)}


# -- 入口 -----------------------------------------------------------------

def receive(base, data_dir, install_dir, log=None, progress=None, resume=True):
    receiver = Receiver(base, data_dir, install_dir, log, progress)
    manifest = fetch_manifest(base)
    result = receiver.run(manifest, resume=resume)
    result["relocated"] = relocate(data_dir, manifest.get("data_dir", ""))
    result["manifest"] = {k: manifest.get(k) for k in ("version", "scope", "files", "bytes")}
    try:
        path = _receipt_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"result": result, "finished": time.time(),
                                    "rewritten": _record_rewritten(data_dir, manifest)},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return result


def discover_target(prefer=""):
    """同网段里找正在分发的主电脑；指定了地址就只认那一台。"""
    from .discovery import announce

    found = announce()
    if prefer:
        return next((item for item in found if item["host"] == prefer), None)
    return found[0] if found else None


def wait_pair(host, token, seconds=180):
    """等主电脑点「允许」；批准后配对码随应答回来一次。"""
    from .discovery import ask_pair

    print("已向主电脑申请接入，请在那台电脑的「局域网分发」里点「允许」…", flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        answer = ask_pair(host, token)
        if answer.get("code"):
            return answer["code"]
        time.sleep(0.8)
    return ""


def receive_main(argv=None):
    parser = argparse.ArgumentParser(prog="handoff-receive", description="从同网段的主电脑接收声笺程序与模型")
    parser.add_argument("--host", default="", help="主电脑的局域网地址；留空则自动发现在同网段的声笺")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--code", default="", help="配对码；留空则向主电脑申请授权，等对方点「允许」")
    parser.add_argument("--wait", type=int, default=180, help="等待主电脑确认的秒数")
    parser.add_argument("--data-dir", type=Path, default=None, help="目标数据目录，默认与安装版一致")
    parser.add_argument("--install-dir", type=Path, default=None, help="目标程序目录")
    parser.add_argument("--no-resume", action="store_true", help="忽略已存在的文件，全部重收")
    args = parser.parse_args(argv)

    from ..data_paths import default_data_dir
    from ..runtime import frozen, project_root

    if not (args.host and args.port and args.code):
        target = discover_target(args.host)
        if target is None:
            print("没有在同网段找到正在分发的声笺。请确认主电脑已点「开始分发」，"
                  "或手动指定 --host / --port / --code。", file=sys.stderr)
            return 1
        args.host, args.port = target["host"], target["port"]
        print(f"找到 {target['name'] or '主电脑'}（{args.host}）："
              f"{target['files']} 个文件 / {target['bytes'] / 1024 ** 3:.1f} GB", flush=True)
        if not args.code:
            args.code = wait_pair(args.host, target["token"], args.wait)
            if not args.code:
                print("主电脑没有在限定时间内允许接入。请点「允许」后重试。", file=sys.stderr)
                return 1

    data_dir = args.data_dir or default_data_dir()
    install_dir = args.install_dir or (project_root() if frozen() else Path.cwd())
    base = f"http://{args.host}:{args.port}/{args.code}"

    def log(text):
        print(text, flush=True)

    last = [0.0]

    def progress(done, total, name):
        now = time.time()
        if now - last[0] < 0.5 and done < total:
            return
        last[0] = now
        sys.stdout.write(f"\r{name[:48]:<50} {done / 1024 ** 2:7.1f} / {total / 1024 ** 2:.1f} MB")
        sys.stdout.flush()

    try:
        result = receive(base, data_dir, install_dir, log, progress, resume=not args.no_resume)
    except HandoffError as exc:
        print(f"\n接收失败：{exc}", file=sys.stderr)
        return 1
    print()
    log(f"完成：接收 {result['downloaded']} 个，跳过 {result['skipped']} 个。")
    relocated = result["relocated"]
    log(f"路径修复：venv {len(relocated['venv'])} 个，config.json {'已更新' if relocated['config'] else '无需更新'}，"
        f"数据库 {relocated['database_rows']} 行。")
    if relocated.get("batch_only"):
        log("这台机器只带批量资产：声音预设已改指本地 HF 权重，单条生成与原生回退不可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(receive_main())
