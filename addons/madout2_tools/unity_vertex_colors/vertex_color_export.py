"""FBX export of the active mesh using the project's Unity preset."""
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
import bpy


def target_path(context, folder):
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        raise ValueError("Выберите меш для экспорта")
    if not folder:
        raise ValueError("Укажите папку мешей в настройках")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', obj.name).strip(' .')
    if not name:
        raise ValueError("Некорректное имя объекта")
    return Path(bpy.path.abspath(folder)).expanduser() / (name + '.fbx')


def export(context, folder):
    path = target_path(context, folder)
    obj = context.active_object
    mode = obj.mode
    if mode not in {'OBJECT', 'VERTEX_PAINT'}:
        raise ValueError("Экспорт доступен в Object Mode и Vertex Paint")
    selection = list(context.selected_objects)
    temporary_destination = None
    try:
        if mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for selected in selection:
            selected.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='madout_fbx_') as folder:
            exported = Path(folder) / path.name
            result = bpy.ops.export_scene.fbx(
                filepath=str(exported), check_existing=False, use_selection=True,
                global_scale=1.0, apply_unit_scale=True, apply_scale_options='FBX_SCALE_UNITS',
                axis_forward='-Z', axis_up='Y', use_space_transform=True,
                bake_space_transform=True, use_mesh_modifiers=True, mesh_smooth_type='OFF',
                use_mesh_edges=False, use_triangles=False, use_tspace=False,
                colors_type='LINEAR', prioritize_active_color=True,
                use_custom_props=False, add_leaf_bones=True, primary_bone_axis='Y',
                secondary_bone_axis='X', armature_nodetype='NULL', use_armature_deform_only=False,
                bake_anim=False, path_mode='AUTO', batch_mode='OFF', embed_textures=False)
            if result != {'FINISHED'} or not exported.is_file():
                raise ValueError("Blender не создал FBX")
            # Unity ignores the dot-prefixed temporary file; it sees only the finished FBX.
            temporary_destination = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
            shutil.copyfile(exported, temporary_destination)
            os.replace(temporary_destination, path)
        return path
    finally:
        if temporary_destination is not None and temporary_destination.exists():
            temporary_destination.unlink()
        obj.select_set(False)
        for selected in selection:
            selected.select_set(True)
        context.view_layer.objects.active = obj
        if mode == 'VERTEX_PAINT' and obj.mode == 'OBJECT':
            bpy.ops.object.mode_set(mode='VERTEX_PAINT')
