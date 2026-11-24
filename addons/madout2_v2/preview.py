# =====================================================
# Живой предпросмотр инструментов вкладки MadOut2 V2.
# Таймер следит за выделением и параметрами в панели; когда они
# меняются, инструмент прогоняется на копии меша, а результат
# рисуется линиями поверх вьюпорта. Сам меш не меняется до «Применить».
# =====================================================

import bmesh
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import fillet_sections, ground_fill, road_remesh, stairs, strip_resample

from mathutils import Vector

PREVIEW_COLOR = (0.15, 1.0, 0.55, 0.95)
SURFACE_COLOR = Vector((0.62, 0.66, 0.6))
LIGHT = Vector((0.35, -0.45, 0.82)).normalized()

_state = {"key": None, "lines": [], "batch": None, "tris": None, "tri_batch": None, "on_top": False,
          "error": "", "info": ""}
_handle = None

_TOOLS = {
    'SR': (strip_resample.StripResampleCore, strip_resample.PARAM_KEYS, "sr_", strip_resample._Fail),
    'FS': (fillet_sections.FilletSectionsCore, fillet_sections.PARAM_KEYS, "fs_", fillet_sections._Fail),
    'GF': (ground_fill.GroundFillCore, ground_fill.PARAM_KEYS, "gf_", ground_fill._Fail),
    'ST': (stairs.StairsCore, stairs.PARAM_KEYS, "st_", stairs._Fail),
    'RR': (road_remesh.RoadRemeshCore, road_remesh.PARAM_KEYS, "rr_", road_remesh._Fail),
}


def active_tool(s):
    if s.preview_sr:
        return 'SR'
    if s.preview_fs:
        return 'FS'
    if s.preview_rr:
        return 'RR'
    if s.preview_gf:
        return 'GF'
    if s.preview_st:
        return 'ST'
    return None


def status():
    return _state["info"], _state["error"]


def _core(tool, s):
    cls, keys, prefix, _ = _TOOLS[tool]
    core = cls()
    for k in keys:
        setattr(core, k, getattr(s, prefix + k))
    return core, tuple(getattr(s, prefix + k) for k in keys)


def _signature(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    acc = 0.0
    count = 0
    for v in bm.verts:
        if v.select:
            c = v.co
            acc += c.x * 1.31 + c.y * 2.71 + c.z * 3.17
            count += 1
    return obj.name, len(bm.verts), len(bm.edges), len(bm.faces), count, round(acc, 5)


def _surface(faces, mw, smooth):
    """Shaded triangles of the new faces: positions and per-vertex colours lit by a fixed light."""
    new = set(faces)
    nmat = mw.inverted().transposed().to_3x3()
    pos, col = [], []
    for f in faces:
        loops = list(f.loops)
        for j in range(1, len(loops) - 1):
            for l in (loops[0], loops[j], loops[j + 1]):
                n = l.vert.normal if smooth else f.normal
                if smooth:
                    # Only the new faces count, so the chain vertices are not bent by the old geometry
                    acc = sum((g.normal for g in l.vert.link_faces if g in new), Vector())
                    n = acc if acc.length > 1e-9 else n
                n = (nmat @ n).normalized()
                light = 0.38 + 0.62 * max(0.0, n.dot(LIGHT)) + 0.12 * max(0.0, -n.dot(LIGHT))
                c = SURFACE_COLOR * light
                pos.append(tuple(mw @ l.vert.co))
                col.append((c.x, c.y, c.z, 1.0))
    return pos, col


def _compute(obj, tool, s):
    """Returns (lines, triangles, error, info)."""
    core, _ = _core(tool, s)
    fail = _TOOLS[tool][3]
    bm = bmesh.from_edit_mesh(obj.data).copy()
    mw = obj.matrix_world
    try:
        faces = core._run(bm, obj)
        bm.normal_update()
        info = f"Будет {len(faces)} граней"
        if tool == 'GF':
            return [], _surface(faces, mw, s.gf_shade_smooth), "", info
        if tool == 'ST':
            return [], _surface(faces, mw, False), "", stairs.describe(core.summary)
        if tool == 'RR':
            info = road_remesh.describe(core.summary) + f", {len(faces)} граней"
        if tool == 'FS':
            info = fillet_sections.describe(core.summary) + f", {len(faces)} граней"
        edges = {e for f in faces for e in f.edges}
        lines = []
        for e in edges:
            lines.append(tuple(mw @ e.verts[0].co))
            lines.append(tuple(mw @ e.verts[1].co))
        return lines, None, "", info
    except fail as ex:
        return [], None, str(ex), ""
    except Exception as ex:
        return [], None, f"Ошибка предпросмотра: {ex}", ""
    finally:
        bm.free()


def _redraw():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type in {'VIEW_3D'}:
                area.tag_redraw()


def _set(lines, tris, error, info, on_top=False):
    _state.update(lines=lines, batch=None, tris=tris, tri_batch=None, on_top=on_top, error=error, info=info)
    _redraw()


def _timer():
    try:
        context = bpy.context
        s = getattr(context.scene, "madout2_v2", None) if context.scene else None
        tool = active_tool(s) if s else None
        obj = context.edit_object if context.mode == 'EDIT_MESH' else None
        if tool is None or obj is None or obj.type != 'MESH':
            if _state["key"] is not None:
                _state["key"] = None
                _set([], None, "", "")
            return 0.25
        _, params = _core(tool, s)
        key = (tool, params, _signature(obj), tuple(obj.matrix_world.col[3]))
        if key != _state["key"]:
            _state["key"] = key
            # Stairs replace the selected face, so the old face must not hide them in the preview
            _set(*_compute(obj, tool, s), on_top=(tool == 'ST'))
    except Exception as ex:
        _state["error"] = f"Ошибка предпросмотра: {ex}"
    return 0.15


def _draw():
    tris = _state["tris"]
    if tris and tris[0]:
        shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        if _state["tri_batch"] is None:
            _state["tri_batch"] = batch_for_shader(shader, 'TRIS', {"pos": tris[0], "color": tris[1]})
        if _state["on_top"]:
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.depth_mask_set(True)
            # own depth only: clear the scene depth so the old face and walls do not cover the stairs
            gpu.state.active_framebuffer_get().clear(depth=1.0)
        else:
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.depth_mask_set(True)
        gpu.state.face_culling_set('NONE')
        shader.bind()
        _state["tri_batch"].draw(shader)
        gpu.state.depth_mask_set(False)
        gpu.state.depth_test_set('NONE')

    lines = _state["lines"]
    if not lines:
        return
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    if _state["batch"] is None:
        _state["batch"] = batch_for_shader(shader, 'LINES', {"pos": lines})
    region = bpy.context.region
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    shader.bind()
    shader.uniform_float("viewportSize", (region.width, region.height))
    shader.uniform_float("lineWidth", 2.0)
    shader.uniform_float("color", PREVIEW_COLOR)
    _state["batch"].draw(shader)
    gpu.state.blend_set('NONE')


def register():
    global _handle
    _handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=0.5, persistent=True)


def unregister():
    global _handle
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    _state.update(key=None, lines=[], batch=None, tris=None, tri_batch=None, on_top=False, error="", info="")
