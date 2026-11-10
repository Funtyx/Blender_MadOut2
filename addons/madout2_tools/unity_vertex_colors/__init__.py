bl_info = {
    "name": "MadOut Unity Vertex Colors",
    "author": "MadOut Tools",
    "version": (1, 6, 0),
    "blender": (3, 6, 0),
    "location": "3D View > Sidebar (N) > MadOut",
    "category": "Import-Export",
}

import importlib.util
import json
import tempfile
from pathlib import Path
import bpy
from bpy.props import StringProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper


def load_matcher():
    path = Path(__file__).with_name("vertex_color_match.py")
    specification = importlib.util.spec_from_file_location("madout_vertex_color_match", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def load_bridge():
    specification = importlib.util.spec_from_file_location(__name__ + ".vertex_color_bridge", Path(__file__).with_name("vertex_color_bridge.py"))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


BRIDGE = load_bridge()


# Настройки живут у корневого аддона madout2_tools
ADDON_ID = __name__.partition(".")[0]


def preferences(context):
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon is not None else None


def save_preferences(self, context):
    if context is not None and bpy.ops.wm.save_userpref.poll():
        bpy.ops.wm.save_userpref()


class MADOUT_Preferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID
    exchange_folder: StringProperty(name="Папка обмена JSON", subtype='DIR_PATH',
                                   default=str(Path(tempfile.gettempdir()) / "MadoutVertexPaintBridge"), update=save_preferences)
    mesh_folder: StringProperty(name="Папка FBX-мешей", subtype='DIR_PATH', update=save_preferences)
    show_settings: BoolProperty(name="Настройки", default=False)

    def draw(self, context):
        self.layout.prop(self, "exchange_folder")
        self.layout.prop(self, "mesh_folder")


class MADOUT_OT_export_mesh(bpy.types.Operator):
    bl_idname = "madout.export_vertex_mesh"
    bl_label = "Экспортировать FBX"
    bl_description = "Экспорт выделенного меша в постоянную папку Unity; существующий FBX обновляется"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode in {'OBJECT', 'VERTEX_PAINT'}

    def execute(self, context):
        try:
            settings = preferences(context)
            if settings is None:
                raise ValueError("Установите аддон из ZIP, чтобы сохранить постоянные настройки")
            specification = importlib.util.spec_from_file_location(__name__ + ".vertex_color_export", Path(__file__).with_name("vertex_color_export.py"))
            exporter = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(exporter)
            path = exporter.export(context, settings.mesh_folder)
            self.report({'INFO'}, "FBX сохранён: " + str(path))
            return {'FINISHED'}
        except Exception as exception:
            self.report({'ERROR'}, str(exception))
            return {'CANCELLED'}


class MADOUT_OT_import_vertex_colors(bpy.types.Operator, ImportHelper):
    bl_idname = "madout.import_vertex_colors"
    bl_label = "Выбрать JSON и перенести цвета"
    bl_description = "Выберите JSON из Unity: цвета сразу перенесутся на выбранный меш"
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode in {'OBJECT', 'VERTEX_PAINT'}

    def draw(self, context):
        # No file-browser options: UV0, Color and matching are automatic.
        self.layout.label(text="Цвета → выбранный меш")

    def execute(self, context):
        previous_mode = 'OBJECT'
        obj = context.active_object
        try:
            obj = context.active_object
            if obj is None or obj.type != 'MESH' or obj.mode not in {'OBJECT', 'VERTEX_PAINT'}:
                raise ValueError("Выберите исходный меш в Object Mode или Vertex Paint")
            previous_mode = obj.mode
            if previous_mode == 'VERTEX_PAINT':
                bpy.ops.object.mode_set(mode='OBJECT')
            mesh = obj.data
            if not mesh.uv_layers:
                raise ValueError("У выбранного меша нет UV-карты")
            # Unity UV0 corresponds to the first exported UV layer.
            layer = mesh.uv_layers[0]
            with open(bpy.path.abspath(self.filepath), encoding='utf-8-sig') as stream:
                data = json.load(stream)
            if 'mesh_data' in data:
                data = data['mesh_data']
            if data.get('version') not in (1, 2) or data.get('color_space') != 'raw_linear':
                raise ValueError("Нужен JSON, экспортированный из Unity Vertex Painter")
            corners = [(tuple(mesh.vertices[loop.vertex_index].co), tuple(layer.data[loop.index].uv))
                       for loop in mesh.loops]
            # Modifiers are not an automatic error: the matcher validates the original geometry.
            matcher = load_matcher()
            if data.get('triangles'):
                faces = [list(polygon.loop_indices) for polygon in mesh.polygons]
                colors, orientation = matcher.match_topology(data['vertices'], data['triangles'], corners, faces)
            else:
                try:
                    colors, orientation = matcher.match_colors(data['vertices'], corners)
                except ValueError as exception:
                    raise ValueError("Старый JSON не содержит грани меша. Экспортируйте JSON заново обновлённым Unity Vertex Painter") from exception
            # Matching finishes before any data changes; linked objects get a private mesh copy.
            if mesh.users > 1:
                mesh = mesh.copy()
                obj.data = mesh
            attribute = mesh.color_attributes.get("Color")
            if attribute is not None and (attribute.domain != 'CORNER' or attribute.data_type != 'FLOAT_COLOR'):
                mesh.color_attributes.remove(attribute)
                attribute = None
            if attribute is None:
                attribute = mesh.color_attributes.new(name="Color", type='FLOAT_COLOR', domain='CORNER')
            attribute.data.foreach_set('color', [value for color in colors for value in color])
            # active_color is read-only in Blender versions; choose by writable index instead.
            attribute_index = next(index for index, item in enumerate(mesh.color_attributes)
                                   if item.name == attribute.name)
            mesh.color_attributes.active_color_index = attribute_index
            mesh.color_attributes.render_color_index = attribute_index
            mesh.update()
            for area in context.screen.areas if context.screen else ():
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
            self.report({'INFO'}, f"Цвета перенесены в {obj.name} → Color. Сохраните .blend")
            return {'FINISHED'}
        except Exception as exception:
            self.report({'ERROR'}, str(exception))
            return {'CANCELLED'}
        finally:
            if previous_mode == 'VERTEX_PAINT' and obj is not None and context.active_object == obj and obj.mode == 'OBJECT':
                bpy.ops.object.mode_set(mode='VERTEX_PAINT')


class MADOUT_OT_toggle_bridge(bpy.types.Operator):
    bl_idname = "madout.toggle_vertex_bridge"
    bl_label = "Принимать из Unity"

    def execute(self, context):
        if BRIDGE.enabled():
            BRIDGE.stop()
        else:
            BRIDGE.start(context)
        return {'FINISHED'}


class MADOUT_PT_vertex_colors(bpy.types.Panel):
    bl_label = "Unity Vertex Colors"
    bl_idname = "MADOUT_PT_vertex_colors"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_order = 6
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        valid = obj is not None and obj.type == 'MESH' and obj.mode in {'OBJECT', 'VERTEX_PAINT'}
        if valid:
            layout.label(text=obj.name, icon='MESH_DATA')
        else:
            layout.label(text="Выберите Mesh: Object / Vertex Paint", icon='INFO')
        layout.operator(MADOUT_OT_toggle_bridge.bl_idname,
                        text="Отключить мост" if BRIDGE.enabled() else "Принимать из Unity",
                        icon='CHECKBOX_HLT' if BRIDGE.enabled() else 'CHECKBOX_DEHLT',
                        depress=BRIDGE.enabled())
        if BRIDGE.enabled():
            layout.label(text="● Мост включён · приём в выделенный меш", icon='LINKED')
        layout.label(text=BRIDGE.status())
        layout.separator()
        settings = preferences(context)
        if settings is not None:
            exists = False
            if valid and settings.mesh_folder:
                import re
                name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', obj.name).strip(' .')
                exists = (Path(bpy.path.abspath(settings.mesh_folder)) / (name + '.fbx')).is_file()
            row = layout.row()
            row.scale_y = 1.5
            row.operator(MADOUT_OT_export_mesh.bl_idname, text="Обновить FBX" if exists else "Экспортировать FBX", icon='EXPORT')
            layout.prop(settings, "show_settings", icon='TRIA_DOWN' if settings.show_settings else 'TRIA_RIGHT', emboss=False)
            if settings.show_settings:
                box = layout.box()
                row = box.row()
                row.enabled = not BRIDGE.enabled()
                row.prop(settings, "exchange_folder")
                box.prop(settings, "mesh_folder")
                box.label(text="Unity: -Z Forward / Y Up / Scale 1", icon='INFO')
                box.label(text="Модификаторы: ON · Vertex Colors: Linear")


def import_menu(self, context):
    self.layout.operator(MADOUT_OT_import_vertex_colors.bl_idname, text="Unity Vertex Colors (.json)")


CLASSES = (MADOUT_Preferences, MADOUT_OT_import_vertex_colors, MADOUT_OT_toggle_bridge, MADOUT_OT_export_mesh, MADOUT_PT_vertex_colors)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    BRIDGE.stop()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
