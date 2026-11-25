# =====================================================
# Fillet Sections — меняет число сечений в скруглении: веер от
# центральной точки и кольца вокруг него (бордюр, тротуар) делятся
# заново на N равных шагов. Контур не сдвигается, концы скругления
# и соседняя геометрия не двигаются, UV и цвета вершин переносятся.
# Edit Mode → выделить грани скругления → вкладка MadOut2 V2.
# =====================================================

import math
from collections import Counter

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .strip_resample import (StripResampleCore, _Fail, _arc, _chains_from_region, _dense_curve,
                             _sample)
from . import fillet_align, fillet_radius
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
    "По радиусу — по таблице: радиус даёт сегменты",
    "на полный круг, угол — какую долю круга взять.",
    "  R 0.25 м — 16 на круг (4 на 90°), 0.5 — 24 (6),",
    "  1 — 28 (8), 1.5 — 32 (8), 2 — 34 (8), 2.5 — 36 (10),",
    "  3 — 38 (10), 4 — 42 (10), 5 — 44 (12),",
    "  больше 5 м — изгиб дороги: сечения через",
    "  «Шаг на длинных дугах» (по умолчанию 4 м).",
    "  Радиус — по внешнему кольцу скругления.",
    "Новый радиус — скругление строится заново:",
    "  дуга касается тех же прямых, кольца идут",
    "  следом, концы выделения на месте. Больше",
    "  радиус — дуга начинается дальше по прямым:",
    "  их грани берутся сами; если прямая кончается,",
    "  скажет максимум.",
    "Плавность 0 — точки на старых рёбрах, контур",
    "  не двигается; 1 — на гладкой дуге.",
    "Концы скругления не сдвигаются. Откат — Ctrl+Z.",
)


def _hub_side(hub, region, edges):
    """The hub, and the straight the inner side may run on along from it. A rounding that goes into
    a straight can end its fan in a QUAD from the hub (Dima's corner: hub 8642, then a quad on to
    8386 along the top), and that edge out of the hub is the inner side carried on, not a ray. It
    is walked from quad to quad along the boundary, never twice through one quad - a quad's own
    second boundary edge is the end of the rounding, which is where the walk must stop."""
    side = [hub]
    for start in [e for e in hub.link_edges if e in edges]:
        quads = [f for f in start.link_faces if f in region]
        if len(quads) != 1 or len(quads[0].verts) != 4:
            continue  # a ray inside the fan, or the fan's own first or last triangle
        e, used, v = start, {quads[0]}, hub
        while True:
            v = e.other_vert(v)
            if v in side:
                break
            side.append(v)
            nxt = None
            for g in v.link_edges:
                if g is e or g not in edges:
                    continue
                fs = [f for f in g.link_faces if f in region]
                if len(fs) == 1 and len(fs[0].verts) == 4 and fs[0] not in used:
                    nxt = (g, fs[0])
                    break
            if nxt is None:
                break
            e, q = nxt
            used.add(q)
    return side


def _fan_chains(region):
    """
    The hub side of a fan and the rings round it, every ring ordered from one end of the rounding
    to the other and the rings ordered outwards. Returns (inner, rings, error): inner is the hub
    side laid out vertex for vertex against the first ring; None when the selection has no fan.

    Rings are found as layers of edge distance from the hub side, not by geometry: a kerb's rings
    stand one above the other (bottom and top of the kerb face share x and y), so anything measured
    in the plane would merge them, while in the grid of a rounding every ring is exactly one edge
    further out than the one inside it.
    """
    verts = {v for f in region for v in f.verts}
    hub, best = None, 0
    for v in verts:
        tris = sum(1 for f in v.link_faces if f in region and len(f.verts) == 3)
        if tris >= 2 and tris > best:
            hub, best = v, tris
    if hub is None:
        return None, None, None

    edges = {e for f in region for e in f.edges}
    side = _hub_side(hub, region, edges)
    layer = {v: 0 for v in side}
    frontier = list(side)
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
        return None, None, "Выделенные грани должны быть связаны"

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
            return None, None, f"Кольцо {k} не цепочка. {uneven}"
        order, prev = [ends[0]], None
        while len(order) < len(ring):
            nxt = [w for w in adj[order[-1]] if w is not prev]
            if not nxt:
                break
            prev = order[-1]
            order.append(nxt[0])
        if len(order) != len(ring):
            return None, None, f"Кольцо {k} не цепочка. {uneven}"
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
        return None, None, f"Концы колец {k} и {k + 1} не связаны ребром. {uneven}"
    if not rings:
        return None, None, "Вокруг центра нет ни одного кольца"
    inner = _expand(side, rings[0], region)
    if inner is None:
        return None, None, f"Центр не связан с первым кольцом гранями по одной на отрезок. {uneven}"
    return inner, rings, None


def _welded(ring, region):
    """Inner points of a ring that StripResampleCore._anchored will refuse to remove once the
    selected faces are gone: an edge leading out of the selection, or a triangle outside it."""
    edges = {e for f in region for e in f.edges}
    return sum(1 for v in ring[1:-1]
               if any(e not in edges for e in v.link_edges)
               or any(f not in region and len(f.verts) <= 3 for f in v.link_faces))


def _turn(points):
    """How far the rounding turns: the turns between neighbouring chords added up, signed against the
    ring's own plane, so a U-turn and a loop past 180 degrees read true (the angle between the first
    and last chord alone tops out at 180). Chords of an arc turn by (n - 1) / n of it - half a step
    is lost at each end - so the sum is scaled back up; read raw, 45 degrees in five pieces was 36."""
    n = len(points) - 1
    if n < 2:
        return 0.0
    normal = Vector()
    for i in range(1, n):
        normal += (points[i] - points[0]).cross(points[i + 1] - points[0])
    total = 0.0
    for a, b, c in zip(points, points[1:], points[2:]):
        d0, d1 = b - a, c - b
        if d0.length < 1e-9 or d1.length < 1e-9:
            continue
        ang = d0.angle(d1)
        if normal.length > 1e-12 and d0.cross(d1).dot(normal) < 0.0:
            ang = -ang
        total += ang
    return abs(total) * n / (n - 1)


def _measure(rings, mw):
    """(turn, radius) of a rounding: how far it turns, and the radius of its OUTERMOST ring - the one
    whose chords cut furthest inside the true arc. The radius comes from the ring's length and its
    turn (n equal chords of an arc of radius R, turning by t, are 2 n R sin(t / 2n) long), so a ring
    that climbs or stands on a slope needs no plane to be fitted to it."""
    # The turn is read off the LONGEST ring, the one the radius is taken from too. The innermost
    # ring is the worst choice: where a fan ends in a quad its last chord is bent (17.7 degrees
    # against 15 on Dima's corner) and a 90 degree rounding read as 91.
    longest = max(rings, key=lambda r: sum(((mw @ b.co) - (mw @ a.co)).length for a, b in zip(r, r[1:])))
    turn = _turn([mw @ v.co for v in longest])
    radius = 0.0
    for r in rings:
        p = [mw @ v.co for v in r]
        n = len(p) - 1
        length = sum((b - a).length for a, b in zip(p, p[1:]))
        if n >= 1 and turn > 1e-6:
            radius = max(radius, length / (2.0 * n * math.sin(turn / (2.0 * n))))
    return turn, radius


# A segment turning by less than this share of the arc's own step is a straight between two arcs
STRAIGHT_SHARE = 0.35


def _segment_turns(points):
    """How far each SEGMENT of a ring turns, not each joint. On an arc every chord turns by the same
    step; at a tangent point between an arc and a straight the JOINT turns by half a step (Dima's
    U-turn: 18, 18, 9, 9, 18 degrees), so reading joints a straight looks like two half-turns and
    nothing more. A joint's turn is the mean of its two segments', t_i = (s_i + s_(i+1)) / 2, which
    fixes every s but one free number c that enters with alternating sign; c is the median of what
    each pair of neighbouring segments says it should be, so a straight or two cannot drag it."""
    n = len(points) - 1
    if n < 2:
        return None
    normal = Vector()
    for i in range(1, n):
        normal += (points[i] - points[0]).cross(points[i + 1] - points[0])
    t = []
    for a, b, c in zip(points, points[1:], points[2:]):
        d0, d1 = b - a, c - b
        ang = d0.angle(d1) if d0.length > 1e-9 and d1.length > 1e-9 else 0.0
        if normal.length > 1e-12 and d0.cross(d1).dot(normal) < 0.0:
            ang = -ang
        t.append(ang)
    a = [0.0]
    for i in range(n - 1):
        a.append(2.0 * t[i] - a[i])
    guesses = sorted((a[i + 1] - a[i]) / 2.0 * (1.0 if i % 2 == 0 else -1.0) for i in range(n - 1))
    c = guesses[len(guesses) // 2]
    return [a[j] + (c if j % 2 == 0 else -c) for j in range(n)]


def _pieces(rings, mw, keep_straight):
    """The rounding cut into arcs and the straights between them: [(first, last, straight), ...] as
    segment indices, the same on every ring. A straight is so on average over the rings, measured
    against each ring's own arc step. None when it is all one arc (or the rings do not pair up
    segment for segment, which is the only way the pieces could be read off them)."""
    counts = {len(r) for r in rings}
    if not keep_straight or len(counts) != 1 or len(rings[0]) < 4:
        return None
    n = len(rings[0]) - 1
    shares = [0.0] * n
    used = 0
    for r in rings:
        st = _segment_turns([mw @ v.co for v in r])
        if st is None:
            continue
        mags = sorted(abs(x) for x in st)
        step = mags[(3 * len(mags)) // 4]
        if step < math.radians(1.0):
            continue
        # Reading a segment's own turn from the joints amplifies noise - an error at one joint
        # carries on, alternating, down the whole ring. On a hand-placed arc whose joints turn
        # 3 to 12 degrees it gave segments of -31 and +14, and the straights it then "found" cut one
        # 7 m rounding into five 2 m arcs of 2 sections each (Dima: "почему странно линии
        # строятся"). A ring with any segment turning back by more than half a step is not read.
        bend = 1.0 if sum(st) >= 0.0 else -1.0
        if any(x * bend < -0.5 * step for x in st):
            continue
        for j in range(n):
            shares[j] += abs(st[j]) / step
        used += 1
    if not used:
        return None
    straight = [shares[j] / used < STRAIGHT_SHARE for j in range(n)]
    # Checked against the signature of a real straight between two arcs, read off the JOINTS
    # (which noise does not get amplified in): inside it the joints do not turn, and at each end a
    # joint turns about half an arc step - the chord leaves the arc at the tangent point (Dima's
    # U-turn: 18, 18, 9, 9, 18). A run whose end joints turn like the arc's is a noisy arc.
    joints = [0.0] * (n - 1)
    for r in rings:
        p = [mw @ v.co for v in r]
        for i, (a, b, c) in enumerate(zip(p, p[1:], p[2:])):
            d0, d1 = b - a, c - b
            if d0.length > 1e-9 and d1.length > 1e-9:
                joints[i] += d0.angle(d1) / len(rings)
    # The typical turn of the arc's joints: all joints that turn at all. Taking only the joints with
    # arc on both sides found none at all on a zigzag arc where every other segment read straight.
    turning = sorted(t for t in joints if t > math.radians(1.0))
    if turning:
        typical = turning[len(turning) // 2]
        j = 0
        while j < n:
            if not straight[j]:
                j += 1
                continue
            k = j
            while k + 1 < n and straight[k + 1]:
                k += 1
            inside = joints[j:k]
            ends = ([joints[j - 1]] if j > 0 else []) + ([joints[k]] if k < n - 1 else [])
            if any(t > 0.3 * typical for t in inside) or any(t > 0.75 * typical for t in ends):
                for m in range(j, k + 1):
                    straight[m] = False
            j = k + 1
    if not any(straight) or all(straight):
        return None
    pieces = []
    start = 0
    for j in range(1, n + 1):
        if j == n or straight[j] != straight[start]:
            pieces.append((start, j - 1, straight[start]))
            start = j
    return pieces


def _arc_of(rings, mw, first, last):
    """(turn, radius) of one arc piece: turn summed over its segments on the longest ring, radius
    the largest of the rings' - chords of angle s on a circle of radius R are 2 R sin(s / 2) long."""
    best = (0.0, 0.0, 0.0)
    radius = 0.0
    for r in rings:
        p = [mw @ v.co for v in r]
        st = _segment_turns(p)
        seg = range(first, last + 1)
        length = sum((p[j + 1] - p[j]).length for j in seg)
        turn = abs(sum(st[j] for j in seg))
        chord = sum(2.0 * math.sin(abs(st[j]) / 2.0) for j in seg)
        if chord > 1e-9:
            radius = max(radius, length / chord)
        if length > best[0]:
            best = (length, turn, 0.0)
    return best[1], radius


def _share(total, weights):
    """total split over the weights, every share at least 1, largest remainders first."""
    k = len(weights)
    if total <= k:
        return [1] * k
    w = sum(weights) or float(k)
    raw = [max(0.0, x) / w * (total - k) for x in weights]
    out = [1 + int(x) for x in raw]
    left = total - sum(out)
    for i in sorted(range(k), key=lambda i: raw[i] - int(raw[i]), reverse=True)[:left]:
        out[i] += 1
    return out


# Segments on a FULL circle for a rounding of this radius - the studio's table. Between its rows the
# count is read off the straight line between them; past the last row it carries on at the last
# row's rate (2 more per metre), below the first it stays at the first.
CIRCLE_TABLE = (
    (0.25, 16), (0.5, 24), (1.0, 28), (1.5, 32), (2.0, 34),
    (2.5, 36), (3.0, 38), (4.0, 42), (5.0, 44),
)


# What the last count was worked out from (turn, radius, even), so the tab can point at the row of
# the table it used. Written by the preview and by the operator alike; display only.
last_measure = {}


def segments_per_circle(radius):
    t = CIRCLE_TABLE
    if radius <= t[0][0]:
        return float(t[0][1])
    for (r0, n0), (r1, n1) in zip(t, t[1:]):
        if radius <= r1:
            return n0 + (n1 - n0) * (radius - r0) / (r1 - r0)
    (r0, n0), (r1, n1) = t[-2], t[-1]
    return n1 + (n1 - n0) / (r1 - r0) * (radius - r1)


def sections_by_table(turn, radius, even):
    """The share of the table's full circle that this rounding turns through. Rounded to an EVEN
    number by default, which is the table's own last column: 34 / 4 = 8.5 gives 8, 36 / 4 = 9 gives
    10, 42 / 4 = 10.5 gives 10 - nearest even, halves going up - and every one of its nine rows
    comes out as written. Even also puts a section exactly at the middle of the rounding."""
    # The turn is taken to the whole degree. Roundings are drawn at round angles and a turn read off
    # real chords is out by a fraction of one, and several rows of the table sit exactly on a
    # rounding boundary (5 m: 44 / 4 = 11, even 12): a quarter turn read as 89.6 dropped that row
    # to 10. A percent of slack fixed that and was wrong the other way - 90.9 degrees at 8.5 m is
    # 12.89, which the slack took to 13 and then to 14 where the table means 12.
    turn = math.radians(round(math.degrees(turn)))
    x = segments_per_circle(radius) * turn / (2.0 * math.pi) + 1e-6
    if even:
        return max(2, int(math.floor(x / 2.0 + 0.5)) * 2)
    return max(1, int(math.floor(x + 0.5)))


def sections_long(turn, length, step, even):
    """Past the table's last row a rounding is a road's bend, not a corner, and carrying the table
    on (+2 a metre) gave a 50 m bend 22 sections. There the count is by a step along the arc, but
    never coarser in angle than the table's 5 m row."""
    per = CIRCLE_TABLE[-1][1]
    x = max(per * math.radians(round(math.degrees(turn))) / (2.0 * math.pi), length / max(step, 0.01)) + 1e-6
    if even:
        return max(2, int(math.floor(x / 2.0 + 0.5)) * 2)
    return max(1, int(math.floor(x + 0.5)))


def rounding_faces(bm):
    """The selected faces that can be part of a rounding. An n-gon selected with it is usually the
    plot or the island it runs round - left in, it hides the ends of the band; left out, it is
    neighbouring geometry and the new points are welded into its edge. But a face of the band
    itself can be an n-gon too: a kerb whose outer edge carries one extra point (Dima's 8321) is a
    pentagon. The two are told apart by how they touch the rest: a face of the band meets its
    neighbours across two OPPOSITE edges, the plot meets them along a run of edges in a row."""
    sel = {f for f in bm.faces if f.select}
    out = {f for f in sel if len(f.verts) <= 4}
    grew = True
    while grew:
        grew = False
        for f in sel - out:
            shared = [e for e in f.edges if any(g is not f and g in out for g in e.link_faces)]
            if len(shared) == 2 and not set(shared[0].verts) & set(shared[1].verts):
                out.add(f)
                grew = True
    return out


def _extras(chains, first_ring, region):
    """Per ring, the points on it that no section runs through - an extra point on a kerb's edge,
    welded to whatever lies beyond. Such a point is kept exactly where it is and the new face it
    falls in takes it as a fifth corner; letting it take part in the sections would drag the
    geometry outside along with it."""
    edges = {e for f in region for e in f.edges}
    out = []
    for ci in range(first_ring, len(chains)):
        near = set()
        if ci > 0:
            near |= set(chains[ci - 1])
        if ci + 1 < len(chains):
            near |= set(chains[ci + 1])
        ring = chains[ci]
        out.append({v for v in ring[1:-1]
                    if not any(e in edges and e.other_vert(v) in near for e in v.link_edges)})
    return out


def _expand(inner, ring, region):
    """The inner side of a band laid out segment for segment against the ring next to it: the
    inner vertex each ring vertex is joined to, through the face between them. Where the faces are
    a fan the same hub comes back again and again; where a quad carries the side on (a rounding
    that runs into a straight, its last piece a quad from the hub) it moves on. None if the two do
    not pair up that way."""
    inner_set = set(inner)
    out = []
    for k in range(len(ring) - 1):
        f = next((g for g in ring[k].link_faces
                  if g in region and ring[k + 1] in g.verts and any(v in inner_set for v in g.verts)), None)
        if f is None:
            return None

        def towards(rv):
            for e in rv.link_edges:
                if f in e.link_faces:
                    w = e.other_vert(rv)
                    if w in inner_set:
                        return w
            return None

        p, q = towards(ring[k]), towards(ring[k + 1])
        if p is None or q is None or (out and out[-1] is not p):
            return None
        if not out:
            out.append(p)
        out.append(q)
    return out


def _layer_beyond(region, chain):
    """The strip of faces just outside one edge chain of the selection - one face across each chain
    edge, plus any face touching the chain at a point between them - or None if what lies there is
    not one (the plot as one n-gon, a face across two chain edges)."""
    layer = []
    on_chain = set()
    for a, b in zip(chain, chain[1:]):
        e = next((g for g in a.link_edges if g.other_vert(a) is b), None)
        if e is None:
            return None
        on_chain.add(e)
        outs = [f for f in e.link_faces if f not in region]
        if len(outs) != 1 or len(outs[0].verts) > 4:
            return None
        layer.append(outs[0])
    if len(set(layer)) != len(layer):
        return None
    for f in layer:
        if sum(1 for e in f.edges if e in on_chain) != 1:
            return None
    out = set(layer)
    # A strip beyond may also hold triangles that touch the chain by one point only - the footway
    # beside Dima's kerb has three, their base on the far side. They belong to the strip too; whether
    # the whole is a clean strip is then decided by reading it (find_rings in grow_across).
    for v in chain[1:-1]:
        for f in v.link_faces:
            if f not in region and f not in out:
                if len(f.verts) > 4:
                    return None
                out.add(f)
    return out


def grow_across(region, layers=12):
    """The selection grown across the strips that run alongside it - the footway beside a kerb, the
    kerb on its far side - for as long as each next one is a clean strip along the whole rounding.
    A kerb resampled on its own leaves the footway beside it with the old sections and every new
    point welded into its edge (Dima, on one kerb picked alone: "линии не красиво
    распределяются")."""
    taken = 0
    for _ in range(layers):
        try:
            inner, rings = find_rings(region)
        except _Fail:
            break
        sides = [rings[-1]] + ([] if inner else [rings[0]])
        grew = False
        for chain in sides:
            layer = _layer_beyond(region, chain)
            if not layer:
                continue
            trial = region | layer
            try:
                _, more = find_rings(trial)
            except _Fail:
                continue
            if len(more) > len(rings):
                region = trial
                taken += 1
                grew = True
                break
        if not grew:
            break
    return region, taken


def find_rings(region):
    """(inner, rings) of a selected rounding. inner is the hub side laid out vertex for vertex
    against the first ring - the hub repeated along a fan, moving on where a straight carries it -
    or None for a plain band; rings run outwards from it."""
    inner, rings, err = _fan_chains(region)
    if inner is not None:
        return inner, rings
    # No clean fan: read it as a strip, whose shorter side may still be a fan with a straight on it
    band, band_err = _chains_from_region(region)
    if band is None or len(band) < 2:
        raise _Fail(err or band_err or "Выделите грани скругления")
    if len(band) >= 3 and len(band[0]) < len(band[1]):
        inner = _expand(band[0], band[1], region)
        if inner:
            return inner, band[1:]
    if len(band) >= 3 and len(band[-1]) < len(band[-2]):
        inner = _expand(band[-1], band[-2], region)
        if inner:
            return inner, band[-2::-1]
    return None, band


def _inner_targets(inner, plan):
    """The new inner side: the hub stays one point along every arc, and the side may leave it only
    at the very ends of a piece - a straight run on from the hub, or a fan whose first or last
    face is a quad (Dima's corner: five triangles round hub 8642, then a quad on to 8386, all six
    turning the same 15 degrees). Those end points are kept where they are and the new sections in
    between all meet the hub. A side that moves along the middle of an arc is a ring, not a hub."""
    out = [inner[0]]
    for first, last, straight, k in plan:
        seg = inner[first:last + 2]
        if all(v is seg[0] for v in seg):
            out.extend([seg[0]] * k)
            continue
        middle = seg[1:-1]
        hub = middle[0] if middle else None
        if hub is not None and all(v is hub for v in middle):
            # seg[0] is already in out; the k - 1 new sections inside meet the hub, the end is kept
            out.extend([hub] * (k - 1) + [seg[-1]])
        elif not middle and k == 1:
            out.append(seg[-1])
        else:
            raise _Fail("Внутренняя сторона скругления идёт по дуге, а не из одной точки: "
                        "выделите скругление без внутреннего края или с веером от центра")
    return out


# How many faces past each end of the selection a bigger radius may take in, one strip-width at a time
MAX_TAKEN = 40


class _TooBig(_Fail):
    """A radius whose arc would start past the end of the selection, and at which end(s)."""

    def __init__(self, message, ends, largest):
        super().__init__(message)
        self.ends = ends
        self.largest = largest


def _continuation(region, chains, ends):
    """The faces just past the selection's ends that carry its strips on - one per strip, across
    the end line - or None if any end that needs one has nothing there to take (the kerb turns
    away, the mesh ends, a strip has no face beyond). Only the end that ran out grows: the far one
    is left as it is, or its straight would be merged into one long face for no reason."""
    out = set()
    for end in ends:
        idx = 0 if end == "a" else -1
        for c0, c1 in zip(chains, chains[1:]):
            a, b = c0[idx], c1[idx]
            e = next((g for g in a.link_edges if g.other_vert(a) is b), None)
            if e is None:
                return None
            beyond = [f for f in e.link_faces if f not in region]
            if len(beyond) != 1 or len(beyond[0].verts) > 4:
                return None
            out.add(beyond[0])
    return out or None


class FilletSectionsCore(StripResampleCore):
    """The strip machinery of Strip Resample (welding to the neighbours, UV, colours), with a hub side."""

    summary = (0, 0, False, 0, [], 0, 0)
    radius = 0.0  # the operator and the tab give their own; 0 keeps the rounding's radius
    long_step = 4.0
    take_band = False

    def _rebuild_with_extras(self, bm, ring, old_pts, targets, extras, chains, moved=None):
        """The ring rebuilt on the section targets, with its extra points kept where they stand.
        Each extra goes in between the two sections it lies between (or IS the section, if one
        lands on it), and the ring is rebuilt piece by piece between them, so an extra is always
        the END of a piece - the one thing Strip Resample's rebuild never moves.
        Returns (new chain, index in it of every section)."""
        if not extras:
            return self._rebuild_chain(bm, ring, old_pts, targets, chains), list(range(len(targets)))
        merged = list(targets)
        sec = list(range(len(targets)))
        cuts = []
        for idx in range(1, len(ring) - 1):
            v = ring[idx]
            if v not in extras:
                continue
            co = moved[idx] if moved and idx in moved else old_pts[idx]
            best = min(range(len(merged) - 1),
                       key=lambda j: _point_segment(co, merged[j], merged[j + 1]))
            near = min((best, best + 1), key=lambda j: (merged[j] - co).length)
            if (merged[near] - co).length < 0.01 and 0 < near < len(merged) - 1:
                merged[near] = co.copy()  # a section already falls on it: that section is it
                cuts.append((idx, near))
            else:
                merged.insert(best + 1, co.copy())
                sec = [j if j <= best else j + 1 for j in sec]
                cuts.append((idx, best + 1))
        cuts.sort(key=lambda c: c[1])
        bounds = [(0, 0)] + cuts + [(len(ring) - 1, len(merged) - 1)]
        chain = []
        for (i0, j0), (i1, j1) in zip(bounds, bounds[1:]):
            part = self._rebuild_chain(bm, ring[i0:i1 + 1], old_pts[i0:i1 + 1], merged[j0:j1 + 1], chains)
            chain.extend(part if not chain else part[1:])
        return chain, sec

    def _plan(self, rings, mw):
        """(old, plan): plan is [(first, last, straight, sections)] over the segments of a ring.
        A straight gets one section and nothing else - only its ends, where the arcs meet it."""
        old = max(len(r) - 1 for r in rings)
        n = len(rings[0]) - 1
        pieces = _pieces(rings, mw, self.keep_straight) or [(0, n - 1, False)]
        arcs = [i for i, pc in enumerate(pieces) if not pc[2]]
        measured = {}
        for i in arcs:
            if len(pieces) == 1:
                measured[i] = _measure(rings, mw)
            else:
                measured[i] = _arc_of(rings, mw, pieces[i][0], pieces[i][1])
        counts = {i: 1 for i in range(len(pieces))}
        if self.mode == 'COUNT':
            if self.count == 0:
                # Show the real current number in the F9 panel, so it can be dragged up or down
                self.count = old
            straights = len(pieces) - len(arcs)
            for i, k in zip(arcs, _share(max(len(arcs), self.count - straights),
                                         [measured[i][0] for i in arcs])):
                counts[i] = k
        elif self.mode == 'ANGLE':
            for i in arcs:
                counts[i] = max(1, round(self.seg_90 * math.degrees(measured[i][0]) / 90.0))
        else:
            for i in arcs:
                counts[i] = self._table_count(measured[i][0], measured[i][1])
        plan = [(pc[0], pc[1], pc[2], counts[i]) for i, pc in enumerate(pieces)]
        found = [(measured[i][0], measured[i][1], counts[i]) for i in arcs]
        if self.mode == 'TABLE':
            last_measure.clear()
            last_measure.update(arcs=found, straights=len(pieces) - len(arcs))
        return old, plan, found

    def _capture_strips(self, bm, chains):
        """Strip Resample's capture, with one difference: a face belongs to a strip if it touches
        both of its chains and no other chain - it may carry a point that is on NEITHER. Strip
        Resample wants every point on the two chains, and the last face of Dima's footway, which
        has one more point on the selection's end line, was left out: never deleted, the new faces
        laid beside it, and a gap where the two met."""
        region = getattr(self, "_region", None)
        sets = [set(c) for c in chains]
        on_any = set().union(*sets)
        uv_layers = list(bm.loops.layers.uv.values())
        col_layers = list(bm.loops.layers.color.values()) + list(bm.loops.layers.float_color.values())
        pool = region if region is not None else bm.faces
        strips = []
        for i in range(len(chains) - 1):
            pair = sets[i] | sets[i + 1]
            faces = [f for f in pool
                     if all(v in pair or v not in on_any for v in f.verts)
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

    def _table_count(self, turn, radius):
        if radius > CIRCLE_TABLE[-1][0]:
            return sections_long(turn, turn * radius, self.long_step, self.even)
        return sections_by_table(turn, radius, self.even)

    def _reshaped(self, rings, mw):
        """(old, plan, found, targets) for a rounding re-laid at self.radius on the LONGEST ring
        (the one whose radius the table and the panel show), every ring moved by the same amount."""
        old = max(len(r) - 1 for r in rings)
        world = [[mw @ v.co for v in r] for r in rings]
        frames = [fillet_radius.frame(p) for p in world]
        if any(f is None for f in frames):
            raise _Fail("Новый радиус — для одного скругления между двумя прямыми. У разворота или "
                        "почти прямой полосы угла нет: выделите каждую дугу отдельно")
        n = len(rings[0]) - 1
        pieces = _pieces(rings, mw, True) or [(0, n - 1, False)]
        if sum(1 for pc in pieces if not pc[2]) > 1:
            raise _Fail("Здесь две дуги с прямой между ними: новый радиус задаётся каждой дуге отдельно")
        straight = [False] * n
        for first, last, flat in pieces:
            for j in range(first, last + 1):
                straight[j] = flat
        radii = [fillet_radius.arc_radius(p, straight) for p in world]
        ref = max(range(len(world)), key=lambda i: sum((b - a).length for a, b in zip(world[i], world[i][1:])))
        layouts, err, largest, short = fillet_radius.reshape(frames, radii, ref, self.radius)
        if short:
            raise _TooBig(err, short, largest)
        if err:
            raise _Fail(err)
        lay = layouts[ref]
        turn = abs(lay["theta"])
        straights = []
        if lay["straight_a"] > fillet_radius.MIN_STRAIGHT:
            straights.append("a")
        if lay["straight_b"] > fillet_radius.MIN_STRAIGHT:
            straights.append("b")
        if self.mode == 'COUNT':
            if self.count == 0:
                self.count = old
            k = max(1, self.count - len(straights))
        elif self.mode == 'ANGLE':
            k = max(1, round(self.seg_90 * math.degrees(turn) / 90.0))
        else:
            k = self._table_count(turn, self.radius)
        parts = ([("a", 1)] if "a" in straights else []) + [("arc", k)] + ([("b", 1)] if "b" in straights else [])
        plan = []
        at = 0
        for kind, kk in parts:
            plan.append((at, at, kind != "arc", kk))
            at += 1
        mwi = mw.inverted()
        targets = []
        for layout, p in zip(layouts, world):
            xy = fillet_radius.lay_out(layout, parts)
            zs = fillet_radius.heights(p, xy)
            targets.append([mwi @ Vector((q.x, q.y, z)) for q, z in zip(xy, zs)])
        found = [(turn, self.radius, k, radii[ref])]
        if self.mode == 'TABLE':
            last_measure.clear()
            last_measure.update(arcs=found, straights=len(straights))
        return old, plan, found, targets

    def _run(self, bm, obj):
        region = rounding_faces(bm)
        if not region:
            raise _Fail("Выделите грани скругления: веер от центра и кольца вокруг него")
        self.taken = 0
        if self.take_band:
            region, self.taken = grow_across(region)

        mw = obj.matrix_world
        taken = 0
        last = None
        while True:
            inner, rings_full = find_rings(region)
            chains_full = ([inner] if inner else []) + rings_full
            extras = _extras(chains_full, 1 if inner else 0, region)
            rings = [[v for v in r if v not in ex] for r, ex in zip(rings_full, extras)]
            if inner and extras[0]:
                raise _Fail("Лишняя точка на первом кольце веера: так пока не умею, уберите её или выделите без веера")
            try:
                reshaped = self._reshaped(rings, mw) if self.radius > 0.0 else None
                break
            except _TooBig as ex:
                # A bigger radius starts the arc further back along the straights: take in the next
                # face of every strip at the end that runs out, and try again
                more = _continuation(region, chains_full, ex.ends)
                if more is None or taken >= MAX_TAKEN:
                    raise _Fail(f"{ex} — дальше за концом скругления нет прямой, на которую можно лечь")
                region = region | more
                taken += 1
                last = ex
            except _Fail:
                if last is None:
                    raise
                # Taking in more ran into the next bend: the straight ends there
                raise _Fail(f"Радиус {self.radius:.2f} м не помещается: больше {last.largest:.2f} м нельзя — "
                            f"прямая за скруглением кончается, дальше следующий поворот")
        if reshaped:
            old, plan, found, ring_targets = reshaped
        else:
            old, plan, found = self._plan(rings, mw)
            ring_targets = None
        count = sum(pc[3] for pc in plan)

        # A point welded to geometry outside the selection cannot be taken out without tearing that
        # geometry, so every ring keeps at least those. Said up front, with the number, rather than
        # as Strip Resample's generic failure halfway through.
        least = 1 + max(_welded(r, region) for r in rings)
        if count < least:
            raise _Fail(f"Меньше {least} сечений нельзя: {least - 1} точек кольца сварены с невыделенной "
                        f"геометрией. Выделите и её или оставьте не меньше {least}")

        pts = [[v.co.copy() for v in r] for r in rings]
        full_pts = [[v.co.copy() for v in r] for r in rings_full]
        if not inner:
            new_inner = None
        elif reshaped:
            new_inner = _inner_reshaped(inner, count)
        else:
            new_inner = _inner_targets(inner, plan)
        chains = ([inner] if inner else []) + rings
        self._region = region
        strips = self._capture_strips(bm, chains_full)
        old_uv = _capture_faces(chains, strips)
        old_pts = ([[v.co.copy() for v in inner]] if inner else []) + pts
        end_points = _end_lines(chains_full, strips)
        all_band = {f for s in strips for f in s["faces"]}
        chain_edge_set = {bm.edges.get((a, b)) for r in chains_full for a, b in zip(r, r[1:]) if a is not b}
        band_edges = {e for f in all_band for e in f.edges} - chain_edge_set
        bmesh.ops.delete(bm, geom=list(all_band), context='FACES_ONLY')
        for e in band_edges:
            if e.is_valid and not e.link_faces:
                bm.edges.remove(e)

        # Every ring is walked at equal steps of its OWN length. On concentric arcs that is the same
        # angle on every ring, so the new sections run straight out from the centre, and a ring's
        # ends stay where they are - they are the seams with the straight runs either side.
        # Each arc is smoothed on its own points only, so a straight beside it is not bent into it.
        # Every arc piece laid out on all rings at once, at the same angles from its centre, so the
        # sections run straight across the band (fillet_align); a piece it cannot do keeps the
        # per-ring share of length.
        aligned = {}
        if ring_targets is None:
            mwi = mw.inverted()
            longest = max(range(len(rings)), key=lambda i: sum((b - a).length for a, b in zip(pts[i], pts[i][1:])))
            for pi, (first, last, straight, k) in enumerate(plan):
                if straight or k < 2:
                    continue
                denses = [_dense_curve([mw @ q for q in p[first:last + 2]], self.smooth, set()) for p in pts]
                ref_t = _sample(denses[longest], k)
                got = fillet_align.align(denses, ref_t, longest)
                if got:
                    aligned[pi] = [[mwi @ q for q in ring_pts] for ring_pts in got]

        new_chains = [new_inner] if inner else []
        sections = [list(range(count + 1))] if inner else []
        for ri, (ring, ring_full, p, pf, ex) in enumerate(zip(rings, rings_full, pts, full_pts, extras)):
            moved = None
            if ring_targets is not None:
                targets = ring_targets[ri]
                # A new radius moves the edge, and a point welded to it moves along: onto the new line
                moved = {idx: _nearest_on(targets, pf[idx]) for idx in range(1, len(ring_full) - 1)
                         if ring_full[idx] in ex}
            else:
                targets = [p[0].copy()]
                for pi, (first, last, straight, k) in enumerate(plan):
                    part = p[first:last + 2]
                    if straight:
                        targets.append(part[-1].copy())
                    elif pi in aligned:
                        targets.extend(aligned[pi][ri][1:])
                    else:
                        targets.extend(_sample(_dense_curve(part, self.smooth, set()), k)[1:])
            chain, sec = self._rebuild_with_extras(bm, ring_full, pf, targets, ex, chains_full, moved)
            new_chains.append(chain)
            sections.append(sec)

        # Where each new point sits on its old chain. While the outline stays put the nearest point
        # of the old edge says it exactly. A new radius moves the edge, up to metres: the nearest
        # point then is the wrong one - several new points caught on one old one, the texture
        # squeezed to nothing and faces turned over (measured at 12 m: density 0, 8 of 64 flipped).
        # So a moved band is read along its middle line in metres, from each end (_band_readings).
        band = None
        if reshaped:
            first = 1 if inner else 0
            band = _band_readings(new_chains[first:], sections[first:], old_pts[first:], count)

        faces = []
        for i, s in enumerate(strips):
            A, sa = new_chains[i], sections[i]
            B, sb = new_chains[i + 1], sections[i + 1]
            for k in range(count):
                a_side = A[sa[k]:sa[k + 1] + 1]
                b_side = B[sb[k]:sb[k + 1] + 1]
                start_line, end_line = end_points[i]
                # the points on the selection's two end lines go into the end faces, in order
                tail = [v for v, _ in reversed(start_line)] if k == 0 else []
                mid = [v for v, _ in end_line] if k == count - 1 else []
                poly = list(dict.fromkeys(a_side + mid + b_side[::-1] + tail))
                if len(poly) < 3 or bm.faces.get(poly):
                    continue
                f = bm.faces.new(poly)
                f.normal_update()
                if s["normal"].length > 0 and f.normal.dot(s["normal"]) < 0:
                    f.normal_flip()
                f.material_index = s["material"]
                f.smooth = s["smooth"]
                faces.append(f)
                if old_uv[i] is not None:
                    _map_loops(f, old_uv[i], old_pts[i], old_pts[i + 1], s, a_side, b_side,
                               (band[0], band[1][k]) if band else None)
                else:
                    self._interpolate_loops(bm, f, s)
                fixed = dict(start_line + end_line)
                for l in f.loops:
                    if l.vert in fixed:
                        uvs, cols = fixed[l.vert]
                        for li, lay in enumerate(s["uv_layers"]):
                            l[lay].uv = uvs[li]
                        for li, lay in enumerate(s["col_layers"]):
                            l[lay] = cols[li]

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
        self.summary = (old, count, inner is not None, len(rings), found, sum(1 for pc in plan if pc[2]),
                        self.taken)
        return faces


def _end_lines(chains, strips):
    """Per strip, the points on the selection's two end lines between its two chains - ones that lie
    on neither, with their old UV and colours - as (start line, end line), each ordered from the
    inner chain to the outer.

    The trap: Dima's footway ends in a step, and between the ends of two rings its end line carries
    one more point, welded to the straight beyond. The new end face joined the two ring ends
    directly, past that point, and left a triangle of daylight between it and the straight ("видишь,
    последняя линия не соединилась")."""
    out = []
    for i, s in enumerate(strips):
        A, B = chains[i], chains[i + 1]
        on_chains = set(A) | set(B)
        lines = []
        for a, b in ((A[0], B[0]), (A[-1], B[-1])):
            found = []
            face = next((f for f in s["faces"] if a in f.verts and b in f.verts), None)
            if face is not None and a is not b:
                loop = list(face.loops)
                verts = [l.vert for l in loop]
                ia, ib = verts.index(a), verts.index(b)
                n = len(verts)
                fwd = [verts[(ia + d) % n] for d in range(1, (ib - ia) % n)]
                back = [verts[(ia - d) % n] for d in range(1, (ia - ib) % n)]
                path = fwd if not any(v in on_chains for v in fwd) else (
                    back if not any(v in on_chains for v in back) else [])
                for v in path:
                    l = loop[verts.index(v)]
                    found.append((v, ([l[lay].uv.copy() for lay in s["uv_layers"]],
                                      [Vector(l[lay]) for lay in s["col_layers"]])))
            lines.append(found)
        out.append(tuple(lines))
    return out


def _capture_faces(chains, strips):
    """Per strip, the old faces in order along it, each as its corners' UVs and colours by ROLE
    (a0, a1 on the inner chain - one and the same vertex where it is a fan's hub - b0, b1 on the
    outer) - the vertices themselves are
    about to be moved, collapsed or split. None for a strip that is not one face per segment (a
    band with uneven sides), which keeps Strip Resample's nearest-triangle transfer."""
    out = []
    for i, s in enumerate(strips):
        A, B = chains[i], chains[i + 1]
        if len(A) != len(B):
            out.append(None)
            continue
        faces = []
        for k in range(len(B) - 1):
            key = {A[k], A[k + 1], B[k], B[k + 1]}
            f = next((g for g in s["faces"] if key <= set(g.verts)), None)
            if f is None:
                faces = None
                break
            at = {l.vert: ([l[lay].uv.copy() for lay in s["uv_layers"]],
                           [Vector(l[lay]) for lay in s["col_layers"]]) for l in f.loops}
            data = {"a0": at[A[k]], "a1": at[A[k + 1]], "b0": at[B[k]], "b1": at[B[k + 1]]}
            faces.append(data)
        out.append({"faces": faces} if faces else None)
    return out


def _nearest_on(pts, co):
    """The point of a polyline nearest co."""
    best = None
    for a, b in zip(pts, pts[1:]):
        d = b - a
        l2 = d.length_squared
        t = 0.0 if l2 < 1e-12 else max(0.0, min(1.0, (co - a).dot(d) / l2))
        q = a + d * t
        if best is None or (q - co).length < best[0]:
            best = ((q - co).length, q)
    return best[1].copy()


def _inner_reshaped(inner, count):
    """The hub side for a rounding laid out afresh: its two ends stay, every section between meets
    the hub. A side that moves along the middle is a ring, not a hub."""
    middle = inner[1:-1]
    hub = middle[0] if middle else inner[0]
    if any(v is not hub for v in middle):
        raise _Fail("Внутренняя сторона скругления идёт по дуге, а не из одной точки: радиус так не поменять")
    return [inner[0]] + [hub] * (count - 1) + [inner[-1]]


def _point_segment(p, a, b):
    d = b - a
    l2 = d.length_squared
    t = 0.0 if l2 < 1e-12 else max(0.0, min(1.0, (p - a).dot(d) / l2))
    return (a + d * t - p).length


def _along(pts):
    acc = [0.0]
    for a, b in zip(pts, pts[1:]):
        acc.append(acc[-1] + (b - a).length)
    return acc


def _project(pts, acc, co, prefer=None):
    """(segment, fraction, station) of the point of an old chain nearest co. A point lying on more
    than one segment - a joint, or a hub that is every segment of a fan - goes to `prefer` if
    that is one of them, so a corner is read off the reference face whenever it can be."""
    best = None
    for k in range(len(pts) - 1):
        a, d = pts[k], pts[k + 1] - pts[k]
        l2 = d.length_squared
        f = 0.0 if l2 < 1e-12 else max(0.0, min(1.0, (co - a).dot(d) / l2))
        dist = (a + d * f - co).length_squared
        if best is None or dist < best[0] - 1e-12 or (k == prefer and dist <= best[0] + 1e-12):
            best = (dist, k, f)
    _, k, f = best
    return k, f, acc[k] + f * (acc[k + 1] - acc[k])


def _reading(acc, st):
    """(segment, fraction, station) for a distance along an old chain, carried on past either end:
    before the start it reads the first segment backwards (fraction below 0), past the end the
    last one onwards (above 1) - that is the texture continuing, not stopping."""
    spans = [k for k in range(len(acc) - 1) if acc[k + 1] - acc[k] > 1e-9]
    if not spans:
        return 0, 0.0, st
    k = spans[0]
    for j in spans:
        if acc[j] <= st + 1e-9:
            k = j
    return k, (st - acc[k]) / (acc[k + 1] - acc[k]), st


def _band_readings(chains, sections, old_pts, count):
    """Where each new ring point reads its texture after a new radius has moved the edge: (old
    middle-line stations, and per new face a vertex -> (old face, fraction, station) map).

    Three things the texture needs, each learned from a broken result:
    - CONTINUE, never stretch (Dima: "не нужно растягивать её, а просто продолжить"): read in
      metres. Read by share of length, a ring that got longer stretched its texture 16 per cent.
    - ONE reading per SECTION. Read in each ring's own metres, the outer edge of an arc - longer
      than the inner one - ran ahead section by section and the stone joints leant over ("тянут
      текстуру"): 13-19 cm of pattern across one face. A section is placed by the middle line of the
      whole band (the mean of its rings) and every corner of the section takes the same old face and
      fraction, so a joint across the kerb stays across it.
    - for the WHOLE band, not per strip: a middle line per strip gave a ring shared by two strips two
      readings, a seam along it in any texture laid over both (32 on a flat-projected fixture).
    The first half of the sections reads from the start, the second from the far end, so both ends
    meet their neighbours exactly; the one joint is at the middle section."""
    def acc(points):
        out = [0.0]
        for p, q in zip(points, points[1:]):
            out.append(out[-1] + (q - p).length)
        return out

    old_accs = [acc(p) for p in old_pts]
    old_mid = [sum(a[j] for a in old_accs) / len(old_accs) for j in range(len(old_accs[0]))]
    new_accs = [acc([v.co for v in c]) for c in chains]
    mid = [sum(d[sec[k]] for d, sec in zip(new_accs, sections)) / len(chains) for k in range(count + 1)]
    total_new, total_old = mid[-1], old_mid[-1]

    def at(t, half):
        return _reading(old_mid, t if half == 0 else total_old - (total_new - t))

    faces = []
    for k in range(count):
        half = 0 if k < count // 2 else 1
        out = {}
        for chain, d, sec in zip(chains, new_accs, sections):
            i0, i1 = sec[k], sec[k + 1]
            span = d[i1] - d[i0]
            for idx in range(i0, i1 + 1):
                u = (d[idx] - d[i0]) / span if span > 1e-9 else (0.0 if idx == i0 else 1.0)
                out[chain[idx]] = at(mid[k] + (mid[k + 1] - mid[k]) * u, half)
        faces.append(out)
    return old_mid, faces


def _map_loops(face, data, pts_a, pts_b, strip, a_side, b_side, reads=None):
    """
    UV and colours of a new face, read off the old faces of its strip by where each corner lies
    ALONG the strip, never by which old triangle is nearest in space.

    The trap the nearest-triangle transfer fell into: a kerb or a paving band is often unwrapped
    a face at a time (each stone its own island), and a new face over the joint of two old ones
    took two corners from one island and two from the other - torn, stretched, even turned over.
    So one old face is the REFERENCE, the one under the new face's middle. A corner falling on
    another old face takes that face's UV only when the unwrap runs on across every joint
    between them; across a seam it carries on in the reference face's own island instead.

    A corner's UV is read ALONG its old edge, never extrapolated across the face. Tried and taken
    out: extending the face's map in its own plane puts a flat-projected texture exactly in place,
    but a new point on the true arc stands outside the old chord by the chord's sag, and across a
    30 cm kerb that pushed the UV 0.39 of a tile past the strip's edge - into the next picture of
    an atlas. Read along the edge, the across coordinate is the old edge's own, so an atlas strip
    stays inside itself and both rings either side of an edge get the same value. What it costs:
    a texture projected flat from above sits up to that sag off (10 cm on a 9 degree step at 8 m).
    """
    olds = data["faces"]
    acc_b = _along(pts_b)
    acc_a = _along(pts_a)
    if reads is not None:
        acc_mid, known = reads
        st = (known[b_side[0]][2] + known[b_side[-1]][2]) * 0.5
        ref = _reading(acc_mid, st)[0]
    else:
        mid = (b_side[0].co + b_side[-1].co) * 0.5
        ref = _project(pts_b, acc_b, mid)[0]

    def joined(c0, c1, kind, li):
        # The unwrap runs on from old face c to c + 1 when both sides of the joint agree
        lo, hi = (c0, c1) if c0 < c1 else (c1, c0)
        for c in range(lo, hi):
            f0, f1 = olds[c], olds[c + 1]
            for r0, r1 in (("b1", "b0"), ("a1", "a0")):
                v0, v1 = f0[r0][kind][li], f1[r1][kind][li]
                if (v0 - v1).length > 1e-4:
                    return False
        return True

    def value(side, vert, kind, li):
        pts, acc = (pts_a, acc_a) if side == "a" else (pts_b, acc_b)
        if reads is not None and vert in reads[1]:
            c, f, st = reads[1][vert]
            acc = reads[0]  # stations are on the middle line
        else:
            c, f, st = _project(pts, acc, vert.co, prefer=ref)
        if c != ref and not joined(ref, c, kind, li):
            # carry on in the reference face's island, measured along the chain
            span = acc[ref + 1] - acc[ref]
            c, f = ref, ((st - acc[ref]) / span if span > 1e-9 else 0.0)
        v0, v1 = olds[c][side + "0"][kind][li], olds[c][side + "1"][kind][li]
        return v0.lerp(v1, f)

    side_of = {v: "a" for v in a_side}
    side_of.update({v: "b" for v in b_side})
    for l in face.loops:
        side = side_of.get(l.vert)
        if side is None:
            continue
        for li, lay in enumerate(strip["uv_layers"]):
            l[lay].uv = value(side, l.vert, 0, li)
        for li, lay in enumerate(strip["col_layers"]):
            l[lay] = value(side, l.vert, 1, li)


def describe(summary):
    old, count, fan, rings, arcs, straights = summary[:6]
    taken = summary[6] if len(summary) > 6 else 0
    what = f"веер и колец: {rings}" if fan else f"колец: {rings}"
    parts = ", ".join(f"{math.degrees(a[0]):.0f}° R " + (f"{a[3]:.2f} → " if len(a) > 3 else "")
                      + f"{a[1]:.2f} м → {a[2]}" for a in arcs)
    if straights:
        parts += f"; прямых без сечений: {straights}"
    if taken:
        parts += f"; захвачено соседних полос: {taken}"
    return f"Сечений {old} → {count} ({what}; {parts})"


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
            ('TABLE', "По радиусу",
             "По таблице студии: радиус скругления даёт число сегментов на полный круг, угол — какую его "
             "долю взять. 2.5 м и поворот 90° — 10 сечений"),
        ])
    count: IntProperty(
        name="Сечений", default=0, min=0, max=1000, options={'SKIP_SAVE'},
        description="Сколько шагов вдоль скругления. Больше — плавнее, меньше — легче. "
                    "При запуске подставляется текущее количество")
    seg_90: IntProperty(
        name="Сечений на 90°", default=8, min=1, max=256,
        description="Сколько шагов на четверть круга; скругление на 45° получит половину")
    radius: FloatProperty(
        name="Радиус", default=0.0, min=0.0, soft_max=50.0, unit='LENGTH', precision=2,
        description="Новый радиус скругления (по внешнему кольцу, как в таблице); 0 — оставить как есть. "
                    "Дуга строится заново касательной к тем же прямым, кольца сдвигаются вместе, ширина "
                    "бордюра не меняется. Больший радиус сам берёт грани прямых за концами выделения; "
                    "если прямая кончается, инструмент скажет, какой максимум")
    long_step: FloatProperty(
        name="Шаг на длинных дугах", default=4.0, min=0.2, soft_max=20.0, unit='LENGTH', precision=1,
        description="Таблица кончается на радиусе 5 м. Больше — это изгиб дороги, а не угол: сечения "
                    "ставятся через этот шаг вдоль дуги (но не реже, чем на радиусе 5 м по углу)")
    take_band: BoolProperty(
        name="Захватывать соседние полосы", default=True,
        description="Если выделен только бордюр, взять и полосы рядом, которые идут вдоль скругления "
                    "(тротуар, второй бордюр), чтобы сечения прошли через них ровно. Останавливается "
                    "на площадке, траве из треугольников и любой неровной сетке")
    keep_straight: BoolProperty(
        name="Прямые без сечений", default=True,
        description="Прямые участки между дугами (разворот, скругление с прямой вставкой) остаются одним "
                    "отрезком; сечения ставятся только на дуги, каждой — по её радиусу и углу")
    even: BoolProperty(
        name="Чётное", default=True,
        description="Округлять до чётного, как в таблице: сечение встаёт ровно на середину скругления")
    # 0 by default (Dima: "немного смещается"). On the circle through the old points the new ones
    # stood up to 3.5 cm off the old edge on his kerb, and the plot and the road welded to that
    # edge were bent along with it. On the old edges the outline does not move at all.
    smooth: FloatProperty(
        name="Плавность", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        description="0 — новые точки на старых рёбрах: контур скругления и соседние грани не двигаются. "
                    "1 — на гладкой дуге через старые вершины: грубая дуга скругляется, но кромка и всё, "
                    "что к ней приварено, сдвигаются на прогиб")
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


PARAM_KEYS = ("mode", "count", "seg_90", "even", "long_step", "keep_straight", "take_band", "smooth", "radius")


def draw_params(layout, data, prefix, count_text=None):
    col = layout.column()
    col.row().prop(data, prefix + "mode", expand=True)
    mode = getattr(data, prefix + "mode")
    if mode == 'COUNT':
        col.prop(data, prefix + "count", text=count_text) if count_text else col.prop(data, prefix + "count")
    elif mode == 'ANGLE':
        col.prop(data, prefix + "seg_90")
    else:
        col.prop(data, prefix + "even")
        col.prop(data, prefix + "long_step")
        if prefix:
            # Only on the tab: the table the count is read from, with the row this rounding falls on
            _draw_table(col)
    col.prop(data, prefix + "keep_straight")
    col.prop(data, prefix + "take_band")
    col.prop(data, prefix + "smooth")
    col.prop(data, prefix + "radius", text="Новый радиус (0 = как есть)")


def _draw_table(layout):
    arcs = last_measure.get("arcs") or []
    radius = arcs[0][1] if arcs else None
    box = layout.box()
    grid = box.column(align=True)
    grid.scale_y = 0.75
    head = grid.row()
    for text in ("Радиус, м", "На круг", "На 90°"):
        head.label(text=text)
    rows = CIRCLE_TABLE
    near = None
    if radius is not None:
        near = min(range(len(rows)), key=lambda i: min(abs(rows[i][0] - a[1]) for a in arcs))
    for i, (r, n) in enumerate(rows):
        row = grid.row()
        hit = radius is not None and any(
            min(range(len(rows)), key=lambda k: abs(rows[k][0] - a[1])) == i for a in arcs)
        row.active = near is None or hit
        row.label(text=f"{r:g}", icon='RIGHTARROW' if hit else 'BLANK1')
        row.label(text=str(n))
        row.label(text=str(sections_by_table(math.pi / 2.0, r, True)))
    note = box.column(align=True)
    note.scale_y = 0.8
    note.label(text="Больше 5 м — длинная дуга: по шагу вдоль неё")
    for i, (turn, r, k) in enumerate(a[:3] for a in arcs):
        name = "Сейчас" if len(arcs) == 1 else f"Дуга {i + 1}"
        if r > CIRCLE_TABLE[-1][0]:
            note.label(text=f"{name}: R {r:.2f} м — длинная дуга, {math.degrees(turn):.0f}°, "
                            f"{turn * r:.1f} м по шагу → {k}", icon='INFO' if i == 0 else 'BLANK1')
        else:
            note.label(text=f"{name}: R {r:.2f} м → {segments_per_circle(r):.0f} на круг, "
                            f"{math.degrees(turn):.0f}° → {k}", icon='INFO' if i == 0 else 'BLANK1')
    if last_measure.get("straights"):
        note.label(text=f"Прямых без сечений: {last_measure['straights']}", icon='BLANK1')


def _menu(self, context):
    self.layout.operator(MESH_OT_fillet_sections.bl_idname, icon='SPHERECURVE')


_MENUS = (
    bpy.types.VIEW3D_MT_edit_mesh_faces,
    bpy.types.VIEW3D_MT_edit_mesh_context_menu,
)


def register():
    bpy.utils.register_class(MESH_OT_fillet_sections)
    for m in _MENUS:
        m.append(_menu)


def unregister():
    for m in _MENUS:
        m.remove(_menu)
    bpy.utils.unregister_class(MESH_OT_fillet_sections)
