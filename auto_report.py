#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览报告自动刷新包装器。

给 Quicker 的「闲置 5 分钟」事件用：静默运行 gen_report.py，
把每次结果追加到 logs/auto-refresh.log（超过 1 MB 自动轮转，保留 3 份备份）。
手动运行也可以，日志会标注 trigger。

用法：
  python auto_report.py                     # 手动
  python auto_report.py --trigger event     # Quicker 事件触发
  python auto_report.py --trigger event --quiet
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
LOG_FILE = LOG_DIR / "auto-refresh.log"
LOCK_FILE = LOG_DIR / ".refresh.lock"
MAX_LOG_BYTES = 1024 * 1024
BACKUPS = 3
TIMEOUT_SECONDS = 150
LOCK_STALE_SECONDS = 15 * 60


def stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def rotate() -> None:
    if not LOG_FILE.exists() or LOG_FILE.stat().st_size < MAX_LOG_BYTES:
        return
    for i in range(BACKUPS - 1, 0, -1):
        src = LOG_FILE.with_name(f"{LOG_FILE.name}.{i}")
        dst = LOG_FILE.with_name(f"{LOG_FILE.name}.{i + 1}")
        if src.exists():
            os.replace(src, dst)
    os.replace(LOG_FILE, LOG_FILE.with_name(f"{LOG_FILE.name}.1"))


def append_log(line: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rotate()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except OSError as exc:
        print(f"写日志失败：{exc}", file=sys.stderr)


def acquire_lock() -> bool:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except OSError:
            age = LOCK_STALE_SECONDS
        if age < LOCK_STALE_SECONDS:
            return False
        try:
            LOCK_FILE.unlink()
        except OSError:
            return False
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {stamp()}\n")
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", default="manual", help="触发来源，写进日志：manual / event / schedule")
    ap.add_argument("--quiet", action="store_true", help="成功时不输出生成器摘要")
    args = ap.parse_args()

    if not acquire_lock():
        append_log(f"{stamp()} [{args.trigger}] SKIP 已有刷新正在运行")
        return 0

    started = time.monotonic()
    try:
        rotate()
        cmd = [sys.executable, "-X", "utf8", str(HERE / "gen_report.py"),
               "--out", str(HERE / "dist" / "browser-report.html")]
        try:
            proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            append_log(f"{stamp()} [{args.trigger}] FAIL {elapsed:.1f}s 超时（>{TIMEOUT_SECONDS}s）")
            print("生成超时", file=sys.stderr)
            return 1
        except OSError as exc:
            elapsed = time.monotonic() - started
            append_log(f"{stamp()} [{args.trigger}] FAIL {elapsed:.1f}s 无法启动 Python：{exc}")
            print(f"无法启动 Python：{exc}", file=sys.stderr)
            return 1

        elapsed = time.monotonic() - started
        out_lines = [ln.strip() for ln in proc.stdout.strip().splitlines() if ln.strip()]
        summary = " | ".join(out_lines[1:5])
        size = ""
        if out_lines and out_lines[0].startswith("OK"):
            match = re.search(r"\(([\d.]+ MB)\)", out_lines[0])
            size = match.group(1) if match else ""
        status = "OK" if proc.returncode == 0 else f"FAIL({proc.returncode})"
        detail = summary if summary else (out_lines[0] if out_lines else "")
        if proc.returncode != 0:
            err = " / ".join(ln.strip() for ln in proc.stderr.strip().splitlines()[-3:])
            detail = err or detail or "无错误输出"
        elif not detail:
            detail = "生成完成"
        append_log(f"{stamp()} [{args.trigger}] {status} {elapsed:.1f}s {size} | {detail}")

        if proc.returncode != 0:
            print(proc.stderr.strip() or proc.stdout.strip(), file=sys.stderr)
            return proc.returncode
        if not args.quiet:
            print(proc.stdout, end="")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
