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
    "Можно выделить и ровные площадки: лестница",
    "  будет только на наклонных гранях (Мин. уклон).",
    "Стенки «Ровные» — верх на уровне верхней",
    "  площадки, «По углу» — вдоль носов ступеней;",
    "  Толщина «Наружу» — ступени той же ширины,",
    "  «Внутрь» — стенка внутри ската, ступени уже.",
    "  Подъём — насколько стенка выше ступеней.",
    "Боковины — закрыть треугольники по бокам",
    "  (со стенками не нужны).",
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
        nmat = inv.transposed().to_3x3()
        faces_in = [f for f in bm.faces if f.select]
        if not faces_in:
            raise _Fail("Выделите грань-скат (можно вместе с ровными площадками)")

        # Flat faces are landings and stay as they are; only sloped ones become stairs
        ramps = []
        for f in faces_in:
            n = (nmat @ f.normal).normalized()
            slope = math.degrees(math.acos(min(1.0, abs(n.z))))
            if slope < math.degrees(self.min_slope):
                continue
            if len(f.verts) != 4:
                raise _Fail("Наклонная грань должна быть четырёхугольной")
            ramps.append(f)
        if not ramps:
            raise _Fail("Нет наклонных граней: лестница строится на скате, ровные площадки остаются как есть")

        side_edges = {}
        for f in ramps:
            (b0, b1), (t0, t1) = _ramp(f, mw)
            for v0, v1 in ((b0, t0), (b1, t1)):
                e = bm.edges.get((v0, v1))
                if e in side_edges:
                    raise _Fail("Два ската касаются боками — делайте их по одному")
                side_edges[e] = f

        self._landings = {f for f in faces_in if f not in ramps}
        self.summary = []
        result = []
        for face in ramps:
            result += self._one(bm, face, mw, inv)
        bm.normal_update()
        return result

    def _one(self, bm, face, mw, inv):
        (b0, b1), (t0, t1) = _ramp(face, mw)
        if self.flip_direction:
            (b0, b1), (t0, t1) = (t0, t1), (b0, b1)
        material, uv_layer = face.material_index, bm.loops.layers.uv.active
        # Walls continue along selected flat landings at both ends of the flight
        landing_far = {}
        for end, (c0, c1) in (('start', (b0, b1)), ('end', (t0, t1))):
            e = bm.edges.get((c0, c1))
            landing = next((f for f in e.link_faces if f in self._landings), None) if e else None
            for side_i, c in enumerate((c0, c1)):
                if landing is None:
                    continue
                side_e = next((x for x in c.link_edges if x is not e and landing in x.link_faces), None)
                if side_e is not None:
                    landing_far[(end, side_i)] = mw @ side_e.other_vert(c).co

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

        # Points on the ramp sides (side edges are split there, so neighbours stay welded)
        side_chains = [self._split_line(bm, b, t, wb, d, n, e, inv) for (b, t, wb, d), e in zip(sides, side_edges)]

        walls_inside = self.walls != 'NONE' and self.wall_side == 'IN'
        if walls_inside:
            # Walls take their thickness from the stair width: steps start at the wall's inner face
            sides, corners = self._inner_sides(bm, sides, side_chains, n, mw, inv)
        else:
            corners = side_chains

        # Inner corners lie on the ramp line, step noses stand one riser above them
        noses = []
        for (b, t, wb, d) in sides:
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
            # With walls the wall's inner face is welded to the step profile instead
            if self.sides and self.walls == 'NONE':
                add([lc[k], ln[k], lc[k + 1]], -across)
                add([rc[k], rn[k], rc[k + 1]], across)

        if uv_layer is not None:
            self._uv(uv_layer, pieces, built, lc, rc, ln, rn, sides, riser, tread, mw)
        faces = [f for f, _ in built]
        if self.walls != 'NONE':
            top_z = max(sides[0][2].z, sides[1][2].z, (sides[0][2] + sides[0][3]).z, (sides[1][2] + sides[1][3]).z)
            for i, (chain, nose, out) in enumerate(((lc, ln, -across), (rc, rn, across))):
                wb, d = sides[i][2], sides[i][3]
                if walls_inside:
                    outer_pts = [mw @ v.co for v in side_chains[i]]
                    outer_verts = side_chains[i]
                else:
                    outer_pts = [wb + d * (k / n) + out * self.wall_thickness for k in range(n + 1)]
                    outer_verts = None
                faces += self._wall(bm, chain, nose, sides[i], out, outer_pts, outer_verts, n, top_z,
                                    height < 0, material, uv_layer, mw, inv,
                                    landing_far.get(('start', i)), landing_far.get(('end', i)))
        return faces

    @staticmethod
    def _split_line(bm, b, t, wb, d, n, edge, inv):
        chain = [b]
        cur = b
        for k in range(1, n):
            e = bm.edges.get((cur, t)) if edge is not None else None
            if e is None:
                nv = bm.verts.new(b.co)
            else:
                _, nv = bmesh.utils.edge_split(e, cur, 1.0 / (n - k + 1))
            nv.co = inv @ (wb + d * (k / n))
            chain.append(nv)
            cur = nv
        chain.append(t)
        return chain

    def _inner_sides(self, bm, sides, side_chains, n, mw, inv):
        th = self.wall_thickness
        (b0, t0, _, _), (b1, t1, _, _) = sides
        ends = []
        for v0, v1 in ((b0, b1), (t0, t1)):
            w0, w1 = mw @ v0.co, mw @ v1.co
            width = (w1 - w0).length
            if 2 * th >= width - 0.05:
                raise _Fail("Стенки внутрь толще, чем ширина лестницы")
            e = bm.edges.get((v0, v1))
            _, p0 = bmesh.utils.edge_split(e, v0, th / width)
            p0.co = inv @ (w0 + (w1 - w0).normalized() * th)
            e = bm.edges.get((p0, v1))
            _, p1 = bmesh.utils.edge_split(e, v1, th / (width - th))
            p1.co = inv @ (w1 + (w0 - w1).normalized() * th)
            ends.append((p0, p1))
        (bi0, bi1), (ti0, ti1) = ends
        new_sides, corners = [], []
        for bi, ti in ((bi0, ti0), (bi1, ti1)):
            wb, wt = mw @ bi.co, mw @ ti.co
            d = wt - wb
            new_sides.append((bi, ti, wb, d))
            corners.append([bi] + [bm.verts.new(inv @ (wb + d * (k / n))) for k in range(1, n)] + [ti])
        return new_sides, corners

    def _wall(self, bm, chain, nose, side, outward, outer_pts, outer_verts, n, top_z, cut, material, uv_layer,
              mw, inv, far_start=None, far_end=None):
        """Side wall with thickness: inner face welded to the step profile, flat or sloped top.
        outer_pts — the wall's outer bottom line; outer_verts — existing vertices there (walls inside)."""
        _, _, wb, d = side
        rise = d.z / n
        raise_by = max(self.wall_raise, 0.005)
        fwd = Vector((d.x, d.y, 0.0)).normalized()
        ramp = [wb + d * (k / n) for k in range(n + 1)]
        if self.walls == 'FLAT':
            tops = [Vector((p.x, p.y, top_z + raise_by)) for p in ramp]
        else:
            lift = max(rise, 0.0)
            tops = [p + Vector((0.0, 0.0, lift + raise_by)) for p in ramp]
        new = lambda co: bm.verts.new(inv @ co)
        top = [new(p) for p in tops]
        top_out = [new(Vector((o.x, o.y, p.z))) for o, p in zip(outer_pts, tops)]
        bottom_out = outer_verts if outer_verts is not None else [new(o) for o in outer_pts]

        made = []

        def add(verts, want):
            verts = list(dict.fromkeys(verts))
            if len(verts) < 3:
                return
            f = bm.faces.get(verts) or bm.faces.new(verts)
            f.normal_update()
            if (mw.to_3x3() @ f.normal).dot(want) < 0:
                f.normal_flip()
            f.material_index = material
            f.smooth = False
            made.append(f)

        inward = -outward
        up = Vector((0.0, 0.0, 1.0))
        for k in range(n):
            if cut:
                inner = [nose[k], chain[k + 1], top[k + 1], top[k], chain[k]]
            else:
                inner = [nose[k], chain[k + 1]] + ([nose[k + 1]] if k + 1 < n else []) + [top[k + 1], top[k]]
            add(inner, inward)
            add([bottom_out[k], bottom_out[k + 1], top_out[k + 1], top_out[k]], outward)
            add([top[k], top[k + 1], top_out[k + 1], top_out[k]], up)
        # Cut-in: the first riser hangs below the ramp line, so the wall end starts at the line itself
        inner_start = [chain[0]] if cut else [chain[0], nose[0]]
        if far_start is not None and (far_start - (mw @ chain[0].co)).dot(fwd) < -1e-4:
            self._landing_wall(add, new, far_start, -fwd, outward, inner_start, top[0], top_out[0], bottom_out[0],
                               tops[0].z, reverse=True)
        else:
            add(inner_start + [top[0], top_out[0], bottom_out[0]], -fwd)
        if far_end is not None and (far_end - (mw @ chain[n].co)).dot(fwd) > 1e-4:
            self._landing_wall(add, new, far_end, fwd, outward, [chain[n]], top[n], top_out[n], bottom_out[n],
                               tops[n].z, reverse=False)
        else:
            add([chain[n], top[n], top_out[n], bottom_out[n]], fwd)

        if uv_layer is not None:
            sc = self.uv_scale
            for f in made:
                n_w = (mw.to_3x3() @ f.normal).normalized()
                for l in f.loops:
                    p = mw @ l.vert.co - wb
                    if abs(n_w.z) > 0.7:
                        uv = (p.dot(fwd), p.dot(outward))
                    elif abs(n_w.dot(fwd)) > 0.7:
                        uv = (p.dot(outward), p.z)
                    else:
                        uv = (p.dot(fwd), p.z)
                    l[uv_layer].uv = (uv[0] * sc, uv[1] * sc)
        return made

    def _landing_wall(self, add, new, far, away, outward, inner_join, join_top, top_out_j, bottom_out_j, z,
                      reverse):
        """Wall continued along a flat landing: same top height as the flight wall where they meet."""
        th = self.wall_thickness
        if self.wall_side == 'IN':
            inner_far, outer_far = far - outward * th, far.copy()
        else:
            inner_far, outer_far = far.copy(), far + outward * th
        lin = new(inner_far)
        lbout = new(outer_far)
        ltop = new(Vector((inner_far.x, inner_far.y, z)))
        ltop_out = new(Vector((outer_far.x, outer_far.y, z)))
        up = Vector((0.0, 0.0, 1.0))
        inner = [lin] + inner_join + [join_top, ltop] if reverse else inner_join + [lin, ltop, join_top]
        add(inner, -outward)
        add([lbout, bottom_out_j, top_out_j, ltop_out], outward)
        add([ltop, join_top, top_out_j, ltop_out], up)
        add([lin, ltop, ltop_out, lbout], away)

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
    walls: EnumProperty(
        name="Стенки", default='NONE',
        items=[
            ('NONE', "Нет", "Без боковых стенок"),
            ('FLAT', "Ровные", "Верх стенки горизонтальный — на уровне верхней площадки плюс подъём"),
            ('SLOPE', "По углу", "Верх стенки идёт параллельно носам ступеней плюс подъём"),
        ])
    wall_thickness: FloatProperty(name="Толщина стенки", default=0.25, min=0.01, soft_max=2.0, unit='LENGTH',
                                  description="Толщина боковой стенки наружу от лестницы")
    wall_side: EnumProperty(
        name="Толщина", default='OUT',
        items=[
            ('OUT', "Наружу", "Стенка снаружи лестницы: ширина ступеней не меняется"),
            ('IN', "Внутрь", "Стенка внутри ширины ската: наружная сторона по краю ската, ступени уже"),
        ])
    wall_raise: FloatProperty(name="Подъём стенки", default=0.1, min=0.0, soft_max=2.0, unit='LENGTH',
                              description="Насколько стенка выше ступеней (у ровной — выше верхней площадки)")
    min_slope: FloatProperty(name="Мин. уклон", default=math.radians(3.0), min=0.0, max=math.radians(80.0),
                             subtype='ANGLE',
                             description="Грани положе этого угла — площадки: они остаются как есть")
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


PARAM_KEYS = ("mode", "step_rule", "riser", "count", "sides", "flip_direction", "walls", "wall_thickness",
              "wall_side", "wall_raise", "min_slope", "uv_mode", "uv_scale")


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
    col.prop(data, prefix + "min_slope")
    col.label(text="Стенки:")
    col.row().prop(data, prefix + "walls", expand=True)
    if getattr(data, prefix + "walls") != 'NONE':
        col.row().prop(data, prefix + "wall_side", expand=True)
        row = col.row(align=True)
        row.prop(data, prefix + "wall_thickness", text="Толщина")
        row.prop(data, prefix + "wall_raise", text="Подъём")
    col.label(text="UV:")
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
