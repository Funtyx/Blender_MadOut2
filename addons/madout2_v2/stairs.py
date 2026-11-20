# =====================================================
# Stairs — лестница по выделенной грани-пандусу.
# Ступени считаются по углу ската и стандарту шага (2h + b ≈ 630 мм):
# пологий скат — широкие низкие ступени, крутой — высокие узкие.
# Лестница вваривается в контур грани, боковины закрывают «пилу»,
# UV раскладывается по ступеням или сплошной развёрткой.
# Edit Mode → выделить грань(и) → вкладка MadOut2 V2 → Stairs.
# =====================================================

import math

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty
from mathutils import Vector

from .ui_help import draw_help

HELP = (
    "Что делает: превращает выделенную грань-скат",
    "в лестницу и вваривает её в соседнюю геометрию.",
    "Низ ската — по нижнему ребру, верх — по верхнему.",
    "",
    "Режим «По углу» — по стандарту 2h + b ≈ 630 мм:",
    "  пологий скат — широкие низкие ступени,",
    "  крутой — высокие узкие.",
    "«Высота ступени» / «Количество» — задать вручную.",
    "Шаг 2h+b — длина шага человека (600–660 мм).",
    "Боковины — закрыть треугольники по бокам.",
    "Врезать в скат — ступени ниже линии ската.",
    "UV «По ступеням» — каждая ступень одна плитка",
    "  (подступёнок + проступь), «Развёртка» — сплошная",
    "  в метрах вдоль профиля.",
    "Под кнопками — размеры ступени и проверка",
    "  по стандарту. Откат — Ctrl+Z.",
)

# Комфортные пределы по стандартам лестниц (СП/IBC), метры
RISER_MIN, RISER_MAX = 0.12, 0.19
TREAD_MIN = 0.26
RULE_MIN, RULE_MAX = 0.60, 0.66


class _Fail(Exception):
    pass


def _ramp(face, mw):
    """Bottom and top edges of a quad ramp, as ((b0, b1), (t0, t1)) with b0-t0 and b1-t1 on the same side."""
    if len(face.verts) != 4:
        raise _Fail("Нужна четырёхугольная грань (скат)")
    loops = list(face.loops)
    best = None
    for k in range(2):
        e0 = (loops[k].vert, loops[k + 1].vert)
        e1 = (loops[k + 3 if k == 0 else 0].vert, loops[k + 2].vert)
        z0 = sum((mw @ v.co).z for v in e0) / 2
        z1 = sum((mw @ v.co).z for v in e1) / 2
        if best is None or abs(z1 - z0) > best[0]:
            best = (abs(z1 - z0), e0, e1) if z1 >= z0 else (abs(z1 - z0), e1, e0)
    _, bottom, top = best
    return bottom, top


def standard_check(riser, tread):
    rule = 2 * riser + tread
    if riser > RISER_MAX:
        return f"Круто: ступень {riser * 1000:.0f} мм выше {RISER_MAX * 1000:.0f} мм"
    if tread < TREAD_MIN:
        return f"Узкая проступь {tread * 1000:.0f} мм (меньше {TREAD_MIN * 1000:.0f} мм)"
    if rule < RULE_MIN or rule > RULE_MAX:
        return f"2h+b = {rule * 1000:.0f} мм — вне 600–660 мм (полого: широкие ступени)" \
            if rule > RULE_MAX else f"2h+b = {rule * 1000:.0f} мм — вне 600–660 мм"
    return "По стандарту"


class StairsCore:
    """Algorithm shared by the operator and the live preview; parameters are read from self."""

    def _plan(self, height, run):
        if height < 1e-4:
            raise _Fail("Грань горизонтальная: нужен скат (верх выше низа)")
        if self.mode == 'COUNT':
            return max(1, self.count)
        if self.mode == 'RISER':
            return max(1, round(height / max(0.01, self.riser)))
        # Blondel: 2h + b = rule, h / b = tan(angle)
        slope = height / max(1e-6, run)
        tread = self.step_rule / (2.0 * slope + 1.0)
        return max(1, round(height / max(0.01, tread * slope)))

    def _run(self, bm, obj):
        mw = obj.matrix_world
        inv = mw.inverted()
        faces_in = [f for f in bm.faces if f.select]
        if not faces_in:
            raise _Fail("Выделите грань-скат")
        shared = {e for f in faces_in for e in f.edges if sum(1 for g in e.link_faces if g in faces_in) > 1}
        if shared:
            raise _Fail("Выделенные грани соседние — делайте лестницу по одной грани")

        self.summary = []
        result = []
        for face in faces_in:
            result += self._one(bm, face, mw, inv)
        bm.normal_update()
        return result

    def _one(self, bm, face, mw, inv):
        (b0, b1), (t0, t1) = _ramp(face, mw)
        if self.flip_direction:
            (b0, b1), (t0, t1) = (t0, t1), (b0, b1)
        material, uv_layer = face.material_index, bm.loops.layers.uv.active

        sides = []
        for b, t in ((b0, t0), (b1, t1)):
            wb, wt = mw @ b.co, mw @ t.co
            d = wt - wb
            sides.append((b, t, wb, d))
        height = sum(s[3].z for s in sides) / 2
        run = sum(Vector((s[3].x, s[3].y)).length for s in sides) / 2
        n = self._plan(abs(height), run)
        riser, tread = abs(height) / n, run / n
        angle = math.degrees(math.atan2(abs(height), max(1e-6, run)))
        self.summary.append((n, riser, tread, angle))

        side_edges = [bm.edges.get((b, t)) for b, t, _, _ in sides]
        bmesh.ops.delete(bm, geom=[face], context='FACES_ONLY')

        # Inner corners lie on the ramp line (side edges are split there, so neighbours stay welded);
        # step noses stand one riser above them.
        corners, noses = [], []
        for (b, t, wb, d), e in zip(sides, side_edges):
            chain = [b]
            cur = b
            for k in range(1, n):
                edge = bm.edges.get((cur, t)) if e is not None else None
                if edge is None:
                    nv = bm.verts.new(b.co)
                else:
                    _, nv = bmesh.utils.edge_split(edge, cur, 1.0 / (n - k + 1))
                nv.co = inv @ (wb + d * (k / n))
                chain.append(nv)
                cur = nv
            chain.append(t)
            corners.append(chain)
            up = Vector((0.0, 0.0, d.z / n))
            noses.append([bm.verts.new(inv @ (wb + d * (k / n) + up)) for k in range(n)])

        lc, rc = corners
        ln, rn = noses
        # Risers face downhill whichever way the profile was built
        down_slope = -Vector((sides[0][3].x + sides[1][3].x, sides[0][3].y + sides[1][3].y, 0.0)).normalized()
        if height < 0:
            down_slope = -down_slope
        across = (sides[1][2] - sides[0][2])
        across.z = 0.0
        across.normalize()

        built = []

        def add(verts, want):
            f = bm.faces.get(verts) or bm.faces.new(verts)
            f.normal_update()
            if (mw.to_3x3() @ f.normal).dot(want) < 0:
                f.normal_flip()
            f.material_index = material
            f.smooth = False
            built.append((f, verts))
            return f

        up_dir = Vector((0, 0, 1))
        pieces = []
        for k in range(n):
            pieces.append(('RISER', k, add([lc[k], rc[k], rn[k], ln[k]], down_slope)))
            pieces.append(('TREAD', k, add([ln[k], rn[k], rc[k + 1], lc[k + 1]], up_dir)))
            if self.sides:
                add([lc[k], ln[k], lc[k + 1]], -across)
                add([rc[k], rn[k], rc[k + 1]], across)

        if uv_layer is not None:
            self._uv(uv_layer, pieces, built, lc, rc, ln, rn, sides, riser, tread, mw)
        return [f for f, _ in built]

    def _uv(self, layer, pieces, built, lc, rc, ln, rn, sides, riser, tread, mw):
        sc = self.uv_scale
        width0 = (sides[1][2] - sides[0][2]).length
        step = riser + tread
        left = set(lc) | set(ln)

        def u_of(v):
            return 0.0 if v in left else width0 * sc

        for kind, k, f in pieces:
            for l in f.loops:
                v = l.vert
                if kind == 'RISER':
                    rel = 0.0 if v in (lc[k], rc[k]) else riser
                else:
                    rel = riser if v in (ln[k], rn[k]) else step
                if self.uv_mode == 'STEP':
                    l[layer].uv = (u_of(v), rel / step)
                else:
                    l[layer].uv = (u_of(v), (k * step + rel) * sc)

        piece_faces = {f for _, _, f in pieces}
        for f, _ in built:
            if f in piece_faces:
                continue
            side = sides[0] if f.verts[0] in left else sides[1]
            wb, d = side[2], side[3]
            horiz = Vector((d.x, d.y, 0.0)).normalized()
            for l in f.loops:
                p = mw @ l.vert.co - wb
                l[layer].uv = (p.dot(horiz) * sc, p.z * sc)


class MESH_OT_stairs_from_face(StairsCore, bpy.types.Operator):
    """Сделать лестницу по выделенной грани-скату: ступени по углу и стандарту, UV по кускам"""
    bl_idname = "mesh.stairs_from_face"
    bl_label = "Stairs"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Ступени", default='AUTO',
        items=[
            ('AUTO', "По углу", "По стандарту 2h + b: пологий скат — широкие низкие ступени, крутой — высокие узкие"),
            ('RISER', "Высота", "Задать высоту ступени"),
            ('COUNT', "Количество", "Задать число ступеней"),
        ])
    step_rule: FloatProperty(
        name="Шаг 2h+b", default=0.63, min=0.5, max=0.8, unit='LENGTH',
        description="Длина шага человека: две высоты плюс проступь. Стандарт 600–660 мм")
    riser: FloatProperty(
        name="Высота ступени", default=0.16, min=0.02, soft_max=0.4, unit='LENGTH',
        description="Желаемая высота ступени; число ступеней подбирается так, чтобы ровно уложиться в высоту ската")
    count: IntProperty(name="Ступеней", default=10, min=1, max=500, description="Число ступеней")
    sides: BoolProperty(name="Боковины", default=True,
                        description="Закрыть треугольники между ступенями и линией ската по бокам")
    flip_direction: BoolProperty(name="Врезать в скат", default=False,
                                 description="Ступени ниже линии ската (врезаны в него), а не над ней")
    uv_mode: EnumProperty(
        name="UV", default='STEP',
        items=[
            ('STEP', "По ступеням", "Каждая ступень — одна плитка по V: подступёнок снизу, проступь сверху"),
            ('UNFOLD', "Развёртка", "Сплошная развёртка вдоль профиля, в метрах × «UV на метр»"),
        ])
    uv_scale: FloatProperty(name="UV на метр", default=1.0, min=0.0001, soft_max=10.0,
                            description="Сколько единиц UV приходится на метр (по ширине и в развёртке)")
    show_help: BoolProperty(name="Помощь", default=False)

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
            faces = self._run(bm, obj)
        except _Fail as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        for f in bm.faces:
            f.select = False
        for f in faces:
            f.select = True
        bm.select_flush_mode()
        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, describe(self.summary))
        return {'FINISHED'}


def describe(summary):
    if not summary:
        return ""
    n, riser, tread, angle = summary[0]
    text = f"{n} ступ.: h {riser * 1000:.0f} мм, b {tread * 1000:.0f} мм, угол {angle:.0f}° — {standard_check(riser, tread)}"
    if len(summary) > 1:
        text += f" (+ ещё {len(summary) - 1} грани)"
    return text


PARAM_KEYS = ("mode", "step_rule", "riser", "count", "sides", "flip_direction", "uv_mode", "uv_scale")


def draw_params(layout, data, prefix):
    col = layout.column()
    col.row().prop(data, prefix + "mode", expand=True)
    mode = getattr(data, prefix + "mode")
    if mode == 'AUTO':
        col.prop(data, prefix + "step_rule")
    elif mode == 'RISER':
        col.prop(data, prefix + "riser")
    else:
        col.prop(data, prefix + "count")
    row = col.row(align=True)
    row.prop(data, prefix + "sides")
    row.prop(data, prefix + "flip_direction")
    col.row().prop(data, prefix + "uv_mode", expand=True)
    col.prop(data, prefix + "uv_scale")


def _menu(self, context):
    self.layout.operator(MESH_OT_stairs_from_face.bl_idname, icon='IPO_CONSTANT')


def register():
    bpy.utils.register_class(MESH_OT_stairs_from_face)
    bpy.types.VIEW3D_MT_edit_mesh_faces.append(_menu)


def unregister():
    bpy.types.VIEW3D_MT_edit_mesh_faces.remove(_menu)
    bpy.utils.unregister_class(MESH_OT_stairs_from_face)
