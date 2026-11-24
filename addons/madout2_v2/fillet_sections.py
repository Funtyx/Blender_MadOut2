# =====================================================
# Fillet Sections — меняет число сечений в скруглении: веер от
# центральной точки и кольца вокруг него (бордюр, тротуар) делятся
# заново на N равных шагов. Дуга остаётся дугой, концы скругления
# и соседняя геометрия не двигаются, UV и цвета вершин переносятся.
# Edit Mode → выделить грани скругления → вкладка MadOut2 V2.
# =====================================================

import math

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty

from .strip_resample import (StripResampleCore, _Fail, _arc, _chains_from_region, _dense_curve,
                             _sample)
from .ui_help import draw_help

HELP = (
    "Что делает: делит скругление заново на нужное",
    "число сечений — больше или меньше. Веер от центра",
    "и все кольца вокруг (бордюр, тротуар) режутся",
    "одними и теми же сечениями, дуга остаётся дугой.",
    "",
    "Что выделить: грани скругления — веер треугольников",
    "от центральной точки и кольца квадов вокруг него.",
    "Можно и без веера: одни кольца бордюра.",
    "",
    "Сечений — сколько шагов вдоль дуги (0 = как сейчас).",
    "На 90° — то же, но по углу: у всех скруглений",
    "одинаковая густота, крутые получат больше.",
    "По эталону — учитывает и угол, и радиус: выделите",
    "образцовое скругление, задайте ему сечения и",
    "нажмите «Взять с выделенного». Остальные получат",
    "ту же гладкость: шире угол или больше радиус —",
    "сечений больше, маленький радиус — меньше.",
    "Концы скругления не сдвигаются. Откат — Ctrl+Z.",
)


def _fan_chains(region):
    """
    The hub of a fan and the rings round it, every ring ordered from one end of the rounding to the
    other and the rings ordered outwards. Returns (hub, rings, error); hub is None when the selection
    has no fan.

    Rings are found as layers of edge distance from the hub, not by geometry: a kerb's rings stand
    one above the other (bottom and top of the kerb face share x and y), so anything measured in
    the plane would merge them, while in the grid of a rounding every ring is exactly one edge
    further out than the one inside it.
    """
    verts = {v for f in region for v in f.verts}
    hub, best = None, 0
    for v in verts:
        own = [f for f in v.link_faces if f in region]
        if len(own) >= 2 and all(len(f.verts) == 3 for f in own) and len(own) > best:
            hub, best = v, len(own)
    if hub is None:
        return None, None, None

    edges = {e for f in region for e in f.edges}
    layer = {hub: 0}
    frontier = [hub]
    while frontier:
        nxt = []
        for v in frontier:
            for e in v.link_edges:
                if e in edges:
                    w = e.other_vert(v)
                    if w not in layer:
                        layer[w] = layer[v] + 1
                        nxt.append(w)
        frontier = nxt
    if len(layer) != len(verts):
        return hub, None, "Выделенные грани должны быть связаны"

    uneven = "Сетка скругления должна быть ровной: веер от центра и кольца квадов вокруг него"
    rings = []
    for k in range(1, max(layer.values()) + 1):
        ring = {v for v in verts if layer[v] == k}
        adj = {v: [] for v in ring}
        for e in edges:
            a, b = e.verts
            if a in ring and b in ring:
                adj[a].append(b)
                adj[b].append(a)
        ends = [v for v in ring if len(adj[v]) == 1]
        if len(ends) != 2 or any(len(n) > 2 for n in adj.values()):
            return hub, None, f"Кольцо {k} не цепочка. {uneven}"
        order, prev = [ends[0]], None
        while len(order) < len(ring):
            nxt = [w for w in adj[order[-1]] if w is not prev]
            if not nxt:
                break
            prev = order[-1]
            order.append(nxt[0])
        if len(order) != len(ring):
            return hub, None, f"Кольцо {k} не цепочка. {uneven}"
        rings.append(order)

    # The end of the rounding is a radial line: each ring starts where the ring inside it starts
    for k in range(1, len(rings)):
        a0 = rings[k - 1][0]

        def touches(v):
            return any(e in edges and e.other_vert(v) is a0 for e in v.link_edges)

        if touches(rings[k][0]):
            continue
        if touches(rings[k][-1]):
            rings[k].reverse()
            continue
        return hub, None, f"Концы колец {k} и {k + 1} не связаны ребром. {uneven}"
    return hub, rings, None


def _welded(ring, region):
    """Inner points of a ring that StripResampleCore._anchored will refuse to remove once the
    selected faces are gone: an edge leading out of the selection, or a triangle outside it."""
    edges = {e for f in region for e in f.edges}
    return sum(1 for v in ring[1:-1]
               if any(e not in edges for e in v.link_edges)
               or any(f not in region and len(f.verts) <= 3 for f in v.link_faces))


def _turn(points):
    """How far the rounding turns. The first and last CHORDS of n equal chords of an arc differ by
    only (n - 1) / n of the arc's turn - half a step lost at each end - so the angle between them
    is scaled back up; read raw, 45 degrees in five pieces measured 36."""
    n = len(points) - 1
    if n < 2:
        return 0.0
    d0, d1 = points[1] - points[0], points[-1] - points[-2]
    if d0.length < 1e-9 or d1.length < 1e-9:
        return 0.0
    return d0.angle(d1) * n / (n - 1)


def _measure(rings, mw):
    """(turn, radius) of a rounding: how far it turns, and the radius of its OUTERMOST ring - the one
    whose chords cut furthest inside the true arc. The radius comes from the ring's length and its
    turn (n equal chords of an arc of radius R, turning by t, are 2 n R sin(t / 2n) long), so a ring
    that climbs or stands on a slope needs no plane to be fitted to it."""
    turn = _turn([mw @ v.co for v in rings[0]])
    radius = 0.0
    for r in rings:
        p = [mw @ v.co for v in r]
        n = len(p) - 1
        length = sum((b - a).length for a, b in zip(p, p[1:]))
        if n >= 1 and turn > 1e-6:
            radius = max(radius, length / (2.0 * n * math.sin(turn / (2.0 * n))))
    return turn, radius


def sections_for(turn, radius, sag):
    """Sections that keep every chord within `sag` of the true arc: a chord over an angle a sits
    R (1 - cos(a / 2)) inside it. Holding that one number is what lets a single rounding set the
    density for all of them - a wider turn gets more sections in proportion, a bigger radius more
    as its square root, a tight one fewer."""
    if turn <= 1e-6 or radius <= 1e-9:
        return 1
    step = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - sag / radius)))
    if step <= 1e-9:
        return 256
    # A tenth of a percent of slack: the rounding a sag was taken from, or one turning exactly twice
    # as far, lands on a whole number give or take rounding noise, and must not be pushed to the next
    return max(1, min(256, math.ceil(turn / step * (1.0 - 1e-3))))


def sag_for(turn, radius, count):
    """The reverse of sections_for: how far the chords of `count` sections sit inside the arc."""
    return radius * (1.0 - math.cos(turn / (2.0 * max(1, count))))


def find_rings(region):
    """(hub, rings) of a selected rounding - a fan and its rings, or a plain band of rings."""
    hub, rings, err = _fan_chains(region)
    if hub is None or err:
        # No fan, or one that is not a clean grid: a band of rings is a plain strip
        band, band_err = _chains_from_region(region)
        if band is None or len(band) < 2:
            raise _Fail(err or band_err or "Выделите грани скругления")
        hub, rings = None, band
    if not rings:
        raise _Fail("Вокруг центра нет ни одного кольца")
    return hub, rings


class FilletSectionsCore(StripResampleCore):
    """The strip machinery of Strip Resample (welding to the neighbours, UV, colours), with a hub."""

    summary = (0, 0, False, 0, 0.0, 0.0)

    def _count(self, rings, mw):
        old = max(len(r) - 1 for r in rings)
        turn, radius = _measure(rings, mw)
        if self.mode == 'COUNT':
            if self.count == 0:
                # Show the real current number in the F9 panel, so it can be dragged up or down
                self.count = old
            count = self.count
        elif self.mode == 'ANGLE':
            count = round(self.seg_90 * math.degrees(turn) / 90.0)
        else:
            count = sections_for(turn, radius, self.sag)
        return old, max(1, count), turn, radius

    def _run(self, bm, obj):
        region = {f for f in bm.faces if f.select}
        if not region:
            raise _Fail("Выделите грани скругления: веер от центра и кольца вокруг него")

        hub, rings = find_rings(region)
        old, count, turn, radius = self._count(rings, obj.matrix_world)

        # A point welded to geometry outside the selection cannot be taken out without tearing that
        # geometry, so every ring keeps at least those. Said up front, with the number, rather than
        # as Strip Resample's generic failure halfway through.
        least = 1 + max(_welded(r, region) for r in rings)
        if count < least:
            raise _Fail(f"Меньше {least} сечений нельзя: {least - 1} точек кольца сварены с невыделенной "
                        f"геометрией. Выделите и её или оставьте не меньше {least}")

        pts =[[v.co.copy() for v in r] for r in rings]
        chains = ([[hub]] if hub else []) + rings
        strips = self._capture_strips(bm, chains)
        all_band = {f for s in strips for f in s["faces"]}
        chain_edge_set = {bm.edges.get((a, b)) for r in rings for a, b in zip(r, r[1:])}
        band_edges = {e for f in all_band for e in f.edges} - chain_edge_set
        bmesh.ops.delete(bm, geom=list(all_band), context='FACES_ONLY')
        for e in band_edges:
            if e.is_valid and not e.link_faces:
                bm.edges.remove(e)

        # Every ring is walked at equal steps of its OWN length. On concentric arcs that is the same
        # angle on every ring, so the new sections run straight out from the centre, and a ring's
        # ends stay where they are - they are the seams with the straight runs either side.
        new_chains = [[hub] * (count + 1)] if hub else []
        for ring, p in zip(rings, pts):
            targets = _sample(_dense_curve(p, self.smooth, set()), count)
            new_chains.append(self._rebuild_chain(bm, ring, p, targets, chains))

        faces = []
        for i, s in enumerate(strips):
            a, b = new_chains[i], new_chains[i + 1]
            for k in range(count):
                quad = list(dict.fromkeys([a[k], a[k + 1], b[k + 1], b[k]]))
                if len(quad) < 3 or bm.faces.get(quad):
                    continue
                f = bm.faces.new(quad)
                f.normal_update()
                if s["normal"].length > 0 and f.normal.dot(s["normal"]) < 0:
                    f.normal_flip()
                f.material_index = s["material"]
                f.smooth = s["smooth"]
                faces.append(f)
                self._interpolate_loops(bm, f, s)

        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False
        for f in faces:
            f.select = True
        bm.select_flush(True)
        bm.normal_update()
        self.summary = (old, count, hub is not None, len(rings), turn, radius)
        return faces


def describe(summary):
    old, count, fan, rings, turn, radius = summary
    what = f"веер и колец: {rings}" if fan else f"колец: {rings}"
    return f"Сечений {old} → {count} ({what}; {math.degrees(turn):.0f}°, R {radius:.2f} м)"


class MESH_OT_fillet_sections(FilletSectionsCore, bpy.types.Operator):
    """Изменить число сечений в скруглении: веер от центра и кольца вокруг делятся заново"""
    bl_idname = "mesh.fillet_sections"
    bl_label = "Fillet Sections"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Режим", default='COUNT',
        items=[
            ('COUNT', "Количество", "Задать, сколько сечений будет вдоль скругления"),
            ('ANGLE', "На 90°", "Задать густоту по углу: скругление на 90° получит столько сечений"),
            ('SAG', "По эталону",
             "Сечений столько, чтобы хорды отходили от дуги не больше заданного: учитывает и угол, и радиус. "
             "Эталон снимается кнопкой «Взять с выделенного»"),
        ])
    count: IntProperty(
        name="Сечений", default=0, min=0, max=1000, options={'SKIP_SAVE'},
        description="Сколько шагов вдоль скругления. Больше — плавнее, меньше — легче. "
                    "При запуске подставляется текущее количество")
    seg_90: IntProperty(
        name="Сечений на 90°", default=8, min=1, max=256,
        description="Сколько шагов на четверть круга; скругление на 45° получит половину")
    sag: FloatProperty(
        name="Отклонение от дуги", default=0.03, min=0.0005, soft_max=0.5, unit='LENGTH', precision=4,
        description="Насколько хорда может срезать дугу на внешнем кольце. Меньше — сечений больше. "
                    "Шире поворот — сечений больше; больше радиус — тоже больше, но плавнее")
    smooth: FloatProperty(
        name="Плавность", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description="1 — новые точки на дуге через старые вершины, 0 — на старых прямых рёбрах")
    show_help: BoolProperty(name="Помощь", default=False)

    # Strip Resample's corner keeping is meaningless along a rounding
    keep_corners = False
    corner_angle = math.radians(35.0)

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def draw(self, context):
        draw_params(self.layout, self, "")
        draw_help(self.layout, self, "show_help", HELP)

    def execute(self, context):
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        try:
            n_faces = len(self._run(bm, obj))
        except _Fail as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, describe(self.summary) + f", {n_faces} граней")
        return {'FINISHED'}


PARAM_KEYS = ("mode", "count", "seg_90", "sag", "smooth")


def draw_params(layout, data, prefix, count_text=None):
    col = layout.column()
    col.row().prop(data, prefix + "mode", expand=True)
    mode = getattr(data, prefix + "mode")
    if mode == 'COUNT':
        col.prop(data, prefix + "count", text=count_text) if count_text else col.prop(data, prefix + "count")
    elif mode == 'ANGLE':
        col.prop(data, prefix + "seg_90")
    else:
        col.prop(data, prefix + "sag")
    col.prop(data, prefix + "smooth")
    if prefix:
        # Only on the tab: the F9 panel already belongs to one applied rounding
        col.operator(MESH_OT_fillet_sections_take.bl_idname, icon='EYEDROPPER')


class MESH_OT_fillet_sections_take(bpy.types.Operator):
    """Сделать выделенное скругление эталоном: столько сечений, сколько сейчас задано на вкладке
    (или уже есть), — и дальше «По эталону» все скругления получат ту же гладкость"""
    bl_idname = "mesh.fillet_sections_take"
    bl_label = "Взять с выделенного"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def execute(self, context):
        s = context.scene.madout2_v2
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        region = {f for f in bm.faces if f.select}
        if not region:
            self.report({'ERROR'}, "Выделите скругление-эталон и задайте ему число сечений")
            return {'CANCELLED'}
        core = FilletSectionsCore()
        for k in PARAM_KEYS:
            setattr(core, k, getattr(s, "fs_" + k))
        try:
            _, rings = find_rings(region)
            _, count, turn, radius = core._count(rings, obj.matrix_world)
        except _Fail as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        s.fs_sag = sag_for(turn, radius, count)
        s.fs_mode = 'SAG'
        self.report({'INFO'}, f"Эталон: {count} сечений на {math.degrees(turn):.0f}° при R {radius:.2f} м — "
                              f"хорда отходит от дуги на {s.fs_sag * 100:.1f} см")
        return {'FINISHED'}


def _menu(self, context):
    self.layout.operator(MESH_OT_fillet_sections.bl_idname, icon='SPHERECURVE')


_MENUS = (
    bpy.types.VIEW3D_MT_edit_mesh_faces,
    bpy.types.VIEW3D_MT_edit_mesh_context_menu,
)


def register():
    bpy.utils.register_class(MESH_OT_fillet_sections)
    bpy.utils.register_class(MESH_OT_fillet_sections_take)
    for m in _MENUS:
        m.append(_menu)


def unregister():
    for m in _MENUS:
        m.remove(_menu)
    bpy.utils.unregister_class(MESH_OT_fillet_sections_take)
    bpy.utils.unregister_class(MESH_OT_fillet_sections)
