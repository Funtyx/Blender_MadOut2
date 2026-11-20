# =====================================================
# Road Remesh — пересобирает сетку всей дорожной сети: полосы между
# бордюрами, изгибы и развязки. Скругления делятся по углу (одинаково
# на всех сторонах), прямые — по длине ребра. На развязках полосы идут
# насквозь через сетку в центре, а скругления сходятся к её углам веером.
# Бордюры пересобираются по тем же точкам: ряды, высоты и UV сохраняются.
# Edit Mode → выделить дорогу с бордюрами (A) → Road Remesh.
# =====================================================

import bisect
import math

import bmesh
import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from .strip_resample import _blend_segment
from .ui_help import draw_help

_D = 16                 # dense points per old edge
_SAMPLE = 0.5           # pairing ray spacing, m
_ARC_TURN = math.radians(1.5)
_MIN_BEND = math.radians(1.0)
_LONG_EDGE = 8.0        # a kink between edges this long is a corner, not a sparse curve
_OPPOSITE = math.cos(math.radians(25.0))
_EPS = 1e-6

HELP = (
    "Что делает: заново раскладывает",
    "сетку всей дороги. Полосы между",
    "бордюрами и изгибы — ровные квады,",
    "развязки — полосы идут насквозь",
    "через сетку в центре, скругления",
    "сходятся веером к её углам.",
    "Скругления остаются плавными.",
    "",
    "Что выделить: дорогу вместе",
    "с бордюрами (A — всё). Дорога —",
    "материал с наибольшей площадью,",
    "остальные грани — бордюры.",
    "",
    "Сегментов на 90° — рёбер на",
    "  скругление в 90°: меньший угол —",
    "  меньше, больший — больше.",
    "  Одинаковые скругления получают",
    "  одинаково со всех сторон.",
    "Длина ребра — шаг на прямых.",
    "Делений по ширине — продольных",
    "  полос дороги.",
    "Развязки: «Сетка» — квады,",
    "  «N-гон» — одна грань.",
    "Сглаживание — выравнивание сетки",
    "  в центре развязки.",
    "Угол излома — изломы круче",
    "  не скругляются.",
    "Макс. ширина — стороны дальше",
    "  не считаются одной дорогой.",
    "",
    "Бордюры: ряды, высоты и UV",
    "переносятся. Невыделенное",
    "не сдвигается. Откат — Ctrl+Z.",
)


class _Fail(Exception):
    pass


def _cross2(a, b):
    return a.x * b.y - a.y * b.x


def _signed_angle(a, b):
    return math.atan2(_cross2(a, b), a.dot(b))


class _Frame:
    def __init__(self, up):
        self.up = up.normalized()
        self.e1 = self.up.orthogonal().normalized()
        self.e2 = self.up.cross(self.e1)

    def to2(self, co):
        return Vector((co.dot(self.e1), co.dot(self.e2)))


def _smooth_dense(p, closed, corner, straight):
    """Circle-blend curve through points; corner verts and straight edges are not rounded.
    A long edge next to short ones stays straight, so a straight road does not bow into a fillet."""
    n = len(p)
    lens = [(p[(i + 1) % n] - p[i]).length for i in range(n)]

    def close(a, b):
        # long edges stay straight: a slope change between them is not a curve
        return 1e-9 < a < _LONG_EDGE and 1e-9 < b < _LONG_EDGE and 1.0 / 3.0 <= a / b <= 3.0

    def bent(a, b, c):
        # nearly straight joints give huge circles that single-precision maths cannot hold
        d0, d1 = b - a, c - b
        return d0.length > 1e-9 and d1.length > 1e-9 and d0.angle(d1) > _MIN_BEND

    out = []
    segs = n if closed else n - 1
    for i in range(segs):
        j = (i + 1) % n
        p1, p2 = p[i], p[j]
        p0 = p3 = None
        if i not in straight:
            if (closed or i > 0) and i not in corner and close(lens[i - 1], lens[i]) and bent(p[i - 1], p1, p2):
                p0 = p[i - 1]
            if (closed or j < n - 1) and j not in corner and close(lens[j], lens[i]) \
                    and bent(p1, p2, p[(j + 1) % n]):
                p3 = p[(j + 1) % n]
        for s in range(_D):
            out.append(_blend_segment(p0, p1, p2, p3, s / _D) if s else p1.copy())
    if not closed:
        out.append(p[-1].copy())
    return out


class _Arc:
    __slots__ = ("s0", "s1", "theta")

    def __init__(self, s0, s1, theta):
        self.s0, self.s1, self.theta = s0, s1, theta


class _Loop:
    """Closed boundary of the road region, road on the left."""

    def __init__(self, idx, verts, edges, types, frame, corner_angle):
        self.idx = idx
        self.verts = verts
        self.edges = edges
        self.types = types      # per edge v[i] -> v[i+1]: R curb, B open, C cap, X fixed
        self.frame = frame
        n = self.n = len(verts)
        self.p = [v.co.copy() for v in verts]
        q = [frame.to2(c) for c in self.p]
        self.turn = []
        for i in range(n):
            d0, d1 = q[i] - q[i - 1], q[(i + 1) % n] - q[i]
            ok = d0.length > 1e-9 and d1.length > 1e-9
            self.turn.append(_signed_angle(d0, d1) if ok else 0.0)
        self.elen = [(self.p[(i + 1) % n] - self.p[i]).length for i in range(n)]
        self.corner = set()
        self.sharp = set()      # real corners of the outline, not just kept kinks
        for i in range(n):
            t = abs(self.turn[i])
            if types[i - 1] != types[i] or 'X' in (types[i - 1], types[i]):
                self.corner.add(i)
                self.sharp.add(i)
            elif t >= corner_angle:
                self.corner.add(i)
                self.sharp.add(i)
            elif t >= math.radians(3.0) and max(abs(self.turn[i - 1]), abs(self.turn[(i + 1) % n])) < 0.3 * t:
                # a lone kink between straight edges is a corner, not a curve
                self.corner.add(i)
            elif t >= math.radians(2.0) and min(self.elen[i - 1], self.elen[i]) >= _LONG_EDGE:
                # a kink between long edges is part of the shape
                self.corner.add(i)
            elif max(self.elen[i - 1], self.elen[i]) >= _LONG_EDGE and self._slope_change(i) >= math.radians(0.5):
                # so is a change of slope next to a long edge
                self.corner.add(i)
        self.anchor = set(self.corner)
        self.build()

    def _slope_change(self, i):
        up = self.frame.up
        d0 = self.p[i] - self.p[i - 1]
        d1 = self.p[(i + 1) % self.n] - self.p[i]
        if d0.length < 1e-9 or d1.length < 1e-9:
            return 0.0
        return abs(math.asin(max(-1.0, min(1.0, d0.normalized().dot(up))))
                   - math.asin(max(-1.0, min(1.0, d1.normalized().dot(up)))))

    def build(self):
        straight = {i for i in range(self.n) if self.types[i] in ('X', 'C')}
        self.dense = _smooth_dense(self.p, True, self.corner, straight)
        self.dq = [self.frame.to2(c) for c in self.dense]
        self.N = len(self.dense)
        acc = [0.0]
        for k in range(self.N):
            acc.append(acc[-1] + (self.dense[(k + 1) % self.N] - self.dense[k]).length)
        self.acc = acc
        self.total = acc[-1]
        self._arcs()

    def _arcs(self):
        n = self.n
        curving = [i not in self.corner and abs(self.turn[i]) > _ARC_TURN for i in range(n)]
        # curvature at a vertex; a fillet next to a wide bend has a very different one
        kappa = [abs(self.turn[i]) / max(1e-6, min(self.elen[i - 1], self.elen[i])) for i in range(n)]

        def same_curve(a, b):
            if (self.turn[a] > 0) != (self.turn[b] > 0):
                return False
            ka, kb = kappa[a], kappa[b]
            return max(ka, kb) <= 3.0 * min(ka, kb)

        runs = []
        if all(curving) and all(same_curve(i - 1, i) for i in range(n)):
            runs.append(list(range(n)))
        else:
            start = next((i for i in range(n) if not curving[i] or not same_curve(i - 1, i)), 0)
            cur = []
            for k in range(n):
                i = (start + k) % n
                same = cur and same_curve(cur[-1], i)
                if curving[i] and (not cur or same):
                    cur.append(i)
                    continue
                if len(cur) >= 2:
                    runs.append(cur)
                cur = [i] if curving[i] else []
            if len(cur) >= 2:
                runs.append(cur)
        self.arcs = []
        for run in runs:
            s0 = self.vparam(run[0])
            s1 = self.vparam(run[-1])
            if len(run) == n:
                s1 = s0 + self.total
            elif s1 <= s0:
                s1 += self.total
            self.arcs.append(_Arc(s0, s1, sum(self.turn[i] for i in run)))

    def breaks(self, anchors_only=False):
        out = [self.vparam(i) for i in self.anchor]
        if anchors_only:
            return sorted(set(round(x, 6) for x in out))
        for a in self.arcs:
            if abs(a.theta) >= math.radians(10.0) and a.s1 - a.s0 < self.total - 1e-6:
                out += [a.s0 % self.total, a.s1 % self.total]
        return sorted(set(round(x, 6) for x in out))

    def vparam(self, i):
        return self.acc[(i % self.n) * _D]

    def _j(self, s):
        s %= self.total
        j = bisect.bisect_right(self.acc, s) - 1
        return min(max(j, 0), self.N - 1), s

    def at(self, s):
        j, s = self._j(s)
        span = self.acc[j + 1] - self.acc[j]
        f = (s - self.acc[j]) / span if span > 1e-12 else 0.0
        return self.dense[j].lerp(self.dense[(j + 1) % self.N], f)

    def at2(self, s):
        return self.frame.to2(self.at(s))

    def seg_frac(self, s):
        """Old edge index and fraction of the smooth arc along it."""
        j, s = self._j(s)
        k = j // _D
        a, b = self.acc[k * _D], self.acc[(k + 1) * _D]
        return k, (s - a) / (b - a) if b - a > 1e-12 else 0.0

    def edge_type(self, s):
        return self.types[self.seg_frac(s)[0]]

    def tangent2(self, s):
        j, _ = self._j(s)
        d = self.dq[(j + 1) % self.N] - self.dq[j]
        return d.normalized() if d.length > 1e-12 else Vector((1.0, 0.0))

    def normal2(self, s):
        t = self.tangent2(s)
        return Vector((-t.y, t.x))

    def curvature(self, s, h=0.5):
        return _signed_angle(self.tangent2(s - h), self.tangent2(s + h)) / (2.0 * h)

    def arc_at(self, s):
        for a in self.arcs:
            if (s - a.s0) % self.total < a.s1 - a.s0:
                return a
        return None

    def pieces(self, a, b, anchors_only=False):
        cuts = sorted({x + k * self.total for x in self.breaks(anchors_only) for k in (-1, 0, 1, 2)
                       if a + 1e-5 < x + k * self.total < b - 1e-5})
        pts = [a] + cuts + [b]
        return [(s0, s1) for s0, s1 in zip(pts, pts[1:]) if s1 - s0 > 1e-5]

    def cdist(self, a, b):
        d = (a - b) % self.total
        return min(d, self.total - d)


def _unwrap(x, ref, total):
    return x + round((ref - x) / total) * total


class _Strip:
    def __init__(self, idx, LA, a0, a1, LB, ss, ts):
        self.idx = idx
        self.LA, self.a0, self.a1, self.LB = LA, a0, a1, LB
        self.ss, self.ts = ss, ts                   # pairing samples: A param -> B param (decreasing)
        self.b0, self.b1 = self.proj(a1), self.proj(a0)
        self.cap = {}                               # 'start'/'end' -> (loop, s0, s1) of a cap closing the strip
        self.w = 1

    def proj(self, s):
        ss, ts = self.ss, self.ts
        if s <= ss[0]:
            return ts[0] - (s - ss[0])
        if s >= ss[-1]:
            return ts[-1] - (s - ss[-1])
        j = bisect.bisect_right(ss, s) - 1
        f = (s - ss[j]) / max(1e-12, ss[j + 1] - ss[j])
        return ts[j] + (ts[j + 1] - ts[j]) * f

    def unproj(self, t):
        ss, ts = self.ss, self.ts
        if t >= ts[0]:
            return ss[0] - (t - ts[0])
        if t <= ts[-1]:
            return ss[-1] - (t - ts[-1])
        lo, hi = 0, len(ts) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if ts[mid] >= t:
                lo = mid
            else:
                hi = mid
        f = (ts[lo] - t) / max(1e-12, ts[lo] - ts[hi])
        return ss[lo] + (ss[hi] - ss[lo]) * f


class _Piece:
    __slots__ = ("kind", "L", "s0", "s1", "theta", "n", "mutable", "strip", "end", "keys", "length")

    def __init__(self, kind, L=None, s0=0.0, s1=0.0, theta=0.0, n=1, mutable=False, strip=None, end=None):
        self.kind, self.L, self.s0, self.s1, self.theta = kind, L, s0, s1, theta
        self.n, self.mutable, self.strip, self.end = n, mutable, strip, end
        self.keys = None
        self.length = s1 - s0


class RoadRemeshCore:
    """Algorithm shared by the operator and the live preview; parameters are read from self."""

    def _run(self, bm, obj):
        self._bm = bm
        sel = [f for f in bm.faces if f.select]
        if not sel:
            raise _Fail("Выделите дорогу вместе с бордюрами (A — всё)")
        area = {}
        for f in sel:
            area[f.material_index] = area.get(f.material_index, 0.0) + f.calc_area()
        road_mat = max(area, key=area.get)
        road = {f for f in sel if f.material_index == road_mat}
        curb = {f for f in sel if f.material_index != road_mat}
        self._road, self._curb = road, curb
        self._extra = {}

        up = sum((f.normal * f.calc_area() for f in road), Vector())
        if up.length < 1e-9:
            raise _Fail("Не удалось понять, где верх у дороги")
        self._frame = _Frame(up)
        self._loops = self._find_loops(road, curb)
        self._bvh_walls()
        self._mark_caps()
        self._curb_columns()
        self._pair()
        self._make_strips()
        self._regions()
        self._stations()
        return self._build(bm, obj)

    # ---------- boundary ----------

    def _find_loops(self, road, curb):
        nxt = {}
        for f in road:
            for l in f.loops:
                e = l.edge
                if sum(1 for g in e.link_faces if g in road) != 1:
                    continue
                other = [g for g in e.link_faces if g is not f]
                if not other:
                    t = 'B'
                elif other[0] in curb and len(other) == 1:
                    t = 'R'
                else:
                    t = 'X'
                a = l.vert
                if a in nxt:
                    raise _Fail("Край дороги касается сам себя в одной вершине — разведите вершины")
                nxt[a] = (l.link_loop_next.vert, e, t)
        loops = []
        seen = set()
        for start in nxt:
            if start in seen:
                continue
            verts, edges, types = [], [], []
            v = start
            while v not in seen:
                seen.add(v)
                b, e, t = nxt[v]
                verts.append(v)
                edges.append(e)
                types.append(t)
                v = b
                if v not in nxt:
                    raise _Fail("Край дороги не замкнут")
            if len(verts) >= 3:
                loops.append(_Loop(len(loops), verts, edges, types, self._frame, self.corner_angle))
        if not loops:
            raise _Fail("У выделенной дороги нет краёв")
        # vertices used by unselected faces must stay where they are
        for L in loops:
            for i, v in enumerate(L.verts):
                if any(g not in road and g not in curb for g in v.link_faces):
                    L.anchor.add(i)
        return loops

    def _bvh_walls(self):
        verts, polys, owner = [], [], []
        step = 4
        for L in self._loops:
            for k in range(0, L.N, step):
                a, b = L.dq[k], L.dq[(k + step) % L.N]
                base = len(verts)
                verts += [(a.x, a.y, -1.0), (b.x, b.y, -1.0), (b.x, b.y, 1.0), (a.x, a.y, 1.0)]
                polys.append((base, base + 1, base + 2, base + 3))
                owner.append((L, k, k + step))
        self._walls = BVHTree.FromPolygons(verts, polys)
        self._owner = owner

    def _cast(self, p2, d2, maxd):
        # start a little inside: on a convex side the wall chords lie just inside the curve
        lift = 0.05
        o = Vector((p2.x, p2.y, 0.0)) + Vector((d2.x, d2.y, 0.0)) * lift
        loc, _, idx, dist = self._walls.ray_cast(o, Vector((d2.x, d2.y, 0.0)), maxd)
        if loc is None:
            return None
        dist += lift
        L, k0, k1 = self._owner[idx]
        a, b = L.dq[k0], L.dq[k1 % L.N]
        ab = b - a
        f = (Vector((loc.x, loc.y)) - a).dot(ab) / max(1e-12, ab.length_squared)
        f = min(1.0, max(0.0, f))
        s = L.acc[k0] + (L.acc[k1] - L.acc[k0]) * f
        return L, s % L.total, dist

    def _mark_caps(self):
        """An open edge run across the road end (the road goes far ahead of it) is a cap, not a road side."""
        for L in self._loops:
            n = L.n
            done = set()
            for i in range(n):
                if L.types[i] != 'B' or i in done:
                    continue
                run = [i]
                j = i
                while L.types[(j + 1) % n] == 'B' and (j + 1) % n not in L.sharp and (j + 1) % n != i:
                    j = (j + 1) % n
                    run.append(j)
                k = i
                while L.types[(k - 1) % n] == 'B' and k not in L.sharp and (k - 1) % n not in run:
                    k = (k - 1) % n
                    run.insert(0, k)
                done |= set(run)
                s0 = L.vparam(run[0])
                s1 = L.vparam((run[-1] + 1) % n)
                if s1 <= s0:
                    s1 += L.total
                ln = s1 - s0
                mid = (s0 + s1) / 2
                hit = self._cast(L.at2(mid), L.normal2(mid), 1e6)
                ahead = hit[2] if hit else 1e9
                # or the road sides on both ends of it face each other: it closes one road
                before = s0 - min(1.0, 0.3 * ln)
                side = self._cast(L.at2(before), L.normal2(before), min(3.0 * ln, self.max_width))
                across = side is not None and side[0] is L and (side[1] - s1) % L.total < 0.5 * ln + 2.0
                # or a short open edge with sharp turns on both ends (a flared end into another road)
                sharp = min(abs(L.turn[run[0]]), abs(L.turn[(run[-1] + 1) % n])) >= math.radians(60.0)
                if ln <= self.max_width and (ahead > 1.5 * ln or across or sharp):
                    for e in run:
                        L.types[e] = 'C'
            L.build()

    # ---------- curb ----------

    def _curb_columns(self):
        """For every curb edge of a loop: the column of curb quads across the curb, row 0 at the road."""
        road, curb = self._road, self._curb
        for L in self._loops:
            cols = [None] * L.n
            for i in range(L.n):
                if L.types[i] != 'R':
                    continue
                a, b = L.verts[i], L.verts[(i + 1) % L.n]
                f = next(g for g in L.edges[i].link_faces if g in curb)
                rows = []
                ok = True
                while f is not None:
                    if len(f.verts) != 4 or len(rows) > 8:
                        ok = False
                        break
                    la = next(l for l in f.loops if l.vert is a)
                    forward = la.link_loop_next.vert is b
                    if not forward and la.link_loop_prev.vert is not b:
                        ok = False
                        break
                    na = (la.link_loop_prev if forward else la.link_loop_next).vert
                    lb = next(l for l in f.loops if l.vert is b)
                    nb = (lb.link_loop_next if forward else lb.link_loop_prev).vert
                    if any(g not in road and g not in curb for v in (na, nb) for g in v.link_faces):
                        ok = False
                        break
                    rows.append((f, na, nb, forward))
                    ne = next((x for x in na.link_edges if x.other_vert(na) is nb), None)
                    nxt = [g for g in ne.link_faces if g is not f] if ne else []
                    a, b = na, nb
                    f = nxt[0] if len(nxt) == 1 and nxt[0] in curb else None
                cols[i] = rows if ok and rows else None
            # neighbouring columns must share their rung and have the same rows
            n = L.n

            def joined(i):
                c0, c1 = cols[i], cols[(i + 1) % n]
                return (c0 is not None and c1 is not None and len(c0) == len(c1)
                        and all(r0[2] is r1[1] for r0, r1 in zip(c0, c1)))

            runs = []
            if all(joined(i) for i in range(n)):
                runs.append((0, n, True))
            else:
                for k in range(n):
                    if cols[k] is not None and not joined((k - 1) % n):
                        count = 1
                        while count < n and joined((k + count - 1) % n):
                            count += 1
                        runs.append((k, count, False))
            L.curb_cols = cols
            L.curb_runs = runs
            in_run = set()
            for first, count, cyclic in runs:
                for c in range(count):
                    in_run.add((first + c) % n)
                if not cyclic:
                    L.anchor.add(first)
                    L.anchor.add((first + count) % n)
            # verts of curb edges that are not rebuilt keep their place
            for i in range(n):
                if L.types[i] == 'R' and i not in in_run:
                    L.anchor.add(i)
                    L.anchor.add((i + 1) % n)

    # ---------- pairing of opposite road sides ----------

    def _pair(self):
        maxw = self.max_width
        for L in self._loops:
            starts = sorted(L.vparam(i) for i in L.corner)
            s_start = starts[0] if starts else 0.0
            m = max(8, int(math.ceil(L.total / _SAMPLE)))
            step = L.total / m
            L.step = step
            samples = []
            for j in range(m):
                s = s_start + j * step
                samples.append((s, self._pair_one(L, s, maxw)))
            L.samples = samples

    def _pair_one(self, L, s, maxw):
        if L.edge_type(s) in ('C', 'X'):
            return None
        pa, na = L.at2(s), L.normal2(s)
        hit = self._cast(pa, na, maxw)
        if hit is None:
            return None
        L2, t, dist = hit
        if L2.edge_type(t) in ('C', 'X'):
            return None
        pb, nb = L2.at2(t), L2.normal2(t)
        if na.dot(nb) > -_OPPOSITE:
            return None
        back = self._cast(pb, nb, maxw)
        if back is None or back[0] is not L or L.cdist(back[1], s) > 0.25 * dist + 1.0:
            return None
        ka, kb = L.curvature(s), L2.curvature(t)
        if not (self._concentric(pa, na, ka, pb, nb, kb, dist) and self._concentric(pb, nb, kb, pa, na, ka, dist)):
            return None
        return L2, t, dist

    @staticmethod
    def _concentric(pa, na, ka, pb, nb, kb, dist):
        """Both sides of a road bend around one centre; a fillet across from a straight side does not."""
        if abs(ka) < 1e-6:
            expect = 0.0
        else:
            c = pa + na / ka
            r = (pb - c).length
            if r < 1e-6:
                return False
            expect = 1.0 / r if (c - pb).dot(nb) > 0 else -1.0 / r
        return abs(kb - expect) <= 0.35 * abs(expect) + 0.1 / max(dist, 1.0)

    def _make_strips(self):
        runs = []
        for L in self._loops:
            cur = None
            loop_runs = []
            for s, hit in L.samples:
                if hit is None:
                    cur = None
                    continue
                L2, t, dist = hit
                if cur is not None and cur["L2"] is L2:
                    tu = _unwrap(t, cur["t"][-1], L2.total)
                    if tu <= cur["t"][-1] + 0.3 and cur["t"][-1] - tu < 4.0 * L.step + 1.0:
                        cur["s"].append(s)
                        cur["t"].append(tu)
                        cur["d"].append(dist)
                        continue
                cur = {"L": L, "L2": L2, "s": [s], "t": [t], "d": [dist]}
                loop_runs.append(cur)
            # a run crossing the start of the loop
            if len(loop_runs) >= 2 and L.samples[0][1] is not None and L.samples[-1][1] is not None:
                first, last = loop_runs[0], loop_runs[-1]
                if first["L2"] is last["L2"] and first["s"][0] == L.samples[0][0]:
                    tu = _unwrap(first["t"][0], last["t"][-1], last["L2"].total)
                    if tu <= last["t"][-1] + 0.3 and last["t"][-1] - tu < 4.0 * L.step + 1.0:
                        shift = tu - first["t"][0]
                        last["s"] += [x + L.total for x in first["s"]]
                        last["t"] += [x + shift for x in first["t"]]
                        last["d"] += first["d"]
                        loop_runs.pop(0)
            runs += [r for r in loop_runs if len(r["s"]) >= 3 and r["s"][-1] - r["s"][0] >= 1.5]

        runs.sort(key=lambda r: r["s"][0] - r["s"][-1])
        used = set()
        strips = []
        for r in runs:
            if id(r) in used:
                continue
            L, L2 = r["L"], r["L2"]
            a0, a1 = r["s"][0], r["s"][-1]
            tmin, tmax = min(r["t"]), max(r["t"])
            best = None
            for q in runs:
                if id(q) in used or q is r or q["L"] is not L2 or q["L2"] is not L:
                    continue
                q0 = _unwrap(q["s"][0], tmin, L2.total)
                q1 = q0 + (q["s"][-1] - q["s"][0])
                ov = min(q1, tmax) - max(q0, tmin)
                if ov > 0 and (best is None or ov > best[0]):
                    best = (ov, q)
            if best is not None:
                q = best[1]
                back = [_unwrap(x, (a0 + a1) / 2, L.total) for x in q["t"]]
                b0, b1 = max(a0, min(back)), min(a1, max(back))
                if b1 - b0 >= 1.0:
                    a0, a1 = b0, b1
                    used.add(id(q))
            used.add(id(r))
            if a1 - a0 < 1.0:
                continue
            width = sorted(r["d"])[len(r["d"]) // 2]
            ss, ts = list(r["s"]), list(r["t"])
            for k in range(1, len(ts)):
                ts[k] = min(ts[k], ts[k - 1])
            S = _Strip(len(strips), L, a0, a1, L2, ss, ts)
            S.width = width
            S.w = self.width_div
            self._snap(S)
            if S.a1 - S.a0 >= 0.5 and S.b1 - S.b0 >= 0.5:
                strips.append(S)
        self._strips = strips
        self._join_strips()
        self._fix_overlaps()
        if not strips:
            raise _Fail("Не нашлось ни одной полосы дороги: проверьте выделение и «Макс. ширина»")

    @staticmethod
    def _flipped(S):
        """The same strip seen from its B side."""
        T = _Strip.__new__(_Strip)
        T.__dict__.update(S.__dict__)
        T.LA, T.LB = S.LB, S.LA
        T.a0, T.a1, T.b0, T.b1 = S.b0, S.b1, S.a0, S.a1
        T.ss, T.ts = list(reversed(S.ts)), list(reversed(S.ss))
        return T

    def _join_strips(self):
        """One road cut into strips that touch end to end (a corner on one side stops the rays) becomes one strip."""
        strips = self._strips
        changed = True
        while changed:
            changed = False
            for S in strips:
                for T0 in strips:
                    if T0 is S:
                        continue
                    for T in (T0, self._flipped(T0)):
                        M = self._joined(S, T) or self._joined(T, S)
                        if M is not None:
                            strips[strips.index(S)] = M
                            strips.remove(T0)
                            changed = True
                            break
                    if changed:
                        break
                if changed:
                    break
        for i, S in enumerate(strips):
            S.idx = i

    def _joined(self, S, T):
        """S followed by T along both sides, or None."""
        if T.LA is not S.LA or T.LB is not S.LB:
            return None
        tol = max(1.0, 0.5 * max(S.width, T.width))
        ga = _unwrap(T.a0, S.a1, S.LA.total) - S.a1
        gb = S.b0 - _unwrap(T.b1, S.b0, S.LB.total)
        if not (-tol <= ga <= tol and -tol <= gb <= tol):
            return None
        shift_a = _unwrap(T.a0, S.a1, S.LA.total) - T.a0
        shift_b = _unwrap(T.b1, S.b0, S.LB.total) - T.b1
        M = _Strip.__new__(_Strip)
        M.__dict__.update(S.__dict__)
        M.a1 = T.a1 + shift_a
        M.b0 = T.b0 + shift_b
        keep = [k for k, x in enumerate(T.ss) if x + shift_a > S.ss[-1]]
        ss = S.ss + [T.ss[k] + shift_a for k in keep]
        ts = S.ts + [T.ts[k] + shift_b for k in keep]
        for k in range(1, len(ts)):
            ts[k] = min(ts[k], ts[k - 1])
        M.ss, M.ts = ss, ts
        M.width = max(S.width, T.width)
        return M

    def _snap(self, S):
        tol = max(0.6, 0.12 * S.width)

        def near(L, s):
            best = None
            for b in L.breaks():
                bu = _unwrap(b, s, L.total)
                d = abs(bu - s)
                if d <= tol and (best is None or d < best[0]):
                    best = (d, bu)
            return best[1] if best else None

        x = near(S.LA, S.a0)
        if x is not None:
            S.a0 = x
        x = near(S.LA, S.a1)
        if x is not None:
            S.a1 = x
        S.b0, S.b1 = S.proj(S.a1), S.proj(S.a0)
        x = near(S.LB, S.b0)
        if x is not None:
            S.b0 = x
        x = near(S.LB, S.b1)
        if x is not None:
            S.b1 = x

    def _sides(self):
        per = {L.idx: [] for L in self._loops}
        for S in self._strips:
            per[S.LA.idx].append([S.a0 % S.LA.total, S.a1 - S.a0, S, 'A'])
            per[S.LB.idx].append([S.b0 % S.LB.total, S.b1 - S.b0, S, 'B'])
        for v in per.values():
            v.sort(key=lambda x: x[0])
        return per

    def _fix_overlaps(self):
        for _ in range(3):
            changed = False
            per = self._sides()
            for L in self._loops:
                sides = per[L.idx]
                for k in range(len(sides)):
                    if len(sides) < 2:
                        break
                    s0, ln, S, side = sides[k]
                    t0, _, T, tside = sides[(k + 1) % len(sides)]
                    t0u = t0 if t0 >= s0 else t0 + L.total
                    over = s0 + ln - t0u
                    if over <= 1e-6:
                        continue
                    cut = t0u + over / 2.0
                    self._set_end(S, side, 'end', cut)
                    self._set_end(T, tside, 'start', cut)
                    changed = True
                if changed:
                    break
            if not changed:
                break
        self._strips = [S for S in self._strips if S.a1 - S.a0 > 0.3 and S.b1 - S.b0 > 0.3]

    @staticmethod
    def _set_end(S, side, which, s):
        if side == 'A':
            if which == 'end':
                S.a1 = _unwrap(s, S.a1, S.LA.total)
                S.b0 = S.proj(S.a1)
            else:
                S.a0 = _unwrap(s, S.a0, S.LA.total)
                S.b1 = S.proj(S.a0)
        else:
            if which == 'end':
                S.b1 = _unwrap(s, S.b1, S.LB.total)
                S.a0 = S.unproj(S.b1)
            else:
                S.b0 = _unwrap(s, S.b0, S.LB.total)
                S.a1 = S.unproj(S.b0)

    # ---------- regions between strips ----------

    def _regions(self):
        per = self._sides()
        free = {}
        for L in self._loops:
            sides = per[L.idx]
            if not sides:
                raise _Fail("Край дороги без полос: увеличьте «Макс. ширина» или выделите дорогу целиком")
            for k, (s0, ln, S, side) in enumerate(sides):
                n0, _, T, tside = sides[(k + 1) % len(sides)]
                start = s0 + ln
                end = _unwrap(n0, start, L.total)
                if end < start - 1e-6:
                    end += L.total
                if len(sides) == 1:
                    end = s0 + L.total
                free[(S.idx, side)] = (L, start, max(start, end), T, tside)
        regions = []
        done = set()
        for key in free:
            if key in done:
                continue
            elems = []
            cur = key
            for _ in range(len(free) + 1):
                if cur in done:
                    break
                done.add(cur)
                L, s0, s1, T, tside = free[cur]
                elems.append(("rail", L, s0, s1))
                # free interval ends at the start of T's side; cross that strip's mouth
                if tside == 'A':
                    elems.append(("mouth", T, 'start', 'A'))
                    cur = (T.idx, 'B')
                else:
                    elems.append(("mouth", T, 'end', 'B'))
                    cur = (T.idx, 'A')
            regions.append(elems)

        self._junctions = []
        for elems in regions:
            if len(elems) == 2:
                _, L, s0, s1 = elems[0]
                _, T, end, _ = elems[1]
                rail = sum(b - a for a, b in L.pieces(s0, s1) if L.edge_type((a + b) / 2) not in ('C', 'X'))
                if rail < 0.5:
                    # mouth points run from A to B: the start cap is walked backwards, the end cap forwards
                    T.cap[end] = (L, s1, s0 - s1) if end == 'start' else (L, s0, s1 - s0)
                    fixed = [k for k in range(L.n) if L.types[k] == 'X'
                             and (L.vparam(k) - s0) % L.total < (s1 - s0) - 1e-6]
                    if fixed:
                        T.w = len(fixed)
                    continue
            self._junctions.append(elems)

    # ---------- counts and stations ----------

    def _count(self, L, s0, s1):
        """Edges for a piece of a road side: by angle on a fillet, by length on a straight."""
        ln = s1 - s0
        t = L.edge_type((s0 + s1) / 2)
        if t == 'X':
            return sum(1 for k in range(L.n) if (L.vparam(k) - s0) % L.total < ln - 1e-6) or 1, False, 'fixed', 0.0
        if t == 'C':
            return self.width_div, False, 'cap', 0.0
        by_len = ln / self.edge_length
        theta = self._turning(L, s0, s1)[1][-1] if ln > 1e-4 else 0.0
        if abs(theta) < math.radians(10.0):
            return max(1, round(by_len)), True, 'line', theta
        by_angle = abs(theta) / (math.pi / 2) * self.seg_90
        return max(1, round(max(by_angle, by_len))), False, 'arc', theta

    def _alloc(self, L, a, b, anchors_only=False):
        """Pieces of a side between kept points and fillet ends, each with its own edge count."""
        if b - a < 1e-4:
            return []
        return [[x, y, self._count(L, x, y)[0]] for x, y in L.pieces(a, b, anchors_only)]

    def _place(self, L, a, b, n):
        """n edges along a side: fillets keep their share by angle, kept points stay stations."""
        parts = self._alloc(L, a, b)
        if not parts:
            return [a] * (n + 1)
        if n < len(parts):
            # too few edges for every fillet end: only kept points must stay
            parts = self._alloc(L, a, b, anchors_only=True)
            n = max(n, len(parts))
        while sum(p[2] for p in parts) > n:
            p = min((q for q in parts if q[2] > 1), key=lambda q: (q[1] - q[0]) / q[2])
            p[2] -= 1
        while sum(p[2] for p in parts) < n:
            p = max(parts, key=lambda q: (q[1] - q[0]) / (q[2] + 1))
            p[2] += 1
        out = []
        for x, y, c in parts:
            out += [x + (y - x) * i / c for i in range(c)]
        out.append(b)
        return out

    def _side_pieces(self, L, s0, s1):
        out = []
        for a, b in L.pieces(s0, s1):
            n, mut, kind, theta = self._count(L, a, b)
            out.append(_Piece(kind, L, a, b, theta, n, mut))
        return out

    def _stations(self):
        self._st = {L.idx: {} for L in self._loops}    # loop -> {param key: param}
        self._strip_st = {}
        for S in self._strips:
            self._strip_stations(S)
        self._junction_data = []
        for elems in self._junctions:
            self._junction_data.append(self._junction(elems))

    def _add_st(self, L, s):
        s %= L.total
        key = round(s, 4)
        if abs(key - round(L.total, 4)) < 1e-4:
            key = 0.0
        self._st[L.idx].setdefault(key, s)
        return (L.idx, key)

    def _strip_stations(self, S):
        pa = self._side_pieces(S.LA, S.a0, S.a1)
        pb = self._side_pieces(S.LB, S.b0, S.b1)
        na, nb = sum(p.n for p in pa), sum(p.n for p in pb)
        master_a = na >= nb
        pieces = pa if master_a else pb
        N = max(na, nb)
        params = []
        for p in pieces:
            for i in range(p.n):
                params.append(p.s0 + (p.s1 - p.s0) * i / p.n)
        params.append(pieces[-1].s1)
        if master_a:
            A = params
            B = [S.proj(s) for s in reversed(A)]
            B[0], B[-1] = S.b0, S.b1
        else:
            B = params
            A = [S.unproj(t) for t in reversed(B)]
            A[0], A[-1] = S.a0, S.a1
        self._snap_anchors(S.LB if master_a else S.LA, B if master_a else A)
        # monotonic
        for arr in (A, B):
            for k in range(1, len(arr)):
                if arr[k] <= arr[k - 1]:
                    arr[k] = arr[k - 1] + 1e-4
        keys_a = [self._add_st(S.LA, s) for s in A]
        keys_b = [self._add_st(S.LB, s) for s in B]
        # rung i: A[i] <-> B[N - i]
        self._strip_st[S.idx] = (keys_a, list(reversed(keys_b)), N)

    @staticmethod
    def _snap_anchors(L, arr):
        if len(arr) < 3:
            return
        for i in L.anchor:
            s = _unwrap(L.vparam(i), (arr[0] + arr[-1]) / 2, L.total)
            if not arr[0] + 1e-4 < s < arr[-1] - 1e-4:
                continue
            k = min(range(1, len(arr) - 1), key=lambda j: abs(arr[j] - s))
            if arr[k - 1] < s < arr[k + 1]:
                arr[k] = s

    def _mouth_keys(self, S, end):
        """Points across the strip end, from the A side to the B side."""
        keys_a, keys_b, N = self._strip_st[S.idx]
        i = 0 if end == 'start' else N
        ka, kb = keys_a[i], keys_b[i]
        return [ka] + [("M", S.idx, end, j) for j in range(1, S.w)] + [kb]

    def _junction(self, elems):
        pieces = []
        for el in elems:
            if el[0] == "rail":
                _, L, s0, s1 = el
                pieces += self._side_pieces(L, s0, s1)
            else:
                _, S, end, side = el
                keys = self._mouth_keys(S, end)
                # leaving the free interval at the start of `side`; walk the mouth away from it
                if side == 'B':
                    keys = list(reversed(keys))
                p = _Piece("mouth", n=S.w, strip=S, end=end)
                p.keys = keys
                pieces.append(p)
        pieces = [p for p in pieces if p.kind == "mouth" or p.s1 - p.s0 > 1e-4]
        pieces = self._merge_caps(self._merge_short(pieces))
        if self.junction_mode == 'GRID':
            saved = {i: dict(st) for i, st in self._st.items()}
            plus = self._plus(pieces)
            if plus is not None:
                return ("plus", pieces, plus)
            self._st = saved
        corners = self._pick_corners(pieces) if self.junction_mode == 'GRID' else None
        if corners is not None:
            plan = self._balance(pieces, corners)
            if plan is not None:
                for p in pieces:
                    self._piece_keys(p)
                return ("grid", pieces, plan)
        for p in pieces:
            self._piece_keys(p)
        return ("ngon", pieces, None)

    @staticmethod
    def _turning(L, a, b):
        """Params along the side and how much it has turned since a."""
        n = max(8, int((b - a) / 0.05))
        prev = L.tangent2(a + 1e-4)
        params, cum = [a], [0.0]
        for i in range(1, n + 1):
            x = a + (b - a) * i / n
            t = L.tangent2(min(x, b - 1e-4))
            cum.append(cum[-1] + _signed_angle(prev, t))
            params.append(x)
            prev = t
        return params, cum

    def _half_turn(self, L, a, b):
        """Where a fillet has turned half of its angle: the split is even on any fillet."""
        return self._turn_at(L, a, b, 0.5)

    def _turn_at(self, L, a, b, frac):
        """Where a fillet has turned the given share of its angle."""
        if b - a < 1e-4:
            return a
        params, cum = self._turning(L, a, b)
        half = cum[-1] * frac
        if abs(cum[-1]) < math.radians(4.0):
            return a + (b - a) * frac
        for i in range(1, len(cum)):
            lo, hi = cum[i - 1], cum[i]
            if (lo - half) * (hi - half) <= 0 and hi != lo:
                f = (half - lo) / (hi - lo)
                return params[i - 1] + (params[i] - params[i - 1]) * f
        return a + (b - a) * frac

    def _merge_short(self, pieces):
        """Tiny straight bits between a strip end and a fillet join the neighbour (not across a kept corner)."""
        limit = min(1.0, 0.3 * self.edge_length)
        out = list(pieces)
        changed = True
        while changed and len(out) > 1:
            changed = False
            for i, p in enumerate(out):
                if p.kind != "line" or p.length >= limit:
                    continue
                anchors = {round(p.L.vparam(k) % p.L.total, 4) for k in p.L.anchor}
                cand = []
                for j in ((i - 1) % len(out), (i + 1) % len(out)):
                    q = out[j]
                    if j == i or q.kind not in ("line", "arc") or q.L is not p.L:
                        continue
                    if abs(q.s1 - p.s0) < 1e-4 and round(p.s0 % p.L.total, 4) not in anchors:
                        cand.append((j, 'after'))
                    elif abs(p.s1 - q.s0) < 1e-4 and round(p.s1 % p.L.total, 4) not in anchors:
                        cand.append((j, 'before'))
                if not cand:
                    continue
                j, where = next((c for c in cand if out[c[0]].kind == "arc"), cand[0])
                q = out[j]
                if where == 'after':
                    q.s1 = p.s1
                else:
                    q.s0 = p.s0
                q.length = q.s1 - q.s0
                if q.kind == "line":
                    q.n = max(1, round(q.length / self.edge_length))
                del out[i]
                changed = True
                break
        return out

    def _merge_caps(self, pieces):
        out = []
        for p in pieces:
            if out and p.kind == "cap" and out[-1].kind == "cap" and out[-1].L is p.L and abs(out[-1].s1 - p.s0) < 1e-4:
                out[-1].s1 = p.s1
                out[-1].length = out[-1].s1 - out[-1].s0
                continue
            out.append(p)
        return out

    def _plus(self, pieces):
        """Junction as the strips running on through the fillets plus a centre patch:
        each fillet is split in two, its halves join across the mouths (as on a hand-made junction)."""
        m = len(pieces)
        for p in pieces:
            if p.kind == "cap":
                p.n = self.width_div
                self._piece_keys(p)
        arms = [i for i, p in enumerate(pieces) if p.kind in ("mouth", "cap")]
        k = len(arms)
        if k < 2:
            return None
        groups = []
        for a in range(k):
            i, j = arms[a], arms[(a + 1) % k]
            grp = []
            x = (i + 1) % m
            while x != j:
                grp.append(x)
                x = (x + 1) % m
            if any(pieces[x].kind not in ("line", "arc", "fixed") for x in grp):
                return None
            if grp and len({pieces[x].L for x in grp}) != 1:
                return None
            groups.append(grp)
        walls = []
        for a, grp in enumerate(groups):
            arm, nxt = pieces[arms[a]], pieces[arms[(a + 1) % k]]
            if grp:
                first, last = pieces[grp[0]], pieces[grp[-1]]
                tot = (_signed_angle(self._tangent(arm, True), self._tangent(first, False))
                       + self._turning(first.L, first.s0, last.s1)[1][-1]
                       + _signed_angle(self._tangent(last, True), self._tangent(nxt, False)))
            else:
                tot = _signed_angle(self._tangent(arm, True), self._tangent(nxt, False))
            # a fillet corner turns by about 90 degrees, a straight wall across a mouth by 180
            walls.append(max(0, int(round(tot / (math.pi / 2))) - 1))
        if k + sum(walls) != 4:
            return None

        arm_keys = [pieces[i].keys for i in arms]
        arm_w = [len(ks) - 1 for ks in arm_keys]

        def span(a):
            grp = groups[a]
            if grp:
                return pieces[grp[0]].L, pieces[grp[0]].s0, pieces[grp[-1]].s1
            # two arms touch: the corner is a single point
            key = arm_keys[a][-1]
            L = self._loops[key[0]]
            s = self._st[key[0]][key[1]]
            return L, s, s

        def inward(a):
            p0, p1 = self._key_pos2(arm_keys[a][0]), self._key_pos2(arm_keys[a][-1])
            d = p1 - p0
            return Vector((-d.y, d.x)).normalized() if d.length > 1e-9 else Vector((0.0, 0.0))

        # a layout that does not fit must not leave its stations on the curbs
        saved = {i: dict(st) for i, st in self._st.items()}
        lanes = self._lanes(pieces, groups, walls, arm_keys, arm_w, span, inward)
        if lanes is not None:
            return lanes
        self._st = saved

        # fillet corners: one split in the middle of the turn
        splits = [None] * k
        for a, grp in enumerate(groups):
            if walls[a]:
                continue
            L, s0, s1 = span(a)
            splits[a] = [self._half_turn(L, s0, s1)]
        for a in range(k):
            if walls[a]:
                splits[a] = self._wall_splits(pieces, groups, walls, a, span, inward, arm_keys,
                                              lambda b: span(b)[0].at2(splits[b][0]))
                if splits[a] is None:
                    return None

        def part(L, a, b):
            return sum(p[2] for p in self._alloc(L, a, b))

        # strips running through: rails before and after each arm, same edge count on both
        ext = []
        for a in range(k):
            prev = (a - 1) % k
            Lp, _, e_prev = span(prev)
            Lc, s_cur, _ = span(a)
            r1 = (Lp, splits[prev][-1], e_prev)
            r2 = (Lc, s_cur, splits[a][0])
            n = max(part(*r1), part(*r2))
            if n and (r1[2] - r1[1] < 1e-4 or r2[2] - r2[1] < 1e-4):
                return None
            ext.append((r1, r2, n))

        centre = self._plus_centre(splits, span, arm_w)
        if centre is None:
            return None
        ext_keys = []
        for a, (r1, r2, n) in enumerate(ext):
            if n == 0:
                ext_keys.append(None)
                continue
            L1, x0, x1 = r1
            L2, y0, y1 = r2
            k1 = [self._add_st(L1, x) for x in self._place(L1, x0, x1, n)]
            k2 = [self._add_st(L2, y) for y in self._place(L2, y0, y1, n)]
            if len(k1) != len(k2):
                return None
            # side chains from the mouth inwards
            ext_keys.append((list(reversed(k1)), k2, len(k1) - 1))
        return {"arms": arm_keys, "w": arm_w, "ext": ext_keys, "fans": [], "centre": self._centre_keys(centre)}

    def _lanes(self, pieces, groups, walls, arm_keys, arm_w, span, inward):
        """Junction with the lanes running straight through: the arms run on to a centre grid,
        and each fillet is a fan of quads coming in to a hub at the corner of that grid."""
        k = len(groups)
        jid = len(self._junction_data)
        frame = self._frame
        long_enough = 0.3 * self.edge_length

        def part(L, a, b):
            return sum(p[2] for p in self._alloc(L, a, b))

        # a fillet may start and end with straight curb: that curb runs on with the arm, the bend is the fan
        bend = {}
        for g in range(k):
            if walls[g]:
                continue
            L, s0, s1 = span(g)
            if s1 - s0 < 1e-4:
                return None
            arcs = [pieces[x] for x in groups[g] if pieces[x].kind == "arc"]
            bend[g] = (arcs[0].s0, arcs[-1].s1) if arcs else (s0, s1)

        def straight(g, at_end):
            L, s0, s1 = span(g)
            t0, t1 = bend[g]
            return (L, t1, s1) if at_end else (L, s0, t0)

        # which straight curbs run on: on both sides of an arm, or next to a wall
        use = {}
        for a in range(k):
            sides = (((a - 1) % k, True), (a, False))
            fil = [(g, e) for g, e in sides if not walls[g]]
            ok = all(straight(g, e)[2] - straight(g, e)[1] >= long_enough for g, e in fil)
            for g, e in fil:
                use[(g, e)] = ok

        fan = {}
        corner = {}
        for g in bend:
            L, s0, s1 = span(g)
            f0 = bend[g][0] if use[(g, False)] else s0
            f1 = bend[g][1] if use[(g, True)] else s1
            if f1 - f0 < 1e-4:
                return None
            fan[g] = (f0, f1)
            # the curbs before and after the bend, turned a little into the road, meet at the hub;
            # straight on they would meet at a cusp and the first quads of the fan would be slivers
            h = min(0.3, (f1 - f0) / 2.0)
            a0, a1 = L.at(f0), L.at(f1)
            p0, p1 = frame.to2(a0), frame.to2(a1)
            e0 = p0 - frame.to2(L.at(f0 - h))
            e1 = frame.to2(L.at(f1 + h)) - p1
            turn = abs(self._turning(L, f0, f1)[1][-1])
            if not math.radians(20.0) < turn < math.radians(160.0):
                return None
            tilt = turn / 6.0
            e0 = Matrix.Rotation(tilt, 2) @ e0
            e1 = Matrix.Rotation(-tilt, 2) @ -e1
            den = _cross2(e0, e1)
            if abs(den) < 1e-9 * e0.length * e1.length:
                return None
            u, v = _cross2(p1 - p0, e1) / den, _cross2(p1 - p0, e0) / den
            if u <= 0 or v <= 0:
                return None
            q = p0 + e0 * u
            m = self._half_turn(L, f0, f1)
            if (q - L.at2(m)).dot(L.normal2(m)) <= 0.05:
                return None
            up = frame.up
            corner[g] = frame.e1 * q.x + frame.e2 * q.y + up * ((a0.dot(up) + a1.dot(up)) / 2.0)

        splits = [[] for _ in range(k)]
        for g in range(k):
            if walls[g]:
                splits[g] = self._wall_splits(pieces, groups, walls, g, span, inward, arm_keys,
                                              lambda b: frame.to2(corner[b]))
                if splits[g] is None:
                    return None

        def rail(g, at_end):
            """Curb an arm runs on along: up to the split on a wall, the straight before the bend at a fillet."""
            L, s0, s1 = span(g)
            if walls[g]:
                return (L, splits[g][-1], s1) if at_end else (L, s0, splits[g][0])
            if not use[(g, at_end)]:
                return None
            return straight(g, at_end)

        # rows each arm runs on by: as many as its curbs need, plus the one into the hub at a fillet
        depth = []
        for a in range(k):
            sides = (((a - 1) % k, True), (a, False))
            rails = [rail(g, e) for g, e in sides]
            hubs = sum(1 for g, _ in sides if not walls[g])
            if hubs and any(r is None for r in rails):
                n = 1
            else:
                n = max(part(*r) for r in rails) + (1 if hubs else 0)
            for (g, e), r in zip(sides, rails):
                if walls[g] and n and r[2] - r[1] < 1e-4:
                    return None
            depth.append(n)

        def chain(g, at_end, n):
            """Side of an arm's rows from the mouth inwards: along the curb, then into the hub at a fillet.
            Params only, so a count that does not fit leaves no stations behind."""
            r = rail(g, at_end)
            along = n if walls[g] else n - 1
            if along and r is None:
                return None
            if along:
                L, x0, x1 = r
                xs = self._place(L, x0, x1, along)
                if at_end:
                    xs.reverse()
            else:
                L, s0, s1 = span(g)
                xs = [s1 if at_end else s0]
            return L, xs, not walls[g]

        def keys(g, spec):
            L, xs, hub = spec
            ks = [self._add_st(L, x) for x in xs]
            if hub:
                key = ("Q", jid, g, "P")
                self._extra[key] = corner[g]
                ks.append(key)
            return ks

        # the fans first: their stations are tried before any is kept
        fans = []
        for g in sorted(fan):
            L, f0, f1 = span(g)[0], fan[g][0], fan[g][1]
            n = part(L, f0, f1)
            n += n % 2
            xs = self._place(L, f0, f1, n)
            if (len(xs) - 1) % 2:
                xs = self._place(L, f0, f1, len(xs))
            if (len(xs) - 1) % 2:
                return None
            fans.append((g, L, xs))

        ext = []
        for a in range(k):
            n = depth[a]
            if n == 0:
                ext.append(None)
                continue
            g0, g1 = (a - 1) % k, a
            # kept points on a curb may ask for more rows: the other side takes as many
            for _ in range(2):
                left, right = chain(g0, True, n), chain(g1, False, n)
                if left is None or right is None:
                    return None
                size = max(len(sp[1]) + sp[2] for sp in (left, right)) - 1
                if size == n:
                    break
                n = size
            left, right = keys(g0, left), keys(g1, right)
            if len(left) != n + 1 or len(right) != n + 1:
                return None
            for r in range(n):
                if not self._convex([left[r], right[r], right[r + 1], left[r + 1]], strict=False):
                    return None
            ext.append((left, right, n))

        fan_keys = []
        for g, L, xs in fans:
            hub = ("Q", jid, g, "P")
            ks = [self._add_st(L, x) for x in xs]
            for i in range(0, len(ks) - 2, 2):
                if not self._convex([hub, ks[i], ks[i + 1], ks[i + 2]], strict=False):
                    return None
            fan_keys.append((hub, ks))

        centre = self._plus_centre(splits, span, arm_w)
        if centre is None:
            return None
        ring = []
        for sd in centre:
            if sd[0] == "rung":
                e = ext[sd[1]]
                ring.append(e[0][-1] if e else arm_keys[sd[1]][0])
            else:
                L, x, _ = sd[1]
                ring.append(self._add_st(L, x))
        if not self._convex(ring):
            return None
        return {"arms": arm_keys, "w": arm_w, "ext": ext, "fans": fan_keys, "centre": self._centre_keys(centre)}

    def _convex(self, keys, strict=True):
        """The patch corners run counter-clockwise and none of them bends back."""
        pts = [self._key_pos2(key) for key in keys]
        area = 0.0
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            area += _cross2(a, b)
            if strict and _cross2(b - a, c - b) <= 0:
                return False
        return area > 0

    def _wall_splits(self, pieces, groups, walls, a, span, inward, arm_keys, near):
        """Splits on a straight wall across from a mouth: the strips run on to where the corners next to it are."""
        k = len(groups)
        grp = groups[a]
        L, s0, s1 = span(a)
        d1 = d2 = 0.0
        prev = (a - 1) % k
        if not walls[prev]:
            d1 = max(0.0, (near(prev) - self._key_pos2(arm_keys[a][-1])).dot(inward(a)))
        nxt = (a + 1) % k
        if not walls[nxt]:
            d2 = max(0.0, (near(nxt) - self._key_pos2(arm_keys[nxt][0])).dot(inward(nxt)))
        ln = s1 - s0
        if d1 + d2 > 0.9 * ln:
            f = 0.9 * ln / (d1 + d2)
            d1, d2 = d1 * f, d2 * f
        pts = [s0 + d1]
        if walls[a] == 2:
            inner = [pieces[x].s0 for x in grp[1:]]
            if inner:
                turns = [(_signed_angle(self._tangent(pieces[grp[n - 1]], True), self._tangent(pieces[grp[n]], False)),
                          pieces[grp[n]].s0) for n in range(1, len(grp))]
                sharp = max(turns)[1]
            else:
                sharp = (s0 + s1) / 2.0
            if not s0 + d1 < sharp < s1 - d2:
                return None
            pts.append(sharp)
        elif walls[a] > 2:
            return None
        pts.append(s1 - d2)
        # a kept vertex inside a centre wall would need more edges than the mouth across;
        # move the split onto it, so the strip running through takes it instead
        kept = sorted({_unwrap(L.vparam(i), s0, L.total) for i in L.anchor})
        if len(pts) == 2:
            mid = (pts[0] + pts[1]) / 2.0
            inside = [q for q in kept if pts[0] + 1e-4 < q < pts[1] - 1e-4]
            before = [q for q in inside if q <= mid]
            after = [q for q in inside if q > mid]
            if before:
                pts[0] = before[-1]
            if after:
                pts[1] = after[0]
        else:
            first = [q for q in kept if pts[0] + 1e-4 < q < pts[1] - 1e-4]
            second = [q for q in kept if pts[1] + 1e-4 < q < pts[2] - 1e-4]
            if first:
                pts[0] = first[-1]
            if second:
                pts[2] = second[0]
        return pts

    def _plus_centre(self, splits, span, arm_w):
        """Centre sides: rungs across the arms and wall pieces between the splits, with their edge counts."""
        k = len(splits)
        sides = []
        for a in range(k):
            sides.append(("rung", a, arm_w[a]))
            L, _, _ = span(a)
            for x, y in zip(splits[a], splits[a][1:]):
                sides.append(("wall", (L, x, y), None))
        if len(sides) != 4:
            return None

        def part(L, a, b):
            return sum(p[2] for p in self._alloc(L, a, b))

        counts = [sd[2] for sd in sides]
        # a wall across a mouth takes the mouth's count (at least one edge per piece between kept points);
        # two walls facing each other share the larger count by angle and length
        need = [len(self._alloc(*sd[1], anchors_only=True)) if sd[0] == "wall" else sd[2] for sd in sides]
        for c in range(4):
            if counts[c] is None:
                o = (c + 2) % 4
                counts[c] = max(part(*sides[c][1]), part(*sides[o][1])) if counts[o] is None else counts[o]
        if counts[0] != counts[2] or counts[1] != counts[3] or any(c < nd for c, nd in zip(counts, need)):
            return None
        return [(sd[0], sd[1], c) for sd, c in zip(sides, counts)]

    def _centre_keys(self, centre):
        out = []
        for kind, what, n in centre:
            if kind == "wall":
                L, x, y = what
                out.append([self._add_st(L, t) for t in self._place(L, x, y, n)])
            else:
                out.append(("rung", what))
        return out

    def _piece_keys(self, p):
        if p.kind == "mouth":
            return
        L = p.L
        if p.kind == "fixed":
            ks = [self._add_st(L, p.s0)]
            for k in range(L.n):
                s = _unwrap(L.vparam(k), p.s0, L.total)
                if s < p.s0 - 1e-6:
                    s += L.total
                if p.s0 + 1e-6 < s < p.s1 - 1e-6:
                    ks.append(self._add_st(L, s))
            ks.append(self._add_st(L, p.s1))
            p.keys = ks
            p.n = len(ks) - 1
            return
        p.keys = [self._add_st(L, p.s0 + (p.s1 - p.s0) * i / p.n) for i in range(p.n + 1)]

    def _tangent(self, p, at_end):
        if p.kind == "mouth":
            a = self._key_pos2(p.keys[0])
            b = self._key_pos2(p.keys[-1])
            d = b - a
            return d.normalized() if d.length > 1e-9 else Vector((1.0, 0.0))
        h = min(0.05, (p.s1 - p.s0) / 3)
        return p.L.tangent2(p.s1 - h if at_end else p.s0 + h)

    def _key_pos2(self, key):
        return self._frame.to2(self._key_pos(key))

    def _key_pos(self, key):
        if key in self._extra:
            return self._extra[key]
        if key[0] == "M":
            S = self._strips[key[1]]
            keys = self._mouth_keys(S, key[2])
            a, b = self._key_pos(keys[0]), self._key_pos(keys[-1])
            cap = S.cap.get(key[2])
            f = key[3] / S.w
            if cap is not None:
                L, s, ln = cap
                return L.at(s + ln * f)
            return a.lerp(b, f)
        L = self._loops[key[0]]
        return L.at(self._st[key[0]][key[1]])

    def _pick_corners(self, pieces):
        """Four patch corners: middles of fillets, and sharp convex turns away from fillets."""
        m = len(pieces)
        if m < 2:
            return None
        cands = []
        big = math.radians(45.0)

        def fillet(j):
            q = pieces[j % m]
            return q.kind == "arc" and abs(q.theta) >= math.radians(20.0)

        def short(j):
            q = pieces[j % m]
            return q.kind == "line" and q.length < 2.0

        for i, p in enumerate(pieces):
            if p.kind == "arc" and abs(p.theta) >= big:
                # fillets go first: a mouth corner next to a fillet would split the junction badly
                cands.append((math.pi + abs(p.theta), i, 'mid'))
        for i, p in enumerate(pieces):
            if fillet(i - 1) or fillet(i) or (short(i - 1) and fillet(i - 2)) or (short(i) and fillet(i + 1)):
                continue
            turn = _signed_angle(self._tangent(pieces[i - 1], True), self._tangent(p, False))
            if turn >= big:
                cands.append((turn, i, 'start'))
        if len(cands) < 4:
            return None
        cands.sort(key=lambda c: -c[0])
        chosen = sorted(cands[:4], key=lambda c: c[1] + (0.5 if c[2] == 'mid' else 0.0))
        return [(i, kind) for _, i, kind in chosen]

    def _balance(self, pieces, corners):
        """Chooses where fillet corners split and adds edges on straights so opposite sides match."""
        import itertools
        opts = []
        for i, kind in corners:
            if kind == 'mid':
                n = pieces[i].n
                c = n / 2.0
                opts.append(sorted({k for k in (math.floor(c) - 1, math.floor(c), math.ceil(c), math.ceil(c) + 1)
                                    if 0 <= k <= n}, key=lambda k: abs(k - c)))
            else:
                opts.append([0])
        cum = [0]
        for p in pieces:
            cum.append(cum[-1] + p.n)
        total = cum[-1]
        corner_pieces = {i for i, kind in corners if kind == 'mid'}
        best = None
        for combo in itertools.product(*opts):
            pos = [cum[i] + k for (i, _), k in zip(corners, combo)]
            lens = [(pos[(c + 1) % 4] - pos[c]) % total for c in range(4)]
            if 0 in lens or sum(lens) != total:
                continue
            groups = [[] for _ in range(4)]
            for j, p in enumerate(pieces):
                if j in corner_pieces or p.kind not in ("line", "arc"):
                    continue
                mid = cum[j] + p.n / 2.0
                side = next(c for c in range(4) if (mid - pos[c]) % total < lens[c])
                groups[side].append(j)
            cost = sum(abs(k - pieces[i].n / 2.0) for (i, kind), k in zip(corners, combo) if kind == 'mid') * 0.6
            adds = {}
            ok = True
            for a, b in ((0, 2), (1, 3)):
                plan = self._match_sides(pieces, (lens[a], groups[a]), (lens[b], groups[b]))
                if plan is None:
                    ok = False
                    break
                c, changes = plan
                cost += c
                for j, d in changes.items():
                    adds[j] = adds.get(j, 0) + d
            if ok and (best is None or cost < best[0]):
                best = (cost, combo, adds)
        if best is None:
            return None
        _, combo, adds = best
        for j, a in adds.items():
            pieces[j].n += a
        return [(i, k) for (i, _), k in zip(corners, combo)]

    @staticmethod
    def _match_sides(pieces, side_a, side_b):
        """Edge changes that give two opposite patch sides the same count: straights may grow or
        shrink (down to one edge), fillets may only grow and cost more."""
        def ways(count, group, target):
            mut = [j for j in group if pieces[j].mutable]
            arcs = [j for j in group if pieces[j].kind == "arc"]
            d = target - count
            if d == 0:
                return 0.0, {}
            change = {}
            if d < 0:
                spare = sum(pieces[j].n - 1 for j in mut)
                if spare < -d:
                    return None
                for _ in range(-d):
                    j = min((x for x in mut if pieces[x].n + change.get(x, 0) > 1),
                            key=lambda x: pieces[x].length / (pieces[x].n + change.get(x, 0) - 1))
                    change[j] = change.get(j, 0) - 1
                return float(-d), change
            pool, weight = (mut, 1.0) if mut else (arcs, 3.0)
            if not pool:
                return None
            for _ in range(d):
                j = max(pool, key=lambda x: pieces[x].length / (pieces[x].n + change.get(x, 0) + 1))
                change[j] = change.get(j, 0) + 1
            return d * weight, change

        (ca, ga), (cb, gb) = side_a, side_b
        best = None
        for target in range(1, 2 * max(ca, cb) + 2):
            wa, wb = ways(ca, ga, target), ways(cb, gb, target)
            if wa is None or wb is None:
                continue
            cost = wa[0] + wb[0]
            if best is None or cost < best[0]:
                best = (cost, {**wa[1], **wb[1]})
        return best

    # ---------- geometry ----------

    def _build(self, bm, obj):
        frame = self._frame
        road, curb = self._road, self._curb
        uv_layers = list(bm.loops.layers.uv.values())
        road_uv = self._road_uv_maps(road, uv_layers)
        self._ground = self._ground_bvh(bm, road)
        road_smooth = sum(1 for f in road if f.smooth) * 2 >= len(road)
        road_mat = next(iter(road)).material_index

        # which loop verts survive
        keep = {}
        for L in self._loops:
            for i in L.anchor:
                keep[(L.idx, round(L.vparam(i) % L.total, 4))] = L.verts[i]
        self._keep = keep

        verts = {}

        def vert(key):
            v = verts.get(key)
            if v is None:
                v = keep.get(key)
                if v is None:
                    co = self._key_pos(key)
                    if key[0] == "Q" or key[0] == "M" and self._strips[key[1]].cap.get(key[2]) is None:
                        co = self._drape(co)
                    v = bm.verts.new(co)
                verts[key] = v
            return v

        new_faces = []
        curb_data = self._curb_plan(uv_layers)

        old_faces = set(road)
        for L in self._loops:
            for first, count, cyclic in L.curb_runs:
                for c in range(count):
                    for row in L.curb_cols[(first + c) % L.n]:
                        old_faces.add(row[0])
        old_verts = {v for f in old_faces for v in f.verts}
        old_edges = {e for f in old_faces for e in f.edges}
        bmesh.ops.delete(bm, geom=list(old_faces), context='FACES_ONLY')

        def face(vs, mat, smooth):
            out = []
            for v in vs:
                if not out or out[-1] is not v:
                    out.append(v)
            if len(out) > 1 and out[0] is out[-1]:
                out.pop()
            if len(out) < 3 or len(set(out)) != len(out):
                return None
            try:
                f = bm.faces.new(out)
            except ValueError:
                return None
            f.material_index = mat
            f.smooth = smooth
            new_faces.append(f)
            return f

        road_new = []
        # strips
        for S in self._strips:
            keys_a, keys_b, N = self._strip_st[S.idx]
            grid = []
            for i in range(N + 1):
                if i == 0:
                    row = self._mouth_keys(S, 'start')
                elif i == N:
                    row = self._mouth_keys(S, 'end')
                else:
                    row = [keys_a[i]] + [("S", S.idx, i, j) for j in range(1, S.w)] + [keys_b[i]]
                grid.append(row)
            for i in range(1, N):
                a, b = self._key_pos(keys_a[i]), self._key_pos(keys_b[i])
                for j in range(1, S.w):
                    verts[("S", S.idx, i, j)] = bm.verts.new(self._drape(a.lerp(b, j / S.w)))
            for i in range(N):
                for j in range(S.w):
                    f = face([vert(grid[i][j]), vert(grid[i + 1][j]), vert(grid[i + 1][j + 1]), vert(grid[i][j + 1])],
                             road_mat, road_smooth)
                    if f:
                        road_new.append(f)
        # junctions
        for r, (mode, pieces, plan) in enumerate(self._junction_data):
            if mode == "plus":
                road_new += self._build_plus(bm, r, plan, vert, verts, face, road_mat, road_smooth)
                continue
            ring = self._ring(pieces)
            if mode == "grid":
                fs = self._coons(bm, r, pieces, plan, vert, verts, face, road_mat, road_smooth)
                if fs is not None:
                    road_new += fs
                    continue
            f = face([vert(k) for k in ring], road_mat, road_smooth)
            if f:
                road_new.append(f)

        # curbs
        for item in curb_data:
            new_faces += self._curb_faces(bm, item, vert, verts, face)

        # clean up what the old faces left
        bmesh.ops.delete(bm, geom=[e for e in old_edges if e.is_valid and not e.link_faces], context='EDGES')
        bmesh.ops.delete(bm, geom=[v for v in old_verts if v.is_valid and not v.link_edges], context='VERTS')

        self._apply_road_uv(road_new, road_uv, uv_layers)
        for f in new_faces:
            f.select = True
        self._fix_normals(road_new)
        self.summary = (len(self._strips), sum(1 for d in self._junction_data if d[0] != "ngon"),
                        sum(1 for d in self._junction_data if d[0] == "ngon"))
        return new_faces

    def _build_plus(self, bm, jid, plan, vert, verts, face, mat, smooth):
        out = []
        rungs = {}
        for a, e in enumerate(plan["ext"]):
            arm = plan["arms"][a]
            w = plan["w"][a]
            if e is None:
                rungs[a] = arm
                continue
            k1, k2, n = e
            rows = [arm]
            for r in range(1, n + 1):
                left, right = k1[r], k2[r]
                pl, pr = self._key_pos(left), self._key_pos(right)
                row = [left]
                for j in range(1, w):
                    key = ("E", jid, a, r, j)
                    self._extra[key] = self._drape(pl.lerp(pr, j / w))
                    row.append(key)
                row.append(right)
                rows.append(row)
            for r in range(n):
                for j in range(w):
                    f = face([vert(rows[r][j]), vert(rows[r][j + 1]), vert(rows[r + 1][j + 1]), vert(rows[r + 1][j])],
                             mat, smooth)
                    if f:
                        out.append(f)
            rungs[a] = rows[n]
        for hub, ks in plan["fans"]:
            for i in range(0, len(ks) - 2, 2):
                f = face([vert(hub), vert(ks[i]), vert(ks[i + 1]), vert(ks[i + 2])], mat, smooth)
                if f:
                    out.append(f)
        sides =[c if isinstance(c, list) else rungs[c[1]] for c in plan["centre"]]
        fs = self._coons_sides(bm, ("P", jid), sides, vert, verts, face, mat, smooth)
        if fs is None:
            ring = []
            for sd in sides:
                ring += sd[:-1]
            f = face([vert(k) for k in ring], mat, smooth)
            fs = [f] if f else []
        return out + fs

    def _drape(self, co):
        """Inner road points lie on the old road surface, so humps and slopes stay as they were."""
        if self._ground is None:
            return co
        up = self._frame.up
        # the nearest surface straight above or below: a bridge over the road does not catch it
        best = None
        for d in (up, -up):
            loc, _, _, dist = self._ground.ray_cast(co - d * 1e-4, d, 5.0)
            if loc is not None and (best is None or dist < best[0]):
                best = (dist, loc)
        return best[1] if best else co

    def _fix_normals(self, faces):
        up = self._frame.up
        for f in faces:
            f.normal_update()
            if f.normal.dot(up) < 0:
                f.normal_flip()

    @staticmethod
    def _ring(pieces):
        ring = []
        for p in pieces:
            ks = p.keys
            if ring and ring[-1] == ks[0]:
                ks = ks[1:]
            ring += ks
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        return ring

    def _coons(self, bm, r, pieces, pos, vert, verts, face, mat, smooth):
        m = len(pieces)
        # boundary as a flat list with piece/vertex index -> position in the list
        flat = []
        where = {}
        for i, p in enumerate(pieces):
            for k, key in enumerate(p.keys):
                if k == 0 and flat and flat[-1] == key:
                    where[(i, 0)] = len(flat) - 1
                    continue
                where[(i, k)] = len(flat)
                flat.append(key)
        if len(flat) > 1 and flat[0] == flat[-1]:
            flat.pop()
            for k, v in list(where.items()):
                if v == len(flat):
                    where[k] = 0
        total = len(flat)
        idx = [where[(i, k)] for i, k in pos]
        sides = []
        for c in range(4):
            a, b = idx[c], idx[(c + 1) % 4]
            run = [flat[(a + t) % total] for t in range(((b - a) % total) + 1)]
            sides.append(run)
        return self._coons_sides(bm, ("C", r), sides, vert, verts, face, mat, smooth)

    def _coons_sides(self, bm, tag, sides, vert, verts, face, mat, smooth):
        """Grid patch from four boundary key lists running around the patch."""
        na, nb = len(sides[0]) - 1, len(sides[1]) - 1
        if na < 1 or nb < 1 or len(sides[2]) - 1 != na or len(sides[3]) - 1 != nb:
            return None
        P = [[None] * (nb + 1) for _ in range(na + 1)]
        bottom = [self._key_pos(k) for k in sides[0]]
        right = [self._key_pos(k) for k in sides[1]]
        top = [self._key_pos(k) for k in reversed(sides[2])]
        left = [self._key_pos(k) for k in reversed(sides[3])]

        def params(pts):
            acc = [0.0]
            for a, b in zip(pts, pts[1:]):
                acc.append(acc[-1] + (b - a).length)
            return [x / acc[-1] if acc[-1] > 1e-12 else i / (len(pts) - 1) for i, x in enumerate(acc)]

        ub, ut, vl, vr = params(bottom), params(top), params(left), params(right)
        c00, c10, c11, c01 = bottom[0], bottom[-1], top[-1], top[0]
        for i in range(na + 1):
            for j in range(nb + 1):
                if i in (0, na) or j in (0, nb):
                    continue
                v = vl[j] * (1 - ub[i]) + vr[j] * ub[i]
                u = ub[i] * (1 - v) + ut[i] * v
                v = vl[j] * (1 - u) + vr[j] * u
                pt = (bottom[i] * (1 - v) + top[i] * v + left[j] * (1 - u) + right[j] * u
                      - (c00 * (1 - u) * (1 - v) + c10 * u * (1 - v) + c11 * u * v + c01 * (1 - u) * v))
                P[i][j] = pt
        # a few relax passes keep the cells even
        for _ in range(self.relax):
            for i in range(1, na):
                for j in range(1, nb):
                    nbrs = []
                    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        x, y = i + di, j + dj
                        if x in (0, na) or y in (0, nb):
                            nbrs.append(bottom[x] if y == 0 else top[x] if y == nb else left[y] if x == 0 else right[y])
                        else:
                            nbrs.append(P[x][y])
                    P[i][j] = P[i][j].lerp(sum(nbrs, Vector()) / 4.0, 0.5)
        keys = [[None] * (nb + 1) for _ in range(na + 1)]
        for i in range(na + 1):
            keys[i][0] = sides[0][i]
            keys[i][nb] = sides[2][na - i]
        for j in range(nb + 1):
            keys[0][j] = sides[3][nb - j]
            keys[na][j] = sides[1][j]
        for i in range(1, na):
            for j in range(1, nb):
                key = tag + (i, j)
                verts[key] = bm.verts.new(self._drape(P[i][j]))
                keys[i][j] = key
        out = []
        for i in range(na):
            for j in range(nb):
                f = face([vert(keys[i][j]), vert(keys[i + 1][j]), vert(keys[i + 1][j + 1]), vert(keys[i][j + 1])],
                         mat, smooth)
                if f:
                    out.append(f)
        return out

    # ---------- UV ----------

    @staticmethod
    def _ground_bvh(bm, road):
        """The old road surface split into the same triangles Blender shows (long quads are often not flat)."""
        bm.faces.index_update()
        keep = {f.index for f in road}
        tmp = bm.copy()
        tmp.faces.ensure_lookup_table()
        bmesh.ops.delete(tmp, geom=[f for f in tmp.faces if f.index not in keep], context='FACES')
        bvh = BVHTree.FromBMesh(tmp) if tmp.faces else None
        tmp.free()
        return bvh

    def _road_uv_maps(self, road, layers):
        """Per old road face: affine map from ground position to UV, so new faces keep the old mapping."""
        frame = self._frame
        faces = list(road)
        coords = []
        polys = []
        maps = []
        for f in faces:
            base = len(coords)
            coords += [v.co.copy() for v in f.verts]
            polys.append(tuple(range(base, base + len(f.verts))))
            per_layer = []
            pts = [frame.to2(v.co) for v in f.verts]
            for lay in layers:
                uvs = [l[lay].uv.copy() for l in f.loops]
                per_layer.append(self._affine(pts, uvs))
            maps.append(per_layer)
        bvh = BVHTree.FromPolygons(coords, polys) if polys else None
        return bvh, maps

    @staticmethod
    def _affine(pts, uvs):
        cx = sum((p for p in pts), Vector((0.0, 0.0))) / len(pts)
        cu = sum((u for u in uvs), Vector((0.0, 0.0))) / len(uvs)
        sxx = sxy = syy = 0.0
        bx = Vector((0.0, 0.0))
        by = Vector((0.0, 0.0))
        for p, u in zip(pts, uvs):
            d = p - cx
            du = u - cu
            sxx += d.x * d.x
            sxy += d.x * d.y
            syy += d.y * d.y
            bx += du * d.x
            by += du * d.y
        det = sxx * syy - sxy * sxy
        if abs(det) < 1e-12:
            return cx, cu, Matrix(((0.0, 0.0), (0.0, 0.0)))
        inv = Matrix(((syy / det, -sxy / det), (-sxy / det, sxx / det)))
        # uv = cu + M (p - cx);  M = [bx by] * inv
        B = Matrix(((bx.x, by.x), (bx.y, by.y)))
        return cx, cu, B @ inv

    def _apply_road_uv(self, faces, road_uv, layers):
        bvh, maps = road_uv
        if bvh is None or not layers:
            return
        frame = self._frame
        for f in faces:
            c = f.calc_center_median()
            hit = bvh.find_nearest(c)
            if hit[2] is None:
                continue
            per = maps[hit[2]]
            for l in f.loops:
                p = frame.to2(l.vert.co)
                for lay, (cx, cu, M) in zip(layers, per):
                    l[lay].uv = cu + M @ (p - cx)

    # ---------- curb ----------

    def _curb_plan(self, layers):
        """Everything needed to rebuild the curb rows on the new stations, captured before deleting."""
        plans = []
        for L in self._loops:
            for first, count, cyclic in L.curb_runs:
                cols = [L.curb_cols[(first + c) % L.n] for c in range(count)]
                nrows = len(cols[0])
                idxs = [(first + c) % L.n for c in range(count + (0 if cyclic else 1))]
                # rails of every row edge: rail 0 is the road side
                rails = [[L.verts[i] for i in idxs]]
                for r in range(nrows):
                    rail = [cols[c][r][1] for c in range(count)]
                    if not cyclic:
                        rail.append(cols[-1][r][2])
                    rails.append(rail)
                corner = {c for c, i in enumerate(idxs) if i in L.corner}
                curves = [None]
                for r in range(1, nrows + 1):
                    pts = [v.co.copy() for v in rails[r]]
                    dense = _smooth_dense(pts, cyclic, corner, set())
                    curves.append(dense)
                uv = []
                for r in range(nrows):
                    row = []
                    off = [Vector((0.0, 0.0)) for _ in layers]
                    for c in range(count):
                        f, na, nb, forward = cols[c][r]
                        a = L.verts[idxs[c]] if r == 0 else cols[c][r - 1][1]
                        b = L.verts[idxs[(c + 1) % len(idxs)]] if r == 0 else cols[c][r - 1][2]
                        lp = {l.vert: l for l in f.loops}
                        corners = []
                        for k, lay in enumerate(layers):
                            q = [lp[a][lay].uv.copy(), lp[b][lay].uv.copy(), lp[nb][lay].uv.copy(), lp[na][lay].uv.copy()]
                            if c > 0:
                                # unwrap old seams: this column continues where the previous one ended
                                off[k] = row[c - 1][1][k][1] + row[c - 1][2][k] - q[0]
                            corners.append(q)
                        row.append((f, corners, [o.copy() for o in off]))
                    uv.append(row)
                plans.append({"L": L, "first": first, "count": count, "cyclic": cyclic, "rows": nrows,
                              "rails": rails, "curves": curves, "uv": uv, "idxs": idxs,
                              "mat": [[cols[c][r][0].material_index for r in range(nrows)] for c in range(count)],
                              "smooth": [[cols[c][r][0].smooth for r in range(nrows)] for c in range(count)],
                              "forward": [cols[0][r][3] for r in range(nrows)]})
        return plans

    def _curb_faces(self, bm, plan, vert, verts, face):
        L = plan["L"]
        first, count, cyclic = plan["first"], plan["count"], plan["cyclic"]
        layers = list(bm.loops.layers.uv.values())
        s_first = L.vparam(first)
        span = L.total if cyclic else ((L.vparam(first + count) - s_first) % L.total or L.total)
        # stations of the road side inside this run, as (column, fraction)
        stations = []
        for key, s in self._st[L.idx].items():
            u = (s - s_first) % L.total
            if u > L.total - 1e-6:
                u = 0.0
            if cyclic or u <= span + 1e-6:
                stations.append((u, (L.idx, key)))
        stations.sort()
        if len(stations) < 2:
            return []
        out = []

        rail_keys = []
        for u, key in stations:
            k, f = L.seg_frac(s_first + u)
            c = (k - first) % L.n
            if not cyclic and u >= span - 1e-6:
                c, f = count, 0.0
            elif f > 1 - 1e-6:
                c, f = c + 1, 0.0
            rail_keys.append((u, key, c, f))
        if cyclic:
            u0, key0, c0, f0 = rail_keys[0]
            rail_keys.append((u0 + L.total, key0, c0 + count, f0))

        def rail_vert(r, item):
            u, key, c, f = item
            if r == 0:
                return vert(key)
            cc = c % count if cyclic else c
            vk = ("K", L.idx, first, r, round(u % L.total, 4) if cyclic else round(u, 4))
            v = verts.get(vk)
            if v is None:
                if f < 1e-6 and key in self._keep:
                    # the road vertex stays, so its rung stays too
                    v = plan["rails"][r][cc]
                else:
                    v = bm.verts.new(self._rail_point(plan, r, cc, f))
                verts[vk] = v
            return v

        for a, b in zip(rail_keys, rail_keys[1:]):
            for r in range(plan["rows"]):
                ca = a[2] % count if cyclic else min(a[2], count - 1)
                v0, v1 = rail_vert(r, a), rail_vert(r, b)
                v2, v3 = rail_vert(r + 1, b), rail_vert(r + 1, a)
                vs = [v0, v1, v2, v3] if plan["forward"][r] else [v1, v0, v3, v2]
                f = face(vs, plan["mat"][ca][r], plan["smooth"][ca][r])
                if f is None:
                    continue
                out.append(f)
                if layers:
                    ua = self._curb_uv(plan, r, a, after=True)
                    ub = self._curb_uv(plan, r, b, after=False)
                    for l in f.loops:
                        side = 0 if l.vert in (v0, v3) else 1
                        hi = l.vert in (v2, v3)
                        src = ua if side == 0 else ub
                        for k, lay in enumerate(layers):
                            l[lay].uv = src[k][1 if hi else 0]
        return out

    @staticmethod
    def _rail_point(plan, r, c, f):
        dense = plan["curves"][r]
        n = len(dense)
        c = c % max(1, plan["count"]) if plan["cyclic"] else min(c, plan["count"])
        if not plan["cyclic"] and c >= plan["count"]:
            return dense[-1].copy()
        seg = dense[c * _D:c * _D + _D + 1]
        if len(seg) < _D + 1:
            seg = seg + [dense[(c * _D + len(seg) + k) % n] for k in range(_D + 1 - len(seg))]
        acc = [0.0]
        for p, q in zip(seg, seg[1:]):
            acc.append(acc[-1] + (q - p).length)
        s = f * acc[-1]
        j = max(0, min(_D - 1, bisect.bisect_right(acc, s) - 1))
        span = acc[j + 1] - acc[j]
        t = (s - acc[j]) / span if span > 1e-12 else 0.0
        return seg[j].lerp(seg[j + 1], t)

    @staticmethod
    def _curb_uv(plan, r, item, after):
        """UV of the low and high rail of row r at a station, continuous across old seams."""
        u, key, c, f = item
        count = plan["count"]
        row = plan["uv"][r]
        wraps = 0
        if plan["cyclic"]:
            wraps, c = divmod(c, count)
        if not after and f < 1e-6 and c > 0:
            c, f = c - 1, 1.0
        elif not after and f < 1e-6 and c == 0 and plan["cyclic"] and wraps > 0:
            c, f, wraps = count - 1, 1.0, wraps - 1
        c = min(c, count - 1)
        _, corners, off = row[c]
        out = []
        for k, q in enumerate(corners):
            o = off[k]
            if wraps:
                last = row[-1]
                o = o + (last[2][k] + last[1][k][1] - row[0][1][k][0]) * wraps
            low = q[0].lerp(q[1], f) + o
            high = q[3].lerp(q[2], f) + o
            out.append((low, high))
        return out


class MESH_OT_road_remesh(RoadRemeshCore, bpy.types.Operator):
    bl_idname = "mesh.road_remesh"
    bl_label = "Road Remesh"
    bl_description = ("Пересобрать сетку дороги с бордюрами: полосы, изгибы и развязки, "
                      "скругления по углу, прямые по длине ребра")
    bl_options = {'REGISTER', 'UNDO'}

    seg_90: IntProperty(
        name="Сегментов на 90°", default=6, min=1, max=64,
        description="Сколько рёбер получает скругление в 90°. Меньший угол — пропорционально меньше")
    edge_length: FloatProperty(
        name="Длина ребра", default=4.0, min=0.1, max=100.0, unit='LENGTH',
        description="Шаг рёбер на прямых участках дороги")
    width_div: IntProperty(
        name="Делений по ширине", default=1, min=1, max=16,
        description="Сколько продольных полос у дороги")
    junction_mode: EnumProperty(
        name="Развязки",
        items=(('GRID', "Сетка", "Развязка заполняется квадами"),
               ('NGON', "N-гон", "Развязка — одна грань")),
        default='GRID')
    relax: IntProperty(
        name="Сглаживание развязок", default=4, min=0, max=50,
        description="Сколько проходов выравнивания сетки внутри развязки")
    corner_angle: FloatProperty(
        name="Угол излома", default=math.radians(50.0), min=math.radians(5.0), max=math.radians(170.0),
        subtype='ANGLE', description="Изломы круче этого угла не скругляются, на них остаётся вершина")
    max_width: FloatProperty(
        name="Макс. ширина", default=40.0, min=1.0, max=500.0, unit='LENGTH',
        description="Стороны дальше друг от друга не считаются одной дорогой")

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def draw(self, context):
        draw_params(self.layout, self, "")

    def execute(self, context):
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        try:
            faces = self._run(bm, obj)
        except _Fail as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, describe(self.summary) + f", {len(faces)} граней")
        return {'FINISHED'}


PARAM_KEYS = ("seg_90", "edge_length", "width_div", "junction_mode", "relax", "corner_angle", "max_width")


def describe(summary):
    strips, grids, ngons = summary
    text = f"Полос: {strips}, развязок: {grids}"
    if ngons:
        text += f", n-гонов: {ngons}"
    return text


def draw_params(layout, data, prefix):
    col = layout.column(align=True)
    col.prop(data, prefix + "seg_90")
    col.prop(data, prefix + "edge_length")
    col.prop(data, prefix + "width_div")
    row = layout.row(align=True)
    row.label(text="Развязки")
    row.prop(data, prefix + "junction_mode", expand=True)
    col = layout.column(align=True)
    col.prop(data, prefix + "relax")
    col.prop(data, prefix + "corner_angle")
    col.prop(data, prefix + "max_width")


def register():
    bpy.utils.register_class(MESH_OT_road_remesh)


def unregister():
    bpy.utils.unregister_class(MESH_OT_road_remesh)
