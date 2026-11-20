# =====================================================
# Живой предпросмотр Strip Resample и Ground Fill.
# Таймер следит за выделением и параметрами в панели; когда они
# меняются, инструмент прогоняется на копии меша, а результат
# рисуется линиями поверх вьюпорта. Сам меш не меняется до «Применить».
# =====================================================

import bmesh
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import ground_fill, strip_resample

PREVIEW_COLOR = (0.15, 1.0, 0.55, 0.95)

_state = {"key": None, "lines": [], "batch": None, "error": "", "info": ""}
_handle = None

_TOOLS = {
    'SR': (strip_resample.StripResampleCore, strip_resample.PARAM_KEYS, "sr_", strip_resample._Fail),
    'GF': (ground_fill.GroundFillCore, ground_fill.PARAM_KEYS, "gf_", ground_fill._Fail),
}


def active_tool(s):
    if s.preview_sr:
        return 'SR'
    if s.preview_gf:
        return 'GF'
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


def _compute(obj, tool, s):
    core, _ = _core(tool, s)
    fail = _TOOLS[tool][3]
    bm = bmesh.from_edit_mesh(obj.data).copy()
    mw = obj.matrix_world
    try:
        faces = core._run(bm, obj)
        edges = {e for f in faces for e in f.edges}
        lines = []
        for e in edges:
            lines.append(tuple(mw @ e.verts[0].co))
            lines.append(tuple(mw @ e.verts[1].co))
        return lines, "", f"Будет {len(faces)} граней"
    except fail as ex:
        return [], str(ex), ""
    except Exception as ex:
        return [], f"Ошибка предпросмотра: {ex}", ""
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


def _set(lines, error, info):
    _state.update(lines=lines, batch=None, error=error, info=info)
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
                _set([], "", "")
            return 0.25
        _, params = _core(tool, s)
        key = (tool, params, _signature(obj), tuple(obj.matrix_world.col[3]))
        if key != _state["key"]:
            _state["key"] = key
            _set(*_compute(obj, tool, s))
    except Exception as ex:
        _state["error"] = f"Ошибка предпросмотра: {ex}"
    return 0.15


def _draw():
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
    _state.update(key=None, lines=[], batch=None, error="", info="")
