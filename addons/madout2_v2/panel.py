# =====================================================
# Вкладка «MadOut2 V2» в боковой панели 3D View (N):
# у каждого инструмента параметры, «Предпросмотр» и «Применить».
# Параметры хранятся в сцене и передаются инструменту при нажатии;
# после применения их можно докрутить в F9.
# =====================================================

import textwrap

import bpy
from bpy.props import BoolProperty, PointerProperty

from . import big_edge_length, ground_fill, preview, stairs, strip_resample
from .ui_help import draw_help

SR = strip_resample.MESH_OT_strip_resample
GF = ground_fill.MESH_OT_ground_fill
ST = stairs.MESH_OT_stairs_from_face
PREVIEWS = ("preview_sr", "preview_gf", "preview_st")


def _only_one_preview(owner):
    def update(self, context):
        if getattr(self, owner):
            for other in PREVIEWS:
                if other != owner and getattr(self, other):
                    setattr(self, other, False)
    return update


class MADOUT2V2_Settings(bpy.types.PropertyGroup):
    pass


def _build_settings():
    # Параметры панели — те же определения, что у операторов, чтобы не расходились
    ann = {}
    ann.update({"sr_" + k: SR.__annotations__[k] for k in strip_resample.PARAM_KEYS})
    ann.update({"gf_" + k: GF.__annotations__[k] for k in ground_fill.PARAM_KEYS})
    ann.update({"st_" + k: ST.__annotations__[k] for k in stairs.PARAM_KEYS})
    for key, title in (("sr", "Strip Resample"), ("gf", "Ground Fill"), ("st", "Stairs")):
        ann["show_" + key] = BoolProperty(name="Параметры " + title, default=True)
        ann["help_" + key] = BoolProperty(name="Помощь", default=False)
        ann["preview_" + key] = BoolProperty(
            name="Предпросмотр", default=False, update=_only_one_preview("preview_" + key),
            description="Показывать результат сразу, пока меняете выделение и параметры. Меш не меняется до «Применить»")
    MADOUT2V2_Settings.__annotations__ = ann


def _wrapped(layout, text, icon):
    lines = textwrap.wrap(text, 42) or [""]
    col = layout.column(align=True)
    col.scale_y = 0.8
    for i, line in enumerate(lines):
        col.label(text=line, icon=icon if i == 0 else 'BLANK1')


def _tool(layout, s, key, title, op_cls, keys, draw_params, help_lines, count_text=None):
    box = layout.box()
    is_open = getattr(s, "show_" + key)
    box.prop(s, "show_" + key, text=title, icon='TRIA_DOWN' if is_open else 'TRIA_RIGHT', emboss=False)
    if is_open:
        if count_text:
            draw_params(box, s, key + "_", count_text)
        else:
            draw_params(box, s, key + "_")

    row = box.row(align=True)
    row.scale_y = 1.3
    row.prop(s, "preview_" + key, toggle=True, icon='HIDE_OFF')
    op = row.operator(op_cls.bl_idname, text="Применить", icon='CHECKMARK')
    for k in keys:
        setattr(op, k, getattr(s, key + "_" + k))

    if getattr(s, "preview_" + key):
        info, error = preview.status()
        if error:
            _wrapped(box, error, 'ERROR')
        elif info:
            _wrapped(box, info, 'INFO')
    if is_open:
        # Помощь живёт внутри секции инструмента и сворачивается вместе с ней
        draw_help(box, s, "help_" + key, help_lines)


class MADOUT2V2_PT_tools(bpy.types.Panel):
    bl_label = "Mesh Tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2 V2"
    bl_order = 0

    def draw(self, context):
        s = context.scene.madout2_v2
        layout = self.layout
        if context.mode != 'EDIT_MESH':
            layout.label(text="Работает в Edit Mode", icon='INFO')
        col = layout.column()
        col.enabled = context.mode == 'EDIT_MESH'
        _tool(col, s, "sr", "Strip Resample", SR, strip_resample.PARAM_KEYS,
              strip_resample.draw_params, strip_resample.HELP, count_text="Сегментов (0 = как сейчас)")
        _tool(col, s, "gf", "Ground Fill", GF, ground_fill.PARAM_KEYS,
              ground_fill.draw_params, ground_fill.HELP)
        _tool(col, s, "st", "Stairs", ST, stairs.PARAM_KEYS,
              stairs.draw_params, stairs.HELP)


class MADOUT2V2_PT_edge_length(bpy.types.Panel):
    bl_label = "Big Edge Length"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2 V2"
    bl_order = 1

    def draw(self, context):
        p = big_edge_length._prefs()
        if p is not None:
            big_edge_length._draw_settings(self.layout, p)


_CLASSES = (MADOUT2V2_Settings, MADOUT2V2_PT_tools, MADOUT2V2_PT_edge_length)


def register():
    _build_settings()
    for c in _CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.madout2_v2 = PointerProperty(type=MADOUT2V2_Settings)


def unregister():
    del bpy.types.Scene.madout2_v2
    for c in reversed(_CLASSES):
        bpy.utils.unregister_class(c)
