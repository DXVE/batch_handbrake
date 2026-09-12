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

# 转码中途写入的临时文件标记，例如 影片.hbpart.mp4，成功后再原子改名为 影片.mp4
PART_TOKEN = ".hbpart."

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
    """返回 (source_dir, single_file)。single_file 为 None 表示扫描整个目录。"""
    while True:
        raw = input(prompt).strip().strip('"').strip("'").strip()
        if not raw:
            continue
        p = Path(raw)
        try:
            if not p.exists():
                user_print("路径不存在: {}".format(p))
                continue
            if p.is_file():
                if p.suffix.lower() not in VIDEO_EXTENSIONS:
                    user_print("不是支持的视频文件: {}".format(p))
                    continue
                user_print("提示：检测到单个视频文件，将只转码该文件: {}".format(p.name))
                return p.parent.resolve(), p.resolve()
            if not p.is_dir():
                user_print("路径不是文件夹: {}".format(p))
                continue
            return p.resolve(), None
        except OSError as e:
            user_print("无法访问 {}: {}".format(p, e))


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
    walk_errors = []

    def onerror(err):
        walk_errors.append(err)
        log("扫描出错: {}".format(err), level="WARN")

    for root, dirs, filenames in os.walk(source_dir, onerror=onerror):
        for fname in filenames:
            if PART_TOKEN in fname:
                continue
            if Path(fname).suffix.lower() in VIDEO_EXTENSIONS:
                files.append(Path(root) / fname)
    files.sort()
    return files, walk_errors


def get_relative_path(file_path, source_dir):
    return file_path.relative_to(source_dir)


def build_output_path(file_path, source_dir, output_dir, ext):
    rel = get_relative_path(file_path, source_dir)
    return output_dir / rel.parent / (rel.stem + ext)


def make_part_path(output_file):
    """转码期间使用的临时文件路径，保留原扩展名以便 HandBrakeCLI 识别容器。"""
    return output_file.with_name(
        output_file.stem + PART_TOKEN + output_file.suffix.lstrip("."))


def _output_key(path):
    # Windows 文件系统大小写不敏感，统一按小写比较
    return str(path).casefold()


def _unique_path(base, taken, avoid_disk):
    candidate = base
    index = 1
    while _output_key(candidate) in taken or (avoid_disk and candidate.exists()):
        candidate = base.with_name("{}_{}{}".format(base.stem, index, base.suffix))
        index += 1
    return candidate


def plan_outputs(video_files, source_dir, output_dir, ext):
    """确定每个源文件的最终输出路径，处理批内重名与已存在的成品。

    返回 [(video_file, output_file), ...]；已按用户选择跳过的不在其中。
    """
    assigned = []
    taken = set()
    renamed = []

    for video_file in video_files:
        base = build_output_path(video_file, source_dir, output_dir, ext)
        final = base
        if _output_key(base) in taken:
            final = _unique_path(base, taken, avoid_disk=False)
        taken.add(_output_key(final))
        assigned.append([video_file, final])
        if final != base:
            renamed.append((base, final))

    if renamed:
        user_print("【预检】发现 {} 处输出重名，已自动改名以保留全部文件：".format(len(renamed)))
        for before, after in renamed:
            user_print("  {}  ->  {}".format(before.name, after.name))

    existing = [pair for pair in assigned if pair[1].exists()]
    if existing:
        user_print("【预检】输出目录已存在 {} 个同名成品，处理方式：".format(len(existing)))
        user_print("  1) 跳过（推荐，可续转）")
        user_print("  2) 覆盖")
        user_print("  3) 自动改名保留")
        choice = input("  选择 (回车=1): ").strip()
        if choice == "3":
            for pair in existing:
                pair[1] = _unique_path(pair[1], taken, avoid_disk=True)
                taken.add(_output_key(pair[1]))
        elif choice == "2":
            pass
        else:
            before_count = len(assigned)
            assigned = [pair for pair in assigned if not pair[1].exists()]
            user_print("  已跳过 {} 个已存在的成品".format(before_count - len(assigned)))

    return [(video_file, output_file) for video_file, output_file in assigned]


def decode_bytes(raw):
    """HandBrakeCLI 输出优先按 UTF-8 解码，失败回退 GBK。"""
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _find_first_preset(json_path):
    """返回 JSON 中第一个叶子预设的 (完整名称, 预设字典)，失败返回 (None, None)。"""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None

    def find_preset(obj, path=""):
        if isinstance(obj, dict):
            name = obj.get("PresetName", "")
            children = obj.get("ChildrenArray", [])
            if children:
                for child in children:
                    found = find_preset(child, "{}/{}".format(path, name) if path else name)
                    if found:
                        return found
            elif name:
                return ("{}/{}".format(path, name) if path else name), obj
        return None

    for item in data.get("PresetList", []):
        found = find_preset(item)
        if found:
            return found
    return find_preset(data) or (None, None)


def extract_preset_name_from_json(json_path):
    name, _ = _find_first_preset(json_path)
    return name


def list_preset_names(preset_file=None):
    """调用 HandBrakeCLI 列出可用预设名（实测输出走 stderr）。失败返回 None。"""
    cmd = [HANDBRAKE_CLI]
    if preset_file:
        cmd += ["--preset-import-file", preset_file]
    cmd += ["--preset-list"]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=120,
        )
    except Exception as e:
        log("预设列表执行失败: {}".format(e), level="WARN")
        return None

    text = decode_bytes((proc.stdout or b"") + b"\n" + (proc.stderr or b""))
    names = set()
    for line in text.replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if (stripped.startswith("[")
                or stripped.startswith("Cannot load")
                or stripped.startswith("HandBrake has exited")):
            continue
        names.add(stripped)
    return names


def check_preset(preset, container):
    """开工前校验预设，返回解析出的预设名；用户选择中止时返回 None。"""
    user_print("【预检】预设")
    preset_name = preset
    preset_json = None

    if os.path.isfile(preset):
        preset_json = preset
        preset_name = extract_preset_name_from_json(preset)
        if not preset_name:
            user_print("  预设校验失败：无法从 JSON 提取预设名，请检查文件内容或格式")
            log("预设校验失败: JSON 无法解析出预设名 ({})".format(preset), level="ERROR")
            return None
        user_print("  JSON 中解析出预设名: {}".format(preset_name))
    else:
        user_print("  使用内置预设名: {}".format(preset_name))

    names = list_preset_names(preset_json)
    if names is None:
        user_print("  警告：无法获取 HandBrakeCLI 预设列表，跳过名称校验")
    else:
        leaf = preset_name.rsplit("/", 1)[-1]
        if preset_name in names or leaf in names:
            user_print("  预设名称校验通过")
        else:
            user_print("  警告：在 HandBrakeCLI 预设列表中未找到 '{}'".format(preset_name))
            answer = input("  是否仍要继续？(y/N): ").strip().lower()
            if answer != "y":
                return None

    if preset_json:
        _, preset_obj = _find_first_preset(preset_json)
        fmt = (preset_obj or {}).get("FileFormat", "")
        want = {"mp4": "av_mp4", "mkv": "av_mkv"}.get(container)
        if fmt and want and fmt != want:
            user_print("  警告：预设输出格式为 {}，与所选容器 {} 不一致，可能导致转码失败"
                       .format(fmt, container))
            answer = input("  是否仍要继续？(y/N): ").strip().lower()
            if answer != "y":
                return None

    log("预设校验完成: {}".format(preset_name))
    return preset_name


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
    part_file = make_part_path(output_file)

    user_print("[{}/{}] 转码中: {}".format(idx, total, rel_path))

    log("=" * 55)
    log("[{}/{}] 开始: {}".format(idx, total, rel_path))
    log("命令: {} --preset ... -i \"{}\" -o \"{}\"  (临时文件)".format(
        HANDBRAKE_CLI, video_file, part_file))
    log("-" * 55)

    # 清掉上次异常退出可能残留的临时文件
    try:
        if part_file.exists():
            part_file.unlink()
            log("[{}/{}] 已清理残留临时文件: {}".format(idx, total, part_file), level="WARN")
    except Exception as e:
        log("[{}/{}] 清理临时文件失败: {} ({})".format(idx, total, part_file, e), level="ERROR")

    encoded = run_handbrake(video_file, part_file, preset)

    ok = False
    if encoded:
        try:
            # 成功才原子改名，保证最终路径上只会出现完整成品
            os.replace(str(part_file), str(output_file))
            ok = True
        except Exception as e:
            log("[{}/{}] 转码完成但改名失败，已保留临时文件 {}: {}".format(
                idx, total, part_file, e), level="ERROR")
    else:
        # 只删临时文件，绝不碰已存在的成品
        try:
            if part_file.exists():
                part_file.unlink()
                log("[{}/{}] 已删除临时文件: {}".format(idx, total, part_file), level="WARN")
        except Exception as e:
            log("[{}/{}] 删除临时文件失败: {} ({})".format(idx, total, part_file, e), level="ERROR")

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

        print("【第一步】原文件夹（或单个视频文件）")
        source_dir, single_file = get_input_path("  （可拖拽）: ")
        log("原文件夹: {}".format(source_dir))
        if single_file is not None:
            log("单文件模式: {}".format(single_file))

        print("【第二步】输出文件夹")
        output_dir = get_output_path("  （可拖拽）: ")
        log("输出文件夹: {}".format(output_dir))
        log()

        user_print("扫描中……")
        if single_file is not None:
            video_files, walk_errors = [single_file], []
        else:
            video_files, walk_errors = collect_video_files(source_dir)
        total = len(video_files)
        if total == 0:
            for e in walk_errors:
                user_print("无法访问: {}".format(e))
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

        if check_preset(preset, container) is None:
            user_print("已取消")
            input("按回车退出...")
            return

        planned = plan_outputs(video_files, source_dir, output_dir, output_ext)
        if not planned:
            user_print("所有输出都已存在，无需转码")
            input("按回车退出...")
            return
        if len(planned) != total:
            user_print("本次将转码 {} 个（扫描到 {} 个）\n".format(len(planned), total))
        total = len(planned)

        tasks = []
        for idx, (video_file, output_file) in enumerate(planned, start=1):
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
