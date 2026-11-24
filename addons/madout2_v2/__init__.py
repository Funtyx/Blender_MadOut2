# =====================================================
# MadOut2 V2 — новые инструменты моделирования MadOut2.
# Вкладка «MadOut2 V2» в боковой панели 3D View (N).
#
# Каждый инструмент — свой модуль со своими register()/unregister().
# Новый инструмент: положить .py в эту папку, добавить в TOOLS
# и кнопку/настройки в panel.py.
#
# Ставится и обновляется как Blender Extension по ссылке —
# см. README.md в корне репозитория.
# =====================================================

import sys

# При обновлении расширения Blender перезагружает только этот файл, а модули
# инструментов остаются старыми из кэша — выкидываем их, чтобы взялся новый код
for _name in [n for n in sys.modules if n.startswith(__name__ + ".")]:
    del sys.modules[_name]
    globals().pop(_name[len(__name__) + 1:], None)

from . import (
    ground_fill,
    strip_resample,
    fillet_sections,
    road_remesh,
    stairs,
    big_edge_length,
    panel,
    preview,
)

TOOLS = (
    ground_fill,
    strip_resample,
    fillet_sections,
    road_remesh,
    stairs,
    big_edge_length,
    panel,
    preview,
)


def register():
    for tool in TOOLS:
        try:
            tool.register()
        except Exception as e:
            # один сломанный инструмент не должен ронять остальные
            print(f"[MadOut2 V2] {tool.__name__}: ошибка регистрации: {e}")


def unregister():
    for tool in reversed(TOOLS):
        try:
            tool.unregister()
        except Exception as e:
            print(f"[MadOut2 V2] {tool.__name__}: ошибка отключения: {e}")
