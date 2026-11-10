# =====================================================
# MadOut2 Tools — все Blender-инструменты MadOut2 одним аддоном.
# Вкладка «MadOut2» в боковой панели 3D View (N).
#
# Каждый инструмент — свой модуль со своими register()/unregister().
# Новый инструмент: положить .py в эту папку, добавить в TOOLS,
# у панели поставить bl_category = "MadOut2" и bl_order.
#
# Ставится и обновляется как Blender Extension по ссылке —
# см. README.md в корне репозитория.
# =====================================================

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


def register():
    for tool in TOOLS:
        try:
            tool.register()
        except Exception as e:
            # один сломанный инструмент не должен ронять остальные
            print(f"[MadOut2 Tools] {tool.__name__}: ошибка регистрации: {e}")


def unregister():
    for tool in reversed(TOOLS):
        try:
            tool.unregister()
        except Exception as e:
            print(f"[MadOut2 Tools] {tool.__name__}: ошибка отключения: {e}")
