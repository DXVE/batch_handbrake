#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HandBrake 批量转码脚本
"""

import json
import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# 优先使用脚本同目录的便携版，否则回退到系统安装路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HANDBRAKE_CLI = os.path.join(SCRIPT_DIR, "HandBrakeCLI.exe")
if not os.path.isfile(HANDBRAKE_CLI):
    _real_prog = os.environ.get("ProgramW6432", os.environ.get("ProgramFiles", "C:\\Program Files"))
    HANDBRAKE_CLI = os.path.join(_real_prog, "HandBrake", "HandBrakeCLI.exe")

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v",
    ".ts", ".mts", ".m2ts", ".vob", ".webm", ".mpeg", ".mpg",
    ".3gp", ".asf", ".divx", ".rmvb", ".f4v",
}

_LOG_FILE = None
_LOG_LOCK = threading.Lock()
_ACTIVE_SUBPROCESSES = set()
_SUBPROCESS_LOCK = threading.Lock()


def setup_log():
    d = Path(__file__).resolve().parent / "log"
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return d / "handbrake_{}.log".format(ts)


def log(msg="", level="INFO", console=False):
    if msg:
        tid = threading.current_thread().name.replace("ThreadPoolExecutor-", "W")
        line = "[{}] [{}] [{}] {}".format(
            datetime.now().strftime("%H:%M:%S"), level, tid, msg)
    else:
        line = ""
    with _LOG_LOCK:
        if console:
            print(line)
        global _LOG_FILE
        if _LOG_FILE is not None:
            try:
                _LOG_FILE.write(line + "\n")
                _LOG_FILE.flush()
            except Exception:
                pass


def user_print(msg):
    with _LOG_LOCK:
        print(msg, flush=True)


def get_input_path(prompt):
    while True:
        raw = input(prompt).strip().strip('"').strip("'").strip()
        if not raw:
            continue
        p = Path(raw)
        if not p.exists():
            user_print("路径不存在: {}".format(p))
            continue
        return p.resolve()


def get_output_path(prompt):
    while True:
        raw = input(prompt).strip().strip('"').strip("'").strip()
        if not raw:
            continue
        p = Path(raw).resolve()
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
                log("已创建: {}".format(p))
                return p
            except Exception as e:
                user_print("无法创建: {}".format(e))
                continue
        if not p.is_dir():
            continue
        return p


def get_worker_count():
    while True:
        raw = input("  并行数 (1-8, 回车=1): ").strip()
        if not raw:
            return 1
        try:
            n = int(raw)
            if 1 <= n <= 8:
                return n
        except ValueError:
            pass
        user_print("输入 1-8 之间的数字")


def get_preset():
    while True:
        raw = input("  预设 (拖入 JSON 或输入预设名): ").strip().strip('"').strip("'")
        if not raw:
            user_print("预设不能为空")
            continue
        if os.path.isfile(raw):
            return raw
        if raw:
            return raw
        user_print("预设不能为空")


def collect_video_files(source_dir):
    files = []
    for root, dirs, filenames in os.walk(source_dir):
        for fname in filenames:
            if Path(fname).suffix.lower() in VIDEO_EXTENSIONS:
                files.append(Path(root) / fname)
    files.sort()
    return files


def get_relative_path(file_path, source_dir):
    return file_path.relative_to(source_dir)


def build_output_path(file_path, source_dir, output_dir, ext):
    rel = get_relative_path(file_path, source_dir)
    return output_dir / rel.parent / (rel.stem + ext)


def extract_preset_name_from_json(json_path):
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    def find_preset(obj, path=""):
        if isinstance(obj, dict):
            name = obj.get("PresetName", "")
            children = obj.get("ChildrenArray", [])
            if children:
                for child in children:
                    r = find_preset(child, "{}/{}".format(path, name) if path else name)
                    if r:
                        return r
            elif name:
                return "{}/{}".format(path, name) if path else name
        return None

    for item in data.get("PresetList", []):
        r = find_preset(item)
        if r:
            return r
    return find_preset(data)


def terminate_all_subprocesses():
    with _SUBPROCESS_LOCK:
        procs = list(_ACTIVE_SUBPROCESSES)
        _ACTIVE_SUBPROCESSES.clear()
    if not procs:
        return
    log("正在终止 {} 个 HandBrakeCLI 进程...".format(len(procs)), level="WARN")
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass
        except Exception:
            pass


def run_handbrake(input_file, output_file, preset):
    cmd = [HANDBRAKE_CLI]

    if os.path.isfile(preset):
        preset_name = extract_preset_name_from_json(preset)
        if not preset_name:
            user_print("错误：无法从 JSON 中提取预设名，请检查文件格式")
            log("无法从 JSON 提取预设名", level="ERROR")
            return False
        cmd += ["--preset-import-file", preset, "--preset", preset_name]
    else:
        cmd += ["--preset", preset]

    cmd += ["-i", str(input_file), "-o", str(output_file)]

    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        with _SUBPROCESS_LOCK:
            _ACTIVE_SUBPROCESSES.add(p)
        try:
            buf = b""
            while True:
                chunk = p.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                # 在原始字节中找最后一个 \r 或 \n，只处理完整行，避免切碎
                pos_cr = buf.rfind(b'\r')
                pos_nl = buf.rfind(b'\n')
                pos = max(pos_cr, pos_nl)
                if pos >= 0:
                    complete = buf[:pos]
                    buf = buf[pos + 1:]
                    text = complete.decode("utf-8", errors="replace").replace("\r", "\n")
                    for line in text.split("\n"):
                        stripped = line.strip()
                        if stripped and "Encoding:" not in stripped:
                            log("  {}".format(stripped))

            if buf:
                text = buf.decode("utf-8", errors="replace").replace("\r", "\n")
                for line in text.split("\n"):
                    stripped = line.strip()
                    if stripped and "Encoding:" not in stripped:
                        log("  {}".format(stripped))

            p.wait()
            return p.returncode == 0
        finally:
            with _SUBPROCESS_LOCK:
                _ACTIVE_SUBPROCESSES.discard(p)
            if p.poll() is None:
                try:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait(timeout=3)
                except Exception:
                    pass

    except FileNotFoundError:
        log("找不到 HandBrakeCLI: {}".format(HANDBRAKE_CLI), level="CRITICAL")
        return False
    except Exception as e:
        log("异常: {}".format(e), level="CRITICAL")
        return False


def encode_one(task):
    (idx, total, video_file, output_file, preset, source_dir, output_dir) = task
    rel_path = get_relative_path(video_file, source_dir)

    user_print("[{}/{}] 转码中: {}".format(idx, total, rel_path))

    log("=" * 55)
    log("[{}/{}] 开始: {}".format(idx, total, rel_path))
    log("命令: {} --preset ... -i \"{}\" -o \"{}\"".format(
        HANDBRAKE_CLI, video_file, output_file))
    log("-" * 55)

    ok = run_handbrake(video_file, output_file, preset)

    if not ok and output_file.exists():
        try:
            output_file.unlink()
            log("[{}/{}] 已删除无效文件: {}".format(idx, total, output_file), level="WARN")
        except Exception as e:
            log("[{}/{}] 删除无效文件失败: {} ({})".format(idx, total, output_file, e), level="ERROR")

    status = "OK  " if ok else "FAIL"
    user_print("[{}/{}] {}   {}".format(idx, total, status, rel_path))
    log("[{}/{}] {}: {}".format(idx, total, status.strip(), rel_path))

    if _LOG_FILE is not None:
        try:
            _LOG_FILE.flush()
            os.fsync(_LOG_FILE.fileno())
        except Exception:
            pass

    return ok


def main():
    global _LOG_FILE

    try:
        _LOG_FILE = None
        log_path = setup_log()
        _LOG_FILE = open(log_path, "w", encoding="utf-8")

        print("=" * 55)
        print("     HandBrake 批量转码工具")
        print("=" * 55)
        log("日志: {}".format(log_path))
        log("启动")
        log("HandBrakeCLI: {}".format(HANDBRAKE_CLI))

        if not os.path.isfile(HANDBRAKE_CLI):
            user_print("找不到 HandBrakeCLI.exe: {}".format(HANDBRAKE_CLI))
            input("按回车退出...")
            return
        if os.path.getsize(HANDBRAKE_CLI) == 0:
            user_print("HandBrakeCLI.exe 为 0 字节！")
            input("按回车退出...")
            return

        print("【预设设置】")
        preset = get_preset()
        log("预设: {}".format(preset))

        print("【并行设置】")
        workers = get_worker_count()
        log("并行数: {}".format(workers))

        print("【第一步】原文件夹")
        source_dir = get_input_path("  （可拖拽）: ")
        log("原文件夹: {}".format(source_dir))

        print("【第二步】输出文件夹")
        output_dir = get_output_path("  （可拖拽）: ")
        log("输出文件夹: {}".format(output_dir))
        log()

        user_print("扫描中……")
        video_files = collect_video_files(source_dir)
        total = len(video_files)
        if total == 0:
            user_print("未找到视频文件")
            input("按回车退出...")
            return
        user_print("找到 {} 个视频文件\n".format(total))
        log("找到 {} 个:".format(total))
        for vf in video_files:
            log("  -> {}".format(get_relative_path(vf, source_dir)))

        print("【第三步】输出容器")
        container = input("  容器格式 (mp4/mkv, 回车=mp4): ").strip().lower()
        if container not in ("mp4", "mkv"):
            if container:  # 用户输入了非空内容（如 "webm"）
                user_print("警告：不支持的容器格式 '{}'，已自动使用 mp4".format(container))
            container = "mp4"
        output_ext = "." + container

        tasks = []
        for idx, video_file in enumerate(video_files, start=1):
            output_file = build_output_path(video_file, source_dir, output_dir, output_ext)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            tasks.append((idx, total, video_file, output_file, preset, source_dir, output_dir))

        start_time = datetime.now()
        success = 0
        failed = 0

        if workers == 1:
            for task in tasks:
                if encode_one(task):
                    success += 1
                else:
                    failed += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(encode_one, t): t for t in tasks}
                try:
                    for future in as_completed(futures):
                        try:
                            if future.result():
                                success += 1
                            else:
                                failed += 1
                        except Exception as e:
                            log("线程异常: {}".format(e), level="ERROR")
                            failed += 1
                except KeyboardInterrupt:
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise

        elapsed = datetime.now() - start_time
        user_print("\n处理完成! 总计:{}  成功:{}  失败:{}  耗时:{}".format(
            total, success, failed, elapsed))
        log("处理完成! 总计:{}  成功:{}  失败:{}  耗时:{}".format(
            total, success, failed, elapsed))

    except KeyboardInterrupt:
        terminate_all_subprocesses()
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        print()
        print("已取消")
    finally:
        if _LOG_FILE is not None:
            try:
                _LOG_FILE.flush()
                os.fsync(_LOG_FILE.fileno())
                _LOG_FILE.close()
            except Exception:
                pass

    input("\n按回车退出...")


if __name__ == "__main__":
    main()
