# Установка MadOut2 Tools — один раз на машине.
#
# Blender → вкладка Scripting → Open → этот файл → Run Script (▶).
# Или из терминала:  blender --python BlenderTools/install.py
#
# Что делает:
#   1. Добавляет эту папку в Preferences → File Paths → Script Directories
#      (Blender сам найдёт в ней addons/madout2_tools).
#   2. Выключает старые отдельные аддоны — они дублируют инструменты
#      и конфликтуют с новыми.
#   3. Включает «MadOut2 Tools» и сохраняет настройки.

import os

import addon_utils
import bpy

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ADDON = "madout2_tools"

# Старые аддоны, которые теперь входят в MadOut2 Tools
OLD_ADDONS = (
    "ModeuleExport",
    "one_side_inset",
    "edge_outline_addon",
    "surface_decal_projector",
    "vc_blend_converter",
    "curb_from_selected_edges_addon",
    "madout_unity_vertex_colors",
)


def install():
    paths = bpy.context.preferences.filepaths.script_directories
    same = [d for d in paths if os.path.normpath(bpy.path.abspath(d.directory)) == TOOLS_DIR]
    if not same:
        entry = paths.new()
        entry.name = "MadOut2"
        entry.directory = TOOLS_DIR
        print(f"[MadOut2] Script Directory добавлена: {TOOLS_DIR}")
    bpy.utils.refresh_script_paths()
    addon_utils.modules_refresh()

    for name in OLD_ADDONS:
        if addon_utils.check(name)[1]:
            addon_utils.disable(name, default_set=True)
            print(f"[MadOut2] Выключен старый аддон: {name}")

    if addon_utils.enable(ADDON, default_set=True) is None:
        raise RuntimeError("Не удалось включить madout2_tools — смотри ошибку выше в консоли")

    bpy.ops.wm.save_userpref()
    print("[MadOut2] Готово: вкладка MadOut2 в боковой панели 3D View (клавиша N)")


install()
