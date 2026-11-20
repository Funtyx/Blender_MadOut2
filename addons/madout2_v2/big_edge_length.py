# =====================================================
# Big Edge Length — крупные подписи длин рёбер в Edit Mode:
# размер шрифта, цвет, подложка, скрытие наложений.
# Настройки: Overlays → Measurement и вкладка MadOut2 V2.
# =====================================================

import blf
import bmesh
import bpy
import gpu
from bpy.props import BoolProperty, FloatVectorProperty, IntProperty
from bpy_extras.view3d_utils import location_3d_to_region_2d
from gpu_extras.batch import batch_for_shader

from .ui_help import draw_help

HELP = (
    "Что делает: крупные подписи длин рёбер",
    "в Edit Mode, вместо мелкого штатного Edge Length",
    "(его лучше выключить в Overlays).",
    "",
    "Шрифт — размер текста, Знаков — после запятой.",
    "Только выделенные — подписи у выделенных рёбер,",
    "  иначе у всех видимых.",
    "Без наложений — прячет налезающие подписи.",
    "Подложка — тёмный фон под текстом.",
)

_handle = None


def _tag_redraw(self=None, context=None):
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


class BigEdgeLengthPrefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    enabled: BoolProperty(name="Показывать длины", default=True, update=_tag_redraw,
                          description="Рисовать крупные подписи длин рёбер в Edit Mode")
    font_size: IntProperty(name="Шрифт", default=18, min=6, max=96, update=_tag_redraw,
                           description="Размер шрифта подписей")
    decimals: IntProperty(name="Знаков", default=2, min=0, max=6, update=_tag_redraw,
                          description="Знаков после запятой: 8.50 m или 8.5 m")
    selected_only: BoolProperty(
        name="Только выделенные", default=True, update=_tag_redraw,
        description="Подписывать только выделенные рёбра (иначе все видимые)")
    hide_overlapping: BoolProperty(
        name="Без наложений", default=True, update=_tag_redraw,
        description="Не рисовать подписи, которые налезают на уже нарисованные")
    max_labels: IntProperty(
        name="Максимум подписей", default=3000, min=10, max=100000, update=_tag_redraw,
        description="Если рёбер больше — не рисовать (чтобы не тормозило)")
    color: FloatVectorProperty(
        name="Цвет текста", subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(1.0, 0.95, 0.4, 1.0), update=_tag_redraw)
    background: BoolProperty(name="Подложка", default=True, update=_tag_redraw,
                             description="Тёмная подложка под текстом (иначе тень)")
    bg_color: FloatVectorProperty(
        name="Цвет подложки", subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.0, 0.0, 0.0, 0.6), update=_tag_redraw)
    show_help: BoolProperty(name="Помощь", default=False)

    def draw(self, context):
        _draw_settings(self.layout, self)


def _prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def _draw_settings(layout, p):
    col = layout.column()
    col.prop(p, "enabled")
    sub = col.column()
    sub.active = p.enabled
    row = sub.row(align=True)
    row.prop(p, "font_size")
    row.prop(p, "decimals")
    row = sub.row(align=True)
    row.prop(p, "selected_only")
    row.prop(p, "hide_overlapping")
    row = sub.row(align=True)
    row.prop(p, "color", text="")
    row.prop(p, "background", text="", icon='MESH_PLANE')
    row.prop(p, "bg_color", text="")
    draw_help(layout, p, "show_help", HELP)


def _format_length(scene, value):
    units = scene.unit_settings
    p = _prefs()
    if units.system == 'NONE':
        return f"{value:.{p.decimals}f}"
    if units.system == 'METRIC':
        return f"{value * units.scale_length:.{p.decimals}f} m"
    return bpy.utils.units.to_string(
        units.system, 'LENGTH', value * units.scale_length, precision=p.decimals,
        split_unit=units.use_separate)


def _draw_rects(rects, color):
    if not rects:
        return
    verts = []
    for x0, y0, x1, y1 in rects:
        verts += [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRIS', {"pos": verts})
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def _draw_callback():
    context = bpy.context
    p = _prefs()
    if p is None or not p.enabled or context.mode != 'EDIT_MESH':
        return
    region = context.region
    rv3d = context.region_data
    space = context.space_data
    if rv3d is None or space is None or not space.overlay.show_overlays:
        return

    # With region overlap the sidebar covers the right part of the viewport.
    right = region.width
    for r in context.area.regions:
        if r.type == 'UI' and r.width > 1 and r.x > region.x:
            right = min(right, r.x - region.x)

    labels = []
    for obj in context.objects_in_mode_unique_data:
        if obj.type != 'MESH':
            continue
        mw = obj.matrix_world
        bm = bmesh.from_edit_mesh(obj.data)
        edges = [e for e in bm.edges if e.select and not e.hide] if p.selected_only else [e for e in bm.edges if not e.hide]
        if len(edges) > p.max_labels:
            continue
        for e in edges:
            a = mw @ e.verts[0].co
            b = mw @ e.verts[1].co
            pos = location_3d_to_region_2d(region, rv3d, (a + b) * 0.5)
            if pos is None or not (0 <= pos.x <= right and 0 <= pos.y <= region.height):
                continue
            labels.append((pos, _format_length(context.scene, (b - a).length)))
    if not labels:
        return

    font_id = 0
    size = p.font_size * context.preferences.system.ui_scale
    blf.size(font_id, size)
    pad = size * 0.25
    placed = []
    for pos, text in labels:
        w, h = blf.dimensions(font_id, text)
        x = pos.x - w * 0.5
        y = pos.y - h * 0.5
        if p.hide_overlapping and any(
                x - pad < px + pw + pad and px - pad < x + w + pad and y - pad < py + ph + pad and py - pad < y + h + pad
                for px, py, pw, ph, _ in placed):
            continue
        placed.append((x, y, w, h, text))

    if p.background:
        _draw_rects([(x - pad, y - pad, x + w + pad, y + h + pad) for x, y, w, h, _ in placed], p.bg_color)

    blf.color(font_id, *p.color)
    if not p.background:
        blf.enable(font_id, blf.SHADOW)
        blf.shadow(font_id, 5, 0.0, 0.0, 0.0, 1.0)
        blf.shadow_offset(font_id, 1, -1)
    for x, y, _, _, text in placed:
        blf.position(font_id, x, y, 0)
        blf.draw(font_id, text)
    blf.disable(font_id, blf.SHADOW)


def _overlay_menu(self, context):
    p = _prefs()
    if p is None:
        return
    layout = self.layout
    layout.separator()
    _draw_settings(layout, p)


def register():
    global _handle
    bpy.utils.register_class(BigEdgeLengthPrefs)
    bpy.types.VIEW3D_PT_overlay_edit_mesh_measurement.append(_overlay_menu)
    _handle = bpy.types.SpaceView3D.draw_handler_add(_draw_callback, (), 'WINDOW', 'POST_PIXEL')
    _tag_redraw()


def unregister():
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    bpy.types.VIEW3D_PT_overlay_edit_mesh_measurement.remove(_overlay_menu)
    bpy.utils.unregister_class(BigEdgeLengthPrefs)
    _tag_redraw()
