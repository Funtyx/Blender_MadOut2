# =====================================================
# MadOut2 Tools — все Blender-инструменты MadOut2 одним аддоном.
# Вкладка «MadOut2» в боковой панели 3D View (N).
#
# Каждый инструмент — свой модуль со своими register()/unregister().
# Новый инструмент: положить .py в эту папку, добавить в TOOLS,
# у панели поставить bl_category = "MadOut2" и bl_order.
#
# Обновление: папка — клон приватного github.com/Funtyx/Blender_MadOut2.
# Панель «Обновление» → «Обновить с GitHub» (git pull + перезагрузка).
# Подробно — README.md в корне репозитория.
# =====================================================

bl_info = {
    "name": "MadOut2 Tools",
    "author": "MadOut2",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > MadOut2",
    "description": "Все инструменты MadOut2: экспорт модулей, бордюры, инсет, "
                   "декали, вертекс-колоры, мост с Unity",
    "category": "MadOut2",
}

import os
import subprocess
import time

import bpy

from . import (
    one_side_inset,
    edge_outline,
    curb,
    surface_decal,
    vc_blend,
    unity_vertex_colors,
    module_export,
)

# Порядок регистрации (порядок панелей во вкладке задаёт bl_order у панелей)
TOOLS = (
    one_side_inset,
    edge_outline,
    curb,
    surface_decal,
    vc_blend,
    unity_vertex_colors,
    module_export,
)

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def _files_stamp():
    """Самое свежее время изменения .py в пакете — по нему видно, что
    после git pull на диске уже новая версия."""
    newest = 0.0
    for root, dirs, files in os.walk(_PACKAGE_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, f)))
                except OSError:
                    pass
    return newest


def _git_commit():
    """'a1b2c3d · 2026-11-10' последнего коммита с инструментами, или ''."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h · %cd", "--date=short", "--", "."],
            cwd=_PACKAGE_DIR, capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip()
    except Exception:
        return ""


_STATE = {"loaded": 0.0, "commit": "", "checked_at": 0.0, "on_disk": 0.0}


def _update_available():
    # draw() зовётся часто — диск проверяем не чаще раза в 5 секунд
    now = time.monotonic()
    if now - _STATE["checked_at"] > 5.0:
        _STATE["checked_at"] = now
        _STATE["on_disk"] = _files_stamp()
    return _STATE["on_disk"] > _STATE["loaded"] + 1.0


def _git(*args, timeout=60):
    # GIT_TERMINAL_PROMPT=0: без сохранённого входа git сразу падает с ошибкой,
    # а не ждёт ввода логина в невидимом терминале (Blender бы завис)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", *args], cwd=_PACKAGE_DIR, capture_output=True,
                          text=True, timeout=timeout, env=env)


def _pull_error_hint(stderr):
    text = stderr.lower()
    if "could not read username" in text or "authentication" in text or "403" in text:
        return "Нет входа в GitHub. Mac: в терминале gh auth login, потом gh auth setup-git. " \
               "Windows: сделайте git pull в папке инструментов — откроется окно входа"
    if "not possible to fast-forward" in text or "diverging" in text or "would be overwritten" in text:
        return "В папке инструментов есть свои правки — git не может обновиться поверх них"
    if "could not resolve host" in text or "unable to access" in text:
        return "Нет связи с GitHub — проверьте интернет"
    return stderr.strip().splitlines()[-1] if stderr.strip() else "неизвестная ошибка"


class MADOUT2_OT_update_tools(bpy.types.Operator):
    """Скачать свежую версию инструментов с GitHub и перезагрузить их"""
    bl_idname = "madout2.update_tools"
    bl_label = "Обновить"

    def execute(self, context):
        try:
            before = _git("rev-parse", "HEAD", timeout=5).stdout.strip()
            result = _git("pull", "--ff-only")
        except FileNotFoundError:
            self.report({'ERROR'}, "git не установлен")
            return {'CANCELLED'}
        except subprocess.TimeoutExpired:
            self.report({'ERROR'}, "GitHub не ответил за минуту — попробуйте ещё раз")
            return {'CANCELLED'}
        if result.returncode != 0:
            self.report({'ERROR'}, "Не обновилось: " + _pull_error_hint(result.stderr))
            return {'CANCELLED'}
        after = _git("rev-parse", "HEAD", timeout=5).stdout.strip()
        if before == after:
            self.report({'INFO'}, "Уже последняя версия")
            return {'FINISHED'}
        self.report({'INFO'}, "Обновлено — перезагружаю инструменты")
        bpy.ops.script.reload()
        return {'FINISHED'}


class MADOUT2_OT_reload_tools(bpy.types.Operator):
    """Перезагрузить скрипты Blender — подхватить новую версию инструментов"""
    bl_idname = "madout2.reload_tools"
    bl_label = "Перезагрузить инструменты"

    def execute(self, context):
        bpy.ops.script.reload()
        return {'FINISHED'}


class MADOUT2_OT_open_tools_folder(bpy.types.Operator):
    """Открыть папку с инструментами"""
    bl_idname = "madout2.open_tools_folder"
    bl_label = "Открыть папку"

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=_PACKAGE_DIR)
        return {'FINISHED'}


class MADOUT2_PT_update(bpy.types.Panel):
    bl_label = "Обновление"
    bl_idname = "MADOUT2_PT_update"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_order = 100
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        if _update_available():
            self.layout.label(icon='ERROR')

    def draw(self, context):
        layout = self.layout
        ver = ".".join(map(str, bl_info["version"]))
        col = layout.column(align=True)
        col.label(text=f"MadOut2 Tools v{ver}", icon='TOOL_SETTINGS')
        if _STATE["commit"]:
            col.label(text=f"Коммит: {_STATE['commit']}", icon='FILE_REFRESH')

        if _update_available():
            box = layout.box()
            box.alert = True
            box.label(text="На диске новая версия", icon='ERROR')
        row = layout.row(align=True)
        row.scale_y = 1.5
        row.operator("madout2.update_tools", text="Обновить с GitHub", icon='IMPORT')
        row.operator("madout2.open_tools_folder", text="", icon='FILE_FOLDER')
        layout.operator("madout2.reload_tools", icon='FILE_REFRESH')


_OWN_CLASSES = (
    MADOUT2_OT_update_tools,
    MADOUT2_OT_reload_tools,
    MADOUT2_OT_open_tools_folder,
    MADOUT2_PT_update,
)


def register():
    for tool in TOOLS:
        try:
            tool.register()
        except Exception as e:
            # один сломанный инструмент не должен ронять остальные
            print(f"[MadOut2 Tools] {tool.__name__}: ошибка регистрации: {e}")
    for cls in _OWN_CLASSES:
        bpy.utils.register_class(cls)
    _STATE["loaded"] = _files_stamp()
    _STATE["commit"] = _git_commit()


def unregister():
    for cls in reversed(_OWN_CLASSES):
        bpy.utils.unregister_class(cls)
    for tool in reversed(TOOLS):
        try:
            tool.unregister()
        except Exception as e:
            print(f"[MadOut2 Tools] {tool.__name__}: ошибка отключения: {e}")
