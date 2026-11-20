# =====================================================
# Ground Fill — заливка между двумя цепочками рёбер сеткой:
# ряды, подъём/прогиб (Bulge), профиль, UV полосой.
# Edit Mode → Ctrl+E / ПКМ → Ground Fill, настройка в F9.
# =====================================================

import math

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty
from mathutils import Vector

from .ui_help import draw_help

_INF = float("inf")

HELP = (
    "Что делает: заливает «умный» рельеф между двумя",
    "цепочками рёбер и вваривает его в них.",
    "Внутри — ровная сетка, у краёв — переходный ряд,",
    "поэтому густые вершины не собирают линии в пучок.",
    "",
    "Что выделить: две цепочки рёбер (обе открытые",
    "или обе замкнутые — тогда получится кольцо).",
    "",
    "Рядов / Сегментов — плотность сетки поперёк и",
    "  вдоль (0 сегментов = ячейки почти квадратные).",
    "Склон «Рельеф» — поверхность продолжает уклон",
    "  соседних граней, без излома на стыке.",
    "Выравнивание — сглаживает сетку на изгибах.",
    "Подъём — холм (+) или яма (−) в метрах.",
    "Предпросмотр в панели N: видно сразу,",
    "  меш меняется только по «Применить».",
)


def _chains_from_selection(bm):
    """Returns list of (verts, is_closed) for every connected component of selected edges."""
    edges = [e for e in bm.edges if e.select]
    adj = {}
    for e in edges:
        for v in e.verts:
            adj.setdefault(v, []).append(e)

    chains = []
    seen = set()
    for start in adj:
        if start in seen:
            continue
        comp = set()
        stack = [start]
        while stack:
            v = stack.pop()
            if v in comp:
                continue
            comp.add(v)
            stack.extend(e.other_vert(v) for e in adj[v])
        seen |= comp

        if any(len(adj[v]) > 2 for v in comp):
            return None, "Выделение ветвится: каждая цепочка должна быть простой линией рёбер"
        ends = [v for v in comp if len(adj[v]) == 1]
        is_closed = not ends
        first = ends[0] if ends else next(iter(comp))

        order = [first]
        prev_edge = None
        v = first
        while True:
            nxt = [e for e in adj[v] if e is not prev_edge]
            if not nxt:
                break
            prev_edge = nxt[0]
            v = prev_edge.other_vert(v)
            if v is first:
                break
            order.append(v)
        chains.append((order, is_closed))
    return chains, None


def _arc_params(cos, is_closed):
    pts = list(cos) + ([cos[0]] if is_closed else [])
    acc = [0.0]
    for a, b in zip(pts, pts[1:]):
        acc.append(acc[-1] + (b - a).length)
    total = acc[-1] or 1.0
    return [x / total for x in acc], acc[-1]


def _align_chains(a, b, a_closed, mw):
    wa = [mw @ v.co for v in a]
    wb = [mw @ v.co for v in b]
    if not a_closed:
        straight = (wa[0] - wb[0]).length + (wa[-1] - wb[-1]).length
        crossed = (wa[0] - wb[-1]).length + (wa[-1] - wb[0]).length
        return b[::-1] if crossed < straight else b

    start = min(range(len(b)), key=lambda i: (wb[i] - wa[0]).length)
    b = b[start:] + b[:start]
    wb = wb[start:] + wb[:start]
    dir_a = wa[1] - wa[0]
    dir_b = wb[1] - wb[0]
    if dir_a.dot(dir_b) < 0:
        b = [b[0]] + b[1:][::-1]
    return b


def _profile(t, shape):
    x = 1.0 - abs(2.0 * t - 1.0)
    if shape == 'SMOOTH':
        return math.sin(math.pi * t)
    if shape == 'SPHERE':
        return math.sqrt(max(0.0, 1.0 - (2.0 * t - 1.0) ** 2))
    if shape == 'LINEAR':
        return x
    if shape == 'SHARP':
        return x * x
    if shape == 'FLAT':
        return min(1.0, x * 3.0)
    return 0.0


def _bias_t(t, bias):
    # Moves the profile peak toward chain A (bias < 0) or chain B (bias > 0).
    if abs(bias) < 1e-6:
        return t
    peak = 0.5 + 0.49 * bias
    return 0.5 * t / peak if t <= peak else 0.5 + 0.5 * (t - peak) / (1.0 - peak)




def _eval(points, params, s, closed):
    pts = list(points) + ([points[0]] if closed else [])
    for i in range(len(params) - 1):
        if params[i] <= s <= params[i + 1] or i == len(params) - 2:
            span = params[i + 1] - params[i]
            f = 0.0 if span <= 0 else (s - params[i]) / span
            return pts[i].lerp(pts[i + 1], min(1.0, max(0.0, f))), i
    return pts[0].copy(), 0


def _hermite(z0, z1, m0, m1, t):
    t2, t3 = t * t, t * t * t
    return (2 * t3 - 3 * t2 + 1) * z0 + (t3 - 2 * t2 + t) * m0 + (-2 * t3 + 3 * t2) * z1 + (t3 - t2) * m1


def _ladder(xs, px, ys, py):
    """Faces bridging two vertex rows with different spacing: quads where they line up, triangles elsewhere."""
    faces = []
    i = j = 0
    while i < len(xs) - 1 or j < len(ys) - 1:
        if i == len(xs) - 1:
            faces.append((xs[i], ys[j + 1], ys[j]))
            j += 1
            continue
        if j == len(ys) - 1:
            faces.append((xs[i], xs[i + 1], ys[j]))
            i += 1
            continue
        nx, ny = px[i + 1], py[j + 1]
        step = min(px[i + 1] - px[i], py[j + 1] - py[j])
        if abs(nx - ny) < 0.35 * step:
            faces.append((xs[i], xs[i + 1], ys[j + 1], ys[j]))
            i += 1
            j += 1
        elif nx < ny:
            faces.append((xs[i], xs[i + 1], ys[j]))
            i += 1
        else:
            faces.append((xs[i], ys[j + 1], ys[j]))
            j += 1
    return faces


class _Fail(Exception):
    pass


class GroundFillCore:
    """Algorithm shared by the operator and the live preview; parameters are read from self."""

    def _run(self, bm, obj):
        mw = obj.matrix_world
        inv = mw.inverted()
        nmat = inv.transposed().to_3x3()

        chains, err = _chains_from_selection(bm)
        if err:
            raise _Fail(err)
        if chains is None or len(chains) != 2:
            raise _Fail("Выделите ровно две отдельные цепочки рёбер")
        (a, a_closed), (b, b_closed) = chains
        if a_closed != b_closed:
            raise _Fail("Обе цепочки должны быть открытыми или обе замкнутыми")
        closed = a_closed
        if len(a) < 2 or len(b) < 2:
            raise _Fail("В каждой цепочке нужно хотя бы одно ребро")
        b = _align_chains(a, b, closed, mw)

        wa = [mw @ v.co for v in a]
        wb = [mw @ v.co for v in b]
        pa, len_a = _arc_params(wa, closed)
        pb, len_b = _arc_params(wb, closed)
        length = 0.5 * (len_a + len_b)
        probe = [k / 16 for k in range(17)]
        width = sum((_eval(wa, pa, s, closed)[0] - _eval(wb, pb, s, closed)[0]).length for s in probe) / len(probe)

        rows = self.rows
        segs = self.segments
        if segs <= 0:
            cell = max(1e-6, width / (rows + 1))
            segs = max(1, round(length / cell))
        if closed:
            segs = max(3, segs)
        cols = [k / segs for k in range(segs + (0 if closed else 1))]

        normals_a = self._side_normals(bm, a, closed, nmat)
        normals_b = self._side_normals(bm, b, closed, nmat)

        # Columns: points on both chains, cross slopes from the neighbouring faces
        col_a, col_b, slope_a, slope_b = [], [], [], []
        for s in cols:
            pa_pt, ia = _eval(wa, pa, s, closed)
            pb_pt, ib = _eval(wb, pb, s, closed)
            col_a.append(pa_pt)
            col_b.append(pb_pt)
            d = pb_pt - pa_pt
            secant = d.z
            slope_a.append(self._slope(normals_a[ia], d, secant))
            slope_b.append(self._slope(normals_b[ib], d, secant))

        # Interior grid positions (XY), then relax so lines stay even on bends
        ts = [r / (rows + 1) for r in range(1, rows + 1)]
        grid = [[col_a[k].lerp(col_b[k], t) for k in range(len(cols))] for t in ts]
        self._relax(grid, col_a, col_b, closed)

        taper_len = self.end_taper
        new_grid = []
        for r, t in enumerate(ts):
            row = []
            tb = _bias_t(t, self.bias)
            for k, s in enumerate(cols):
                co = grid[r][k]
                z = _hermite(col_a[k].z, col_b[k].z, slope_a[k], slope_b[k], t)
                taper = 1.0
                if not closed and taper_len > 0:
                    edge_dist = min(s, 1.0 - s) / taper_len
                    taper = 0.5 - 0.5 * math.cos(math.pi * min(1.0, edge_dist))
                z += self.bulge * _profile(tb, self.shape) * taper
                row.append(bm.verts.new(inv @ Vector((co.x, co.y, z))))
            new_grid.append(row)

        uv_of = {}
        for i, v in enumerate(a):
            uv_of[v] = (0.0, pa[i])
        for i, v in enumerate(b):
            uv_of[v] = (1.0, pb[i])
        for r, row in enumerate(new_grid):
            for k, v in enumerate(row):
                uv_of[v] = (ts[r], cols[k])

        quads = []
        wrap = (lambda lst: lst + [lst[0]]) if closed else (lambda lst: lst)
        col_params = cols + ([1.0] if closed else [])
        a_list, a_par = wrap(list(a)), pa
        b_list, b_par = wrap(list(b)), pb
        if rows == 0:
            quads += _ladder(a_list, a_par, b_list, b_par)
        else:
            quads += _ladder(a_list, a_par, wrap(new_grid[0]), col_params)
            for r in range(rows - 1):
                lo, hi = wrap(new_grid[r]), wrap(new_grid[r + 1])
                for k in range(len(lo) - 1):
                    quads.append((lo[k], lo[k + 1], hi[k + 1], hi[k]))
            quads += _ladder(wrap(new_grid[-1]), col_params, b_list, b_par)

        faces = []
        for q in quads:
            q = list(dict.fromkeys(q))
            if len(q) < 3 or bm.faces.get(q):
                continue
            faces.append(bm.faces.new(q))
        bm.normal_update()
        self._orient(faces, a + b)
        self._assign_material(faces, a + b)
        if self.uv_strip:
            self._write_uv(bm, faces, uv_of, width, length, closed)
        return faces

    @staticmethod
    def _side_normals(bm, chain, closed, nmat):
        pts = chain + ([chain[0]] if closed else [])
        normals = []
        for v0, v1 in zip(pts, pts[1:]):
            e = bm.edges.get((v0, v1))
            n = Vector()
            if e:
                for f in e.link_faces:
                    n += nmat @ f.normal
            normals.append(n.normalized() if n.length > 1e-9 else None)
        return normals

    def _slope(self, n, d, secant):
        if self.slope_mode == 'LINEAR':
            return secant
        if self.slope_mode == 'EASE':
            return 0.0
        # Rise of the neighbouring surface continued across the fill (z per whole cross distance)
        horiz = Vector((d.x, d.y, 0.0))
        if n is None or abs(n.z) < 0.2 or horiz.length < 1e-9:
            return secant
        h = horiz.normalized()
        rise = -(n.x * h.x + n.y * h.y) / n.z * horiz.length
        limit = max(abs(secant) * 3.0, horiz.length * 0.5)
        return max(-limit, min(limit, rise))

    def _relax(self, grid, col_a, col_b, closed):
        rows = len(grid)
        if rows == 0:
            return
        n = len(grid[0])
        for _ in range(self.relax):
            new = [[p.copy() for p in row] for row in grid]
            for r in range(rows):
                for k in range(n):
                    if not closed and (k == 0 or k == n - 1):
                        continue
                    left = grid[r][(k - 1) % n]
                    right = grid[r][(k + 1) % n]
                    down = col_a[k] if r == 0 else grid[r - 1][k]
                    up = col_b[k] if r == rows - 1 else grid[r + 1][k]
                    avg = (left + right + down + up) * 0.25
                    new[r][k] = grid[r][k].lerp(avg, 0.5)
            grid[:] = new

    def _orient(self, faces, chain_verts):
        new = set(faces)
        flip_all = None
        for v in chain_verts:
            for e in v.link_edges:
                olds = [f for f in e.link_faces if f not in new]
                news = [f for f in e.link_faces if f in new]
                if olds and news:
                    def start(f):
                        for l in f.loops:
                            if l.edge is e:
                                return l.vert
                    flip_all = start(olds[0]) is start(news[0])
                    break
            if flip_all is not None:
                break
        if flip_all is None:
            flip_all = sum((f.normal for f in faces), Vector()).z < 0
        if flip_all != self.flip:
            for f in faces:
                f.normal_flip()

    @staticmethod
    def _assign_material(faces, chain_verts):
        new = set(faces)
        src = None
        for v in chain_verts:
            src = next((f for f in v.link_faces if f not in new), None)
            if src:
                break
        if src:
            for f in faces:
                f.material_index = src.material_index
                f.smooth = src.smooth

    def _write_uv(self, bm, faces, uv_of, width, length, closed):
        layer = bm.loops.layers.uv.active
        if layer is None:
            return
        for f in faces:
            vs = [uv_of[v][1] for v in f.verts if v in uv_of]
            wrap = closed and vs and max(vs) - min(vs) > 0.5
            for l in f.loops:
                t, s = uv_of.get(l.vert, (0.0, 0.0))
                if wrap and s < 0.5:
                    s += 1.0
                l[layer].uv = (t * width * self.uv_scale, s * length * self.uv_scale)


class MESH_OT_ground_fill(GroundFillCore, bpy.types.Operator):
    """Залить «умный» рельеф между двумя выделенными цепочками рёбер"""
    bl_idname = "mesh.ground_fill"
    bl_label = "Ground Fill"
    bl_options = {'REGISTER', 'UNDO'}

    rows: IntProperty(
        name="Рядов", default=3, min=0, max=200,
        description="Сколько линий провести между цепочками (параллельно им). Больше — граней больше")
    segments: IntProperty(
        name="Сегментов", default=0, min=0, max=2000,
        description="Сколько сегментов вдоль. 0 — подобрать так, чтобы ячейки были почти квадратными")
    slope_mode: EnumProperty(
        name="Склон", default='SMART',
        items=[
            ('SMART', "Рельеф", "Поверхность продолжает уклон соседних граней — без излома на стыке"),
            ('EASE', "Плавный", "Плавно выходит горизонтально от обоих краёв"),
            ('LINEAR', "Прямой", "Прямой уклон от края до края"),
        ])
    relax: IntProperty(
        name="Выравнивание", default=4, min=0, max=50,
        description="Сколько раз сгладить сетку: линии на изгибах становятся ровнее")
    bulge: FloatProperty(
        name="Подъём", default=0.0, soft_min=-20.0, soft_max=20.0, unit='LENGTH',
        description="Поднять землю вверх (+) или прогнуть вниз (−), метры по мировой Z")
    shape: EnumProperty(
        name="Профиль", default='SMOOTH',
        items=[
            ('SMOOTH', "Плавный", "Плавный холм (синусоида)"),
            ('SPHERE', "Круглый", "Круглый, круто у краёв"),
            ('LINEAR', "Гребень", "Прямые склоны с острым гребнем"),
            ('SHARP', "Острый", "Узкий пик"),
            ('FLAT', "Плато", "Ровный верх и склоны у краёв"),
        ])
    bias: FloatProperty(
        name="Сдвиг вершины", default=0.0, min=-1.0, max=1.0, subtype='FACTOR',
        description="Сдвинуть вершину холма к первой цепочке (−) или ко второй (+)")
    end_taper: FloatProperty(
        name="Затухание у концов", default=0.1, min=0.0, max=0.5, subtype='FACTOR',
        description="Плавно убрать подъём у открытых концов (доля длины)")
    flip: BoolProperty(name="Перевернуть нормали", default=False,
                       description="Развернуть новые грани, если они смотрят не туда")
    uv_strip: BoolProperty(
        name="UV полосой", default=True,
        description="Записать UV в активный слой: U поперёк, V вдоль")
    uv_scale: FloatProperty(name="UV на метр", default=0.1, min=0.0001, soft_max=10.0,
                            description="Сколько единиц UV приходится на метр")
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
        self.report({'INFO'}, f"Ground Fill: {len(faces)} граней")
        return {'FINISHED'}


PARAM_KEYS = ("rows", "segments", "slope_mode", "relax", "bulge", "shape", "bias", "end_taper",
              "flip", "uv_strip", "uv_scale")


def draw_params(layout, data, prefix):
    col = layout.column()
    row = col.row(align=True)
    row.prop(data, prefix + "rows")
    row.prop(data, prefix + "segments")
    col.prop(data, prefix + "slope_mode")
    col.prop(data, prefix + "relax")
    col.separator()
    col.prop(data, prefix + "bulge")
    col.prop(data, prefix + "shape")
    col.prop(data, prefix + "bias")
    col.prop(data, prefix + "end_taper")
    col.separator()
    col.prop(data, prefix + "flip")
    col.prop(data, prefix + "uv_strip")
    sub = col.column()
    sub.enabled = getattr(data, prefix + "uv_strip")
    sub.prop(data, prefix + "uv_scale")


def _menu(self, context):
    self.layout.separator()
    self.layout.operator(MESH_OT_ground_fill.bl_idname, icon='MOD_DISPLACE')


_MENUS = (
    bpy.types.VIEW3D_MT_edit_mesh_edges,
    bpy.types.VIEW3D_MT_edit_mesh_context_menu,
)


def register():
    bpy.utils.register_class(MESH_OT_ground_fill)
    for m in _MENUS:
        m.append(_menu)


def unregister():
    for m in _MENUS:
        m.remove(_menu)
    bpy.utils.unregister_class(MESH_OT_ground_fill)
