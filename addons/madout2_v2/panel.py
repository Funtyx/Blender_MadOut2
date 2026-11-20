# =====================================================
# Вкладка «MadOut2 V2» в боковой панели 3D View (N):
# кнопки инструментов и настройки Big Edge Length.
# =====================================================

import bpy

from . import big_edge_length, ground_fill, strip_resample


class MADOUT2V2_PT_tools(bpy.types.Panel):
    bl_label = "Mesh Tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2 V2"
    bl_order = 0

    def draw(self, context):
        col = self.layout.column(align=True)
        col.enabled = context.mode == 'EDIT_MESH'
        col.operator(strip_resample.MESH_OT_strip_resample.bl_idname, icon='CENTER_ONLY')
        col.operator(ground_fill.MESH_OT_ground_fill.bl_idname, icon='MOD_DISPLACE')
        if context.mode != 'EDIT_MESH':
            self.layout.label(text="Только в Edit Mode", icon='INFO')
        else:
            self.layout.label(text="Выделите линии, настройка — F9", icon='INFO')


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


_CLASSES = (MADOUT2V2_PT_tools, MADOUT2V2_PT_edge_length)


def register():
    for c in _CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_CLASSES):
        bpy.utils.unregister_class(c)
