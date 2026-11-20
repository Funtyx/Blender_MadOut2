# =====================================================
# Вкладка «MadOut2 V2» в боковой панели 3D View (N):
# кнопки инструментов с параметрами и настройки Big Edge Length.
# Параметры хранятся в сцене и передаются инструменту при нажатии;
# после запуска их можно докрутить в F9.
# =====================================================

import bpy
from bpy.props import BoolProperty, PointerProperty

from . import big_edge_length, ground_fill, strip_resample
from .ui_help import draw_help

SR = strip_resample.MESH_OT_strip_resample
GF = ground_fill.MESH_OT_ground_fill
SR_KEYS = ("mode", "count", "spacing", "smooth", "keep_corners", "corner_angle")
GF_KEYS = ("rows", "extra_along", "merge_along", "bulge", "shape", "bias", "end_taper", "flip", "uv_strip", "uv_scale")


class MADOUT2V2_Settings(bpy.types.PropertyGroup):
    pass


def _build_settings():
    # Параметры панели — те же определения, что у операторов, чтобы не расходились
    ann = {}
    ann.update({"sr_" + k: SR.__annotations__[k] for k in SR_KEYS})
    ann.update({"gf_" + k: GF.__annotations__[k] for k in GF_KEYS})
    ann["show_sr"] = BoolProperty(name="Параметры Strip Resample", default=True)
    ann["show_gf"] = BoolProperty(name="Параметры Ground Fill", default=False)
    ann["help_sr"] = BoolProperty(name="Помощь", default=False)
    ann["help_gf"] = BoolProperty(name="Помощь", default=False)
    MADOUT2V2_Settings.__annotations__ = ann


def _tool_box(layout, s, show_prop, op_cls, icon):
    box = layout.box()
    row = box.row(align=True)
    is_open = getattr(s, show_prop)
    row.prop(s, show_prop, text="", icon='TRIA_DOWN' if is_open else 'TRIA_RIGHT', emboss=False)
    op = row.operator(op_cls.bl_idname, icon=icon)
    return box, op, is_open


class MADOUT2V2_PT_tools(bpy.types.Panel):
    bl_label = "Mesh Tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2 V2"
    bl_order = 0

    def draw(self, context):
        s = context.scene.madout2_v2
        layout = self.layout
        layout.enabled = context.mode == 'EDIT_MESH'
        if context.mode != 'EDIT_MESH':
            layout.label(text="Только в Edit Mode", icon='INFO')

        box, op, is_open = _tool_box(layout, s, "show_sr", SR, 'CENTER_ONLY')
        for k in SR_KEYS:
            setattr(op, k, getattr(s, "sr_" + k))
        if is_open:
            col = box.column()
            col.row().prop(s, "sr_mode", expand=True)
            if s.sr_mode == 'COUNT':
                col.prop(s, "sr_count", text="Сегментов (0 = как сейчас)")
            else:
                col.prop(s, "sr_spacing")
            col.prop(s, "sr_smooth")
            row = col.row(align=True)
            row.prop(s, "sr_keep_corners")
            sub = row.row()
            sub.active = s.sr_keep_corners
            sub.prop(s, "sr_corner_angle", text="")
            draw_help(box, s, "help_sr", strip_resample.HELP)

        box, op, is_open = _tool_box(layout, s, "show_gf", GF, 'MOD_DISPLACE')
        for k in GF_KEYS:
            setattr(op, k, getattr(s, "gf_" + k))
        if is_open:
            col = box.column()
            col.prop(s, "gf_rows")
            col.prop(s, "gf_extra_along")
            col.prop(s, "gf_merge_along")
            col.separator()
            col.prop(s, "gf_bulge")
            col.prop(s, "gf_shape")
            col.prop(s, "gf_bias")
            col.prop(s, "gf_end_taper")
            col.separator()
            col.prop(s, "gf_flip")
            col.prop(s, "gf_uv_strip")
            sub = col.column()
            sub.enabled = s.gf_uv_strip
            sub.prop(s, "gf_uv_scale")
            draw_help(box, s, "help_gf", ground_fill.HELP)


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
