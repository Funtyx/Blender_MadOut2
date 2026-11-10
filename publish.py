# Сборка новой версии MadOut2 Tools для установки по ссылке.
#
#   python3 publish.py
#
# Перед запуском поднимите version в addons/madout2_tools/blender_manifest.toml.
# После — закоммитьте и запушьте папку repo/: у всех в Blender появится Update.
# Путь к Blender можно задать переменной окружения BLENDER.

import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(ROOT, "addons", "madout2_tools")
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
    # В репозитории лежит только последняя версия — старые архивы убираем
    for old in glob.glob(os.path.join(REPO, "*.zip")):
        os.remove(old)
    blender("build", "--source-dir", SOURCE, "--output-dir", REPO)
    blender("server-generate", "--repo-dir", REPO)
    print("\nГотово. Закоммитьте и запушьте папку repo/.")


main()
