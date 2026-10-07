"""Build the standalone J.A.R.V.I.S. executable with PyInstaller.

    python build.py                 # one-file windowed build  ->  dist/Jarvis.exe on Windows
    python build.py --install       # pip-install requirements first
    python build.py --onedir        # folder build (starts faster, easier to inspect)
    python build.py --console       # keep a console window for debugging
    python build.py --skip-tests    # don't run the unit tests before building

PyInstaller is not a cross-compiler: run this on Windows to get Jarvis.exe
(on Linux/macOS it produces a native dist/Jarvis binary instead). The GitHub
Actions workflow in .github/workflows/build.yml builds Jarvis.exe on a Windows runner.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
NAME = "Jarvis"
IS_WIN = sys.platform == "win32"

RUNTIME_MODULES = {
    "webview": "pywebview",
    "ollama": "ollama",
    "edge_tts": "edge-tts",
    "pygame": "pygame",
    "speech_recognition": "SpeechRecognition",
    "pyaudio": "PyAudio",
    "numpy": "numpy",
    "psutil": "psutil",
    "onnxruntime": "onnxruntime",
    "PyInstaller": "pyinstaller",
}

# Big packages that may sit in the build environment but are never used by the app.
EXCLUDES = [
    "tkinter", "_tkinter", "matplotlib", "scipy", "pandas", "IPython", "notebook", "jupyter",
    "pytest", "playwright", "lameenc", "PIL.ImageQt", "PySide2", "PySide6", "PyQt5",
    "faster_whisper", "ctranslate2", "vosk", "torch",
]


def banner(text: str) -> None:
    print(f"\n\033[96m== {text}\033[0m" if sys.stdout.isatty() else f"\n== {text}", flush=True)


def app_version() -> str:
    namespace: dict = {}
    exec((ROOT / "core" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return namespace["APP_VERSION"]


def check_python() -> None:
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9 or newer is required.")
    print(f"Python {platform.python_version()} on {platform.system()} {platform.machine()}")


def install_requirements() -> None:
    banner("Installing requirements")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])


def check_modules() -> None:
    banner("Checking dependencies")
    missing = [pkg for mod, pkg in RUNTIME_MODULES.items() if importlib.util.find_spec(mod) is None]
    for mod, pkg in RUNTIME_MODULES.items():
        print(f"  {'ok ' if pkg not in missing else 'MISSING'}  {pkg}")
    if missing:
        sys.exit(f"\nMissing packages: {', '.join(missing)}\nRun:  python build.py --install")
    if not IS_WIN and sys.platform != "darwin":
        if not any(importlib.util.find_spec(m) for m in ("qtpy", "gi")):
            sys.exit("pywebview needs a GUI backend on Linux: pip install pywebview[qt]  (or PyGObject + WebKit2GTK)")


def run_tests() -> None:
    if importlib.util.find_spec("pytest") is None:
        print("pytest not installed; skipping unit tests (pip install -r requirements-dev.txt)")
        return
    banner("Running unit tests")
    env = {**os.environ, "SDL_AUDIODRIVER": "dummy"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", str(ROOT / "tests")], cwd=ROOT, env=env)
    if result.returncode != 0:
        sys.exit("Unit tests failed; fix them or pass --skip-tests.")


def ensure_icon() -> Path | None:
    ico, png = ROOT / "assets" / "jarvis.ico", ROOT / "assets" / "jarvis.png"
    if not (ico.exists() and png.exists()) and importlib.util.find_spec("PIL"):
        subprocess.run([sys.executable, str(ROOT / "assets" / "make_icon.py")], check=False)
    return ico if ico.exists() else (png if png.exists() else None)


def write_version_file(version: str) -> Path:
    parts = [int(p) for p in (version.split(".") + ["0", "0", "0"])[:4]]
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(parts)}, prodvers={tuple(parts)}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'JARVIS Project'),
      StringStruct('FileDescription', 'J.A.R.V.I.S. Desktop Voice Assistant'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{NAME}'),
      StringStruct('OriginalFilename', '{NAME}.exe'),
      StringStruct('ProductName', 'J.A.R.V.I.S.'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    BUILD.mkdir(exist_ok=True)
    path = BUILD / "version_info.txt"
    path.write_text(text, encoding="utf-8")
    return path


def pyinstaller_args(args: argparse.Namespace, icon: Path | None, version: str) -> list[str]:
    sep = os.pathsep
    cli = [
        str(ROOT / "app.py"),
        "--name", NAME,
        "--noconfirm",
        "--clean",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
        "--onedir" if args.onedir else "--onefile",
        "--add-data", f"{ROOT / 'web'}{sep}web",
        "--add-data", f"{ROOT / 'assets' / 'jarvis.png'}{sep}assets",
        # Windows needs a real .ico for the window icon (WinForms rejects PNGs and crashes)
        "--add-data", f"{ROOT / 'assets' / 'jarvis.ico'}{sep}assets",
        "--add-data", f"{ROOT / 'assets' / 'wakeword'}{sep}assets/wakeword",
        "--additional-hooks-dir", str(ROOT / "hooks"),
        "--hidden-import", "pyaudio",
        "--collect-submodules", "core",
    ]
    if not args.console:
        cli.append("--windowed")
    if icon is not None and (IS_WIN or sys.platform == "darwin"):
        cli += ["--icon", str(icon)]
    if IS_WIN:
        cli += ["--version-file", str(write_version_file(version))]
    splash = ROOT / "assets" / "splash.png"
    if IS_WIN and not args.no_splash and splash.exists():
        if importlib.util.find_spec("tkinter"):  # the bootloader splash bundles its own minimal Tcl/Tk
            cli += ["--splash", str(splash)]
        else:
            print("tkinter is not available; building without the start-up splash screen")
    for mod in EXCLUDES:
        cli += ["--exclude-module", mod]
    return cli


def output_path(onedir: bool) -> Path:
    exe = f"{NAME}.exe" if IS_WIN else NAME
    return DIST / NAME / exe if onedir else DIST / exe


def selftest(exe: Path) -> bool:
    banner("Self-testing the build")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "selftest.json"
        env = {**os.environ, "JARVIS_HOME": tmp, "SDL_AUDIODRIVER": "dummy"}
        try:
            proc = subprocess.run([str(exe), "--selftest", "--selftest-report", str(report)],
                                  env=env, timeout=240, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            print("Self-test timed out.")
            return False
        if not report.exists():
            print(f"No self-test report (exit code {proc.returncode}).\n{proc.stdout}\n{proc.stderr}")
            return False
        data = json.loads(report.read_text(encoding="utf-8"))
    for name, result in data["checks"].items():
        mark = "PASS" if result["ok"] else ("info" if name == "ollama_probe" else "FAIL")
        print(f"  {mark:4}  {name:15} {json.dumps(result['detail'])[:110]}")
    return bool(data.get("ok")) and proc.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the J.A.R.V.I.S. executable")
    parser.add_argument("--install", action="store_true", help="pip install -r requirements.txt first")
    parser.add_argument("--onedir", action="store_true", help="build a folder instead of a single file")
    parser.add_argument("--console", action="store_true", help="keep the console window (debugging)")
    parser.add_argument("--skip-tests", action="store_true", help="skip the unit tests")
    parser.add_argument("--no-selftest", action="store_true", help="skip running the built executable's self-test")
    parser.add_argument("--no-splash", action="store_true", help="don't show a splash screen while the exe unpacks")
    args = parser.parse_args()

    os.chdir(ROOT)
    banner("J.A.R.V.I.S. build")
    check_python()
    if args.install:
        install_requirements()
    check_modules()
    if not args.skip_tests:
        run_tests()
    if not IS_WIN:
        print("\nNote: PyInstaller builds for the platform it runs on. Run this on Windows to produce Jarvis.exe;"
              " here it will produce a native executable.")

    version = app_version()
    icon = ensure_icon()
    cli = pyinstaller_args(args, icon, version)

    banner(f"PyInstaller ({'onedir' if args.onedir else 'onefile'}, v{version})")
    started = time.time()
    import PyInstaller.__main__

    PyInstaller.__main__.run(cli)

    exe = output_path(args.onedir)
    if not exe.exists():
        print(f"Build failed: {exe} was not created.")
        return 1
    size = sum(f.stat().st_size for f in exe.parent.rglob("*")) if args.onedir else exe.stat().st_size
    print(f"\nBuilt {exe} ({size / 1024 / 1024:.1f} MB) in {time.time() - started:.0f}s")

    if not args.no_selftest and not selftest(exe):
        print("\nSelf-test FAILED.")
        return 1
    banner("Done")
    print(f"Executable: {exe}")
    print("Before first launch: install Ollama (https://ollama.com/download) and run `ollama run llama3.2`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
