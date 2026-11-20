# Сборка новых версий аддонов из addons/ (MadOut2 Tools, MadOut2 V2) для установки по ссылке.
#
#   python3 publish.py
#
# Перед запуском поднимите version в addons/<аддон>/blender_manifest.toml.
# После — закоммитьте и запушьте папку repo/: у всех в Blender появится Update.
# Путь к Blender можно задать переменной окружения BLENDER.

import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ADDONS = os.path.join(ROOT, "addons")
REPO = os.path.join(ROOT, "repo")

DEFAULT_BLENDER = {
    "darwin": "/Applications/Blender.app/Contents/MacOS/Blender",
    "win32": r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
}


def blender(*args):
    exe = os.environ.get("BLENDER") or DEFAULT_BLENDER.get(sys.platform, "blender")
    subprocess.run([exe, "--factory-startup", "--command", "extension", *args], check=True)


def main():
    os.makedirs(REPO, exist_ok=True)
    # В репозитории лежит только последняя версия каждого аддона — старые архивы убираем
    for old in glob.glob(os.path.join(REPO, "*.zip")):
        os.remove(old)
    for manifest in sorted(glob.glob(os.path.join(ADDONS, "*", "blender_manifest.toml"))):
        blender("build", "--source-dir", os.path.dirname(manifest), "--output-dir", REPO)
    blender("server-generate", "--repo-dir", REPO)
    print("\nГотово. Закоммитьте и запушьте папку repo/.")


main()
