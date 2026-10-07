# Overrides the generic contrib hook, which bundles every platform's FLAC encoder plus
# 38 MB of PocketSphinx models. JARVIS only needs this platform's FLAC binary, which
# recognize_google() uses to compress audio before upload.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files

if sys.platform == "win32":
    _wanted = {"flac-win32.exe"}
elif sys.platform == "darwin":
    _wanted = {"flac-mac"}
else:
    _wanted = {"flac-linux-x86_64", "flac-linux-x86"}

datas = [
    (src, dest)
    for src, dest in collect_data_files("speech_recognition")
    if os.path.basename(src) in _wanted or os.path.basename(src) == "version.txt"
]
