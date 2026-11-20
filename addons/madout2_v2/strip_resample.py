# =====================================================
# Strip Resample — равномерно перераспределяет поперечные рёбра
# полосы между параллельными линиями по гладкой кривой (смешение
# окружностей: дуга остаётся дугой), с высотами, UV и сваркой.
# Edit Mode → выделить линии вдоль полосы → Ctrl+E → Strip Resample.
# =====================================================

import math
from collections import Counter

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import barycentric_transform

from .ui_help import draw_help

_DENSE = 24
_INF = float("inf")

HELP = (
    "Что делает: расставляет поперечные рёбра полосы",
    "равномерно по плавной кривой (дуга остаётся дугой),",
    "с учётом высот. Соседняя геометрия остаётся сварена,",
    "UV и цвета вершин переносятся.",
    "",
    "Что выделить: всю полосу (грани или вершины,",
    "концы могут быть ступенькой) или только линии",
    "вдоль неё (Alt+клик / Ctrl+клик).",
    "",
    "Сегментов — сколько шагов вдоль полосы:",
    "  больше — граней больше, меньше — реже.",
    "Шаг — то же, но расстоянием в метрах.",
    "Плавность — 0: по старым рёбрам, 1: по кривой.",
    "Сохранять углы — изломы круче «Угла»",
    "  не скругляются, на них встаёт ребро.",
    "Концы полосы не сдвигаются. Откат — Ctrl+Z.",
)


def _chains_from_selection(bm):
    """Returns list of ordered vertex lists for every connected component of selected edges, or an error string."""
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
            return None, "Выделенные линии ветвятся: выделите полосу гранями или каждую линию отдельной цепочкой рёбер"
        ends = [v for v in comp if len(adj[v]) == 1]
        if not ends:
            return None, "Замкнутые кольца пока не поддерживаются: выделите незамкнутые линии"
        order = [ends[0]]
        prev = None
        v = ends[0]
        while True:
            nxt = [e for e in adj[v] if e is not prev]
            if not nxt:
                break
            prev = nxt[0]
            v = prev.other_vert(v)
            order.append(v)
        chains.append(order)
    return chains, None


def _boundary_loop(region):
    edges = {e for f in region for e in f.edges if sum(1 for g in e.link_faces if g in region) == 1}
    adj = {}
    for e in edges:
        for v in e.verts:
            adj.setdefault(v, []).append(e)
    if not adj or any(len(x) != 2 for x in adj.values()):
        return None
    start = next(iter(adj))
    loop = [start]
    prev = None
    v = start
    while True:
        e = next(x for x in adj[v] if x is not prev)
        prev = e
        v = e.other_vert(v)
        if v is start:
            break
        loop.append(v)
    return loop if len(loop) == len(adj) else None


def _geodesic(sources, region_edges):
    import heapq
    dist = {v: 0.0 for v in sources}
    heap = [(0.0, id(v), v) for v in sources]
    heapq.heapify(heap)
    while heap:
        d, _, v = heapq.heappop(heap)
        if d > dist[v]:
            continue
        for e in v.link_edges:
            if e not in region_edges:
                continue
            w = e.other_vert(v)
            nd = d + e.calc_length()
            if nd < dist.get(w, _INF):
                dist[w] = nd
                heapq.heappush(heap, (nd, id(w), w))
    return dist


def _turn(loop, i):
    a, b, c = loop[i - 1].co, loop[i].co, loop[(i + 1) % len(loop)].co
    d0, d1 = b - a, c - b
    return d0.angle(d1) if d0.length > 1e-9 and d1.length > 1e-9 else 0.0


def _strip_ends(loop):
    """Splits the outline at sharp turns; the two longest smooth pieces are the sides,
    what lies between them (possibly stepped) are the two ends."""
    n = len(loop)
    breaks = [i for i in range(n) if _turn(loop, i) > math.radians(50.0)]
    if len(breaks) < 4:
        return None
    pieces = []
    for k, b in enumerate(breaks):
        nb = breaks[(k + 1) % len(breaks)]
        idx = [i % n for i in range(b, nb + 1 if nb > b else nb + n + 1)]
        pieces.append(idx)
    plen = [_arc([loop[i].co for i in p])[-1] for p in pieces]
    m = len(pieces)
    best = None
    for i in range(m):
        for j in range(i + 2, m):
            if i == 0 and j == m - 1:
                continue
            total = plen[i] + plen[j]
            if best is None or total > best[0]:
                best = (total, i, j)
    if best is None:
        return None
    _, i, j = best
    end1 = [loop[x] for k in range(i + 1, j) for x in pieces[k]]
    end2 = [loop[x] for k in list(range(j + 1, m)) + list(range(0, i)) for x in pieces[k]]
    end1 = list(dict.fromkeys([loop[pieces[i][-1]]] + end1 + [loop[pieces[j][0]]]))
    end2 = list(dict.fromkeys([loop[pieces[j][-1]]] + end2 + [loop[pieces[i][0]]]))
    return end1, end2


def _chains_from_region(region):
    """Ordered lines along a selected strip of faces, from one end of the strip to the other.
    Works with stepped ends and with triangles inside the strip."""
    loop = _boundary_loop(region)
    if loop is None:
        return None, "Выделенные грани должны быть одной полосой без дыр"
    ends = _strip_ends(loop)
    if ends is None:
        return None, "Не удалось найти два конца полосы: выделите полосу целиком или линии вдоль неё"
    end_a, end_b = ends
    region_edges = {e for f in region for e in f.edges}
    verts = {v for f in region for v in f.verts}

    # Station along the strip: 0 on one end, 1 on the other (geodesic, bends with the strip)
    da = _geodesic(end_a, region_edges)
    db = _geodesic(end_b, region_edges)
    if any(v not in da or v not in db for v in verts):
        return None, "Выделенные грани должны быть связаны"
    station = {v: da[v] / max(1e-9, da[v] + db[v]) for v in verts}
    length = sum(da[v] + db[v] for v in verts) / len(verts)

    def along(e):
        a, b = e.verts
        return abs(station[a] - station[b]) * length / max(1e-9, e.calc_length())

    end_b_set = set(end_b)
    chains = []
    used = set()
    for v in end_a:
        chain = [v]
        cur = v
        for _ in range(len(verts)):
            if cur in end_b_set and len(chain) > 1:
                break
            best = None
            for e in cur.link_edges:
                if e not in region_edges:
                    continue
                w = e.other_vert(cur)
                if w in used or w in chain or station[w] <= station[cur]:
                    continue
                score = along(e)
                if best is None or score > best[0]:
                    best = (score, w)
            if best is None:
                return None, "Не удалось провести линию вдоль полосы: выделите линии вручную"
            cur = best[1]
            chain.append(cur)
        if chain[-1] not in end_b_set:
            return None, "Не удалось провести линию вдоль полосы: выделите линии вручную"
        used |= set(chain)
        chains.append(chain)
    return chains, None


def _arc(points):
    acc = [0.0]
    for a, b in zip(points, points[1:]):
        acc.append(acc[-1] + (b - a).length)
    return acc


def _arc_point(a, b, c, u, from_b):
    """Point at fraction u along the arc of the circle through a, b, c.
    from_b=False: arc a->b (the span next to c is b->c); from_b=True: arc b->c."""
    ab, ac = b - a, c - a
    n = ab.cross(ac)
    if n.length < 1e-9 * max(1.0, ab.length * ac.length):
        return (b.lerp(c, u) if from_b else a.lerp(b, u))
    center = a + (n.cross(ab) * ac.length_squared + ac.cross(n) * ab.length_squared) / (2.0 * n.length_squared)
    start, end = (b, c) if from_b else (a, b)
    r0, r1 = start - center, end - center
    nn = n.normalized()
    e1 = r0.normalized()
    e2 = nn.cross(e1)
    ang = math.atan2(r1.dot(e2), r1.dot(e1))
    rad0, rad1 = r0.length, r1.length
    t = ang * u
    radius = rad0 + (rad1 - rad0) * u
    return center + (e1 * math.cos(t) + e2 * math.sin(t)) * radius


def _blend_segment(p0, p1, p2, p3, u):
    # Circle blend: a true arc stays an arc, a straight line stays straight, C1-smooth between spans.
    left = _arc_point(p0, p1, p2, u, True) if p0 is not None else None
    right = _arc_point(p1, p2, p3, u, False) if p3 is not None else None
    if left is None and right is None:
        return p1.lerp(p2, u)
    if left is None:
        return right
    if right is None:
        return left
    return left.lerp(right, u)


def _corner_indices(pts, angle):
    corners = set()
    for i in range(1, len(pts) - 1):
        d0 = pts[i] - pts[i - 1]
        d1 = pts[i + 1] - pts[i]
        if d0.length > 1e-9 and d1.length > 1e-9 and d0.angle(d1) > angle:
            corners.add(i)
    return corners


def _dense_curve(pts, smooth, corners):
    out = [pts[0].copy()]
    n = len(pts)
    for i in range(n - 1):
        p1, p2 = pts[i], pts[i + 1]
        p0 = pts[i - 1] if i > 0 and i not in corners else None
        p3 = pts[i + 2] if i + 2 < n and (i + 1) not in corners else None
        for s in range(1, _DENSE + 1):
            u = s / _DENSE
            straight = p1.lerp(p2, u)
            out.append(straight.lerp(_blend_segment(p0, p1, p2, p3, u), smooth) if smooth > 0 else straight)
    out[-1] = pts[-1].copy()
    return out


def _sample(dense, count):
    acc = _arc(dense)
    total = acc[-1]
    result = []
    j = 0
    for k in range(count + 1):
        s = total * k / count
        while j < len(acc) - 2 and acc[j + 1] < s:
            j += 1
        span = acc[j + 1] - acc[j]
        f = 0.0 if span <= 0 else (s - acc[j]) / span
        result.append(dense[j].lerp(dense[j + 1], min(1.0, max(0.0, f))))
    result[0] = dense[0].copy()
    result[-1] = dense[-1].copy()
    return result


def _match(params, anchored, targets):
    """Monotonic assignment of existing interior verts to target params.
    Returns per-target index of matched vert (or None) and the set of vert indices to remove."""
    n, m = len(params), len(targets)
    skip_vert = [(_INF if a else 0.0004) for a in anchored]
    skip_target = 0.0004
    dp = [[_INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            cur = dp[i][j]
            if cur == _INF:
                continue
            if i < n and j < m:
                c = cur + (params[i] - targets[j]) ** 2
                if c < dp[i + 1][j + 1]:
                    dp[i + 1][j + 1], back[i + 1][j + 1] = c, 'M'
            if i < n and cur + skip_vert[i] < dp[i + 1][j]:
                dp[i + 1][j], back[i + 1][j] = cur + skip_vert[i], 'V'
            if j < m and cur + skip_target < dp[i][j + 1]:
                dp[i][j + 1], back[i][j + 1] = cur + skip_target, 'T'
    if dp[n][m] == _INF:
        return None, None
    assign = [None] * m
    removed = set()
    i, j = n, m
    while i or j:
        step = back[i][j]
        if step == 'M':
            assign[j - 1] = i - 1
            i, j = i - 1, j - 1
        elif step == 'V':
            removed.add(i - 1)
            i -= 1
        else:
            j -= 1
    return assign, removed


class StripResampleCore:
    """Algorithm shared by the operator and the live preview; parameters are read from self."""

    def _run(self, bm, obj):
        region = {f for f in bm.faces if f.select}
        if region:
            chains, err = _chains_from_region(region)
        else:
            chains, err = _chains_from_selection(bm)
        if err:
            raise _Fail(err)
        if not chains or len(chains) < 2:
            raise _Fail("Выделите полосу (грани/вершины) или хотя бы две линии вдоль неё")
        if not region:
            chains = self._order_chains(chains)

        old_segments = max(len(c) - 1 for c in chains)
        pts = [[v.co.copy() for v in c] for c in chains]
        lengths = [_arc(p)[-1] for p in pts]
        if self.mode == 'COUNT':
            if self.count == 0:
                # Show the real current number in the F9 panel, so it can be dragged up or down
                self.count = old_segments
            count = self.count
        else:
            unit = obj.matrix_world.to_scale()
            scale = (abs(unit.x) + abs(unit.y) + abs(unit.z)) / 3.0
            count = max(1, round(sum(lengths) / len(lengths) * scale / self.spacing))

        strips = self._capture_strips(bm, chains)
        all_band = set(f for s in strips for f in s["faces"])
        chain_edge_set = {bm.edges.get((a, b)) for c in chains for a, b in zip(c, c[1:])}
        band_edges = {e for f in all_band for e in f.edges} - chain_edge_set
        bmesh.ops.delete(bm, geom=list(all_band), context='FACES_ONLY')
        for e in band_edges:
            if e.is_valid and not e.link_faces:
                bm.edges.remove(e)

        new_chains = []
        for chain, p in zip(chains, pts):
            corners = _corner_indices(p, self.corner_angle) if self.keep_corners else set()
            targets = _sample(_dense_curve(p, self.smooth, corners), count)
            for ci in corners:
                k = min(range(1, count), key=lambda k: (targets[k] - p[ci]).length, default=None)
                if k is not None:
                    targets[k] = p[ci].copy()
            new_chains.append(self._rebuild_chain(bm, chain, p, targets, chains))

        faces = []
        for i, s in enumerate(strips):
            a, b = new_chains[i], new_chains[i + 1]
            for k in range(count):
                quad = [a[k], a[k + 1], b[k + 1], b[k]]
                if len(set(quad)) < 3:
                    continue
                quad = list(dict.fromkeys(quad))
                if bm.faces.get(quad):
                    continue
                f = bm.faces.new(quad)
                f.normal_update()
                if s["normal"].length > 0 and f.normal.dot(s["normal"]) < 0:
                    f.normal_flip()
                f.material_index = s["material"]
                f.smooth = s["smooth"]
                faces.append(f)
                self._interpolate_loops(bm, f, s)

        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False
        for v in bm.verts:
            v.select = False
        if region:
            for f in faces:
                f.select = True
        else:
            for c in new_chains:
                for v0, v1 in zip(c, c[1:]):
                    e = bm.edges.get((v0, v1))
                    if e:
                        e.select = True
        bm.select_flush(True)
        bm.normal_update()
        return faces

    @staticmethod
    def _order_chains(chains):
        # Direction of all chains follows the first one; chains are sorted across the strip.
        ref = chains[0]
        out = [ref]
        for c in chains[1:]:
            straight = (c[0].co - ref[0].co).length + (c[-1].co - ref[-1].co).length
            crossed = (c[-1].co - ref[0].co).length + (c[0].co - ref[-1].co).length
            out.append(c[::-1] if crossed < straight else c)
        mid = ref[len(ref) // 2].co
        lo = max(0, len(ref) // 2 - 1)
        hi = min(len(ref) - 1, len(ref) // 2 + 1)
        tangent = (ref[hi].co - ref[lo].co).normalized()
        far = max(out, key=lambda c: (c[len(c) // 2].co - mid).length)
        side = far[len(far) // 2].co - mid
        side = side - tangent * side.dot(tangent)
        return sorted(out, key=lambda c: (c[len(c) // 2].co - mid).dot(side))

    def _capture_strips(self, bm, chains):
        sets = [set(c) for c in chains]
        strips = []
        uv_layers = list(bm.loops.layers.uv.values())
        col_layers = list(bm.loops.layers.color.values()) + list(bm.loops.layers.float_color.values())
        for i in range(len(chains) - 1):
            pair = sets[i] | sets[i + 1]
            faces = [f for f in bm.faces
                     if all(v in pair for v in f.verts)
                     and any(v in sets[i] for v in f.verts) and any(v in sets[i + 1] for v in f.verts)]
            if not faces:
                raise _Fail(f"Нет граней между линиями {i + 1} и {i + 2}: выделите соседние параллельные линии")
            tri_co, tri_data = [], []
            normal = Vector()
            for f in faces:
                normal += f.normal * f.calc_area()
                loops = list(f.loops)
                for j in range(1, len(loops) - 1):
                    tri = (loops[0], loops[j], loops[j + 1])
                    tri_co.append(tuple(l.vert.co.copy() for l in tri))
                    tri_data.append(tuple(
                        ([l[lay].uv.copy() for lay in uv_layers], [Vector(l[lay]) for lay in col_layers])
                        for l in tri))
            verts = [c for t in tri_co for c in t]
            polys = [(3 * k, 3 * k + 1, 3 * k + 2) for k in range(len(tri_co))]
            strips.append({
                "faces": faces,
                "material": Counter(f.material_index for f in faces).most_common(1)[0][0],
                "smooth": Counter(f.smooth for f in faces).most_common(1)[0][0],
                "normal": normal.normalized() if normal.length > 0 else normal,
                "tri_co": tri_co, "tri_data": tri_data,
                "bvh": BVHTree.FromPolygons(verts, polys),
                "uv_layers": uv_layers, "col_layers": col_layers,
            })
        return strips

    def _rebuild_chain(self, bm, chain, old_pts, targets, all_chains):
        chain_edges = {bm.edges.get((a, b)) for a, b in zip(chain, chain[1:])}
        interior = chain[1:-1]
        acc = _arc(old_pts)
        total = acc[-1] or 1.0
        params = [acc[i] / total for i in range(1, len(chain) - 1)]
        anchored = [self._anchored(v, chain_edges) for v in interior]
        count = len(targets) - 1
        target_params = [k / count for k in range(1, count)]
        assign, removed = _match(params, anchored, target_params)
        if assign is None:
            raise _Fail(f"Слишком мало сегментов: на линии {sum(anchored)} вершин связаны с другой геометрией")

        for idx in sorted(removed):
            v = interior[idx]
            e = next(e for e in v.link_edges)
            bmesh.utils.vert_collapse_edge(v, e)

        kept = [chain[0]] + [interior[i] for i in range(len(interior)) if i not in removed] + [chain[-1]]
        slots = [interior[a] if a is not None else None for a in assign]
        result = [chain[0]]
        ki = 0
        pending = 0
        for s in slots + [chain[-1]]:
            if s is None:
                pending += 1
                continue
            a = kept[ki]
            b = s
            cur = a
            for j in range(pending):
                edge = bm.edges.get((cur, b))
                if edge is None:
                    raise _Fail("Рёбра линии разорваны, не удалось вставить вершины")
                _, nv = bmesh.utils.edge_split(edge, cur, 1.0 / (pending - j + 1))
                result.append(nv)
                cur = nv
            result.append(b)
            ki += 1
            pending = 0
        for v, co in zip(result, targets):
            v.co = co
        return result

    @staticmethod
    def _anchored(v, chain_edges):
        if any(e not in chain_edges for e in v.link_edges):
            return True
        return any(len(f.verts) <= 3 for f in v.link_faces)

    @staticmethod
    def _interpolate_loops(bm, f, s):
        for l in f.loops:
            co = l.vert.co
            loc, _, idx, _ = s["bvh"].find_nearest(co)
            if idx is None:
                continue
            a, b, c = s["tri_co"][idx]
            # Barycentric of the real point (not the clamped nearest one) extrapolates linearly
            # when the smoothed curve bulges outside the old chords.
            w = barycentric_transform(co, a, b, c, Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
            data = s["tri_data"][idx]
            for li, lay in enumerate(s["uv_layers"]):
                l[lay].uv = data[0][0][li] * w.x + data[1][0][li] * w.y + data[2][0][li] * w.z
            for li, lay in enumerate(s["col_layers"]):
                l[lay] = data[0][1][li] * w.x + data[1][1][li] * w.y + data[2][1][li] * w.z


class MESH_OT_strip_resample(StripResampleCore, bpy.types.Operator):
    """Равномерно перераспределить поперечные рёбра полосы по плавной кривой (больше или меньше граней)"""
    bl_idname = "mesh.strip_resample"
    bl_label = "Strip Resample"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Режим", default='COUNT',
        items=[
            ('COUNT', "Количество", "Задать, сколько сегментов будет вдоль полосы"),
            ('SPACING', "Шаг", "Задать расстояние между поперечными рёбрами"),
        ])
    count: IntProperty(
        name="Сегментов", default=0, min=0, max=10000, options={'SKIP_SAVE'},
        description="Сколько сегментов вдоль полосы. Больше — граней больше, меньше — граней меньше. "
                    "При запуске подставляется текущее количество")
    spacing: FloatProperty(
        name="Шаг", default=2.0, min=0.01, soft_max=50.0, unit='LENGTH',
        description="Расстояние между поперечными рёбрами вдоль полосы. Меньше шаг — граней больше")
    smooth: FloatProperty(
        name="Плавность", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description="0 — точки идут по старым прямым рёбрам, 1 — по плавной кривой через вершины (дуга остаётся дугой)")
    keep_corners: BoolProperty(
        name="Сохранять углы", default=True,
        description="Острые изломы линии не скругляются: на каждый угол встаёт поперечное ребро")
    corner_angle: FloatProperty(
        name="Угол", default=math.radians(35.0), min=math.radians(1.0), max=math.radians(179.0),
        subtype='ANGLE', description="Излом круче этого угла считается острым углом")
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
            n_faces = len(self._run(bm, obj))
        except _Fail as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f"Strip Resample: {n_faces} граней")
        return {'FINISHED'}


PARAM_KEYS = ("mode", "count", "spacing", "smooth", "keep_corners", "corner_angle")


def draw_params(layout, data, prefix, count_text=None):
    col = layout.column()
    col.row().prop(data, prefix + "mode", expand=True)
    if getattr(data, prefix + "mode") == 'COUNT':
        col.prop(data, prefix + "count", text=count_text) if count_text else col.prop(data, prefix + "count")
    else:
        col.prop(data, prefix + "spacing")
    col.prop(data, prefix + "smooth")
    row = col.row(align=True)
    row.prop(data, prefix + "keep_corners")
    sub = row.row()
    sub.active = getattr(data, prefix + "keep_corners")
    sub.prop(data, prefix + "corner_angle", text="")


class _Fail(Exception):
    pass


def _menu(self, context):
    self.layout.operator(MESH_OT_strip_resample.bl_idname, icon='CENTER_ONLY')


_MENUS = (
    bpy.types.VIEW3D_MT_edit_mesh_edges,
    bpy.types.VIEW3D_MT_edit_mesh_context_menu,
)


def register():
    bpy.utils.register_class(MESH_OT_strip_resample)
    for m in _MENUS:
        m.append(_menu)


def unregister():
    for m in _MENUS:
        m.remove(_menu)
    bpy.utils.unregister_class(MESH_OT_strip_resample)
