"""Run the actual Windows bundle with no Python on PATH and isolated data."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time


def check_bundle(bundle: Path, screenshot: Path) -> None:
    bundle = bundle.resolve()
    executable = bundle / "WindowsLinkManager.exe"
    if not executable.is_file() or not (bundle / "_internal").is_dir():
        raise ValueError("Expected WindowsLinkManager.exe and its _internal folder")
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    windows = Path(os.environ["SystemRoot"])
    env["PATH"] = os.pathsep.join(str(path) for path in (windows / "System32", windows))
    env["QT_QPA_PLATFORM"] = "offscreen"
    screenshot = screenshot.resolve()
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="linkmanager-binary-smoke-") as directory:
        isolated = Path(directory).resolve() / "program"
        shutil.copytree(bundle, isolated)
        exe = isolated / executable.name
        subprocess.run([str(exe), "--demo", "--screenshot", str(screenshot)],
                       cwd=directory, env=env, check=True, timeout=40)
        data = screenshot.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("The bundled GUI did not produce a PNG screenshot")
        width, height = struct.unpack(">II", data[16:24])
        if width < 900 or height < 560:
            raise ValueError(f"Unexpected GUI screenshot dimensions: {width}x{height}")
        process = subprocess.Popen([str(exe)], cwd=directory, env=env)
        try:
            deadline = time.monotonic() + 12
            lock = isolated / ".state/gui.lock"
            while not lock.exists():
                if process.poll() is not None:
                    raise RuntimeError(f"First-run GUI exited early: {process.returncode}")
                if time.monotonic() >= deadline:
                    raise RuntimeError("First-run GUI did not initialize its instance lock")
                time.sleep(0.1)
            time.sleep(1)
            if process.poll() is not None:
                raise RuntimeError("First-run GUI exited instead of staying open")
            if (isolated / "软链接.txt").exists():
                raise RuntimeError("First-run GUI wrote a configuration without user input")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
    print(f"Bundled GUI passed: {width}x{height}; first run stays open with an empty draft")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    args = parser.parse_args()
    check_bundle(args.bundle, args.screenshot)


if __name__ == "__main__":
    main()
