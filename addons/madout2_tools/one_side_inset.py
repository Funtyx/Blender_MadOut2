bl_info = {
    "name": "One Side Inset (Offset Edge Loop)",
    "author": "Claude",
    "version": (1, 8, 0),
    "blender": (4, 0, 0),
    "location": "View3D > N-панель > вкладка One Side; меню Edge / Face / ПКМ",
    "description": "Создаёт параллельную петлю рёбер только с одной стороны выделенной петли "
                   "(в отличие от Offset Edge Slide / Inset, которые режут с двух сторон)",
    "category": "Mesh",
}

import math
import bpy
import bmesh
from bpy.props import FloatProperty, BoolProperty, EnumProperty, FloatVectorProperty, StringProperty, BoolVectorProperty


# ---------------------------------------------------------------- helpers

def build_chains(edges):
    """Упорядочивает выделенные рёбра в цепочки вершин. Возвращает [(verts, closed)]."""
    adj = {}
    for e in edges:
        for v in e.verts:
            adj.setdefault(v, []).append(e)
    for v, es in adj.items():
        if len(es) > 2:
            raise ValueError("В выделении есть развилка (вершина с >2 выделенными рёбрами)")

    used = set()
    chains = []
    # сначала открытые цепочки (начинаем с концов), потом замкнутые
    starts = [v for v, es in adj.items() if len(es) == 1] + list(adj.keys())
    for start in starts:
        free = [e for e in adj[start] if e not in used]
        if not free:
            continue
        verts = [start]
        v = start
        e = free[0]
        closed = False
        while True:
            used.add(e)
            v = e.other_vert(v)
            if v is start:
                closed = True
                break
            verts.append(v)
            nxt = [x for x in adj[v] if x not in used]
            if not nxt:
                break
            e = nxt[0]
        chains.append((verts, closed))
    return chains


def chain_length(verts, closed):
    n = len(verts)
    return sum((verts[(i + 1) % n].co - verts[i].co).length
               for i in range(n if closed else n - 1))


def region_components(region):
    comp = {}
    cid = 0
    for seed in region:
        if seed in comp:
            continue
        stack = [seed]
        comp[seed] = cid
        while stack:
            f = stack.pop()
            for e in f.edges:
                for g in e.link_faces:
                    if g in region and g not in comp:
                        comp[g] = cid
                        stack.append(g)
        cid += 1
    return comp


def face_bfs(src, region):
    dist = {src: 0}
    queue = [src]
    for f in queue:
        for e in f.edges:
            for g in e.link_faces:
                if g in region and g not in dist:
                    dist[g] = dist[f] + 1
                    queue.append(g)
    return dist


def strip_sides(verts, faces):
    """Если выделение — полоса (один контур, два конца), режет контур на две
    боковые стороны, выкидывая торцы. Иначе возвращает None."""
    fs = set(faces)
    d0 = face_bfs(faces[0], fs)
    A = max(d0, key=d0.get)
    dA = face_bfs(A, fs)
    B = max(dA, key=dA.get)
    if A is B:
        return None
    dB = face_bfs(B, fs)

    n = len(verts)
    edges = [edge_between(verts[i], verts[(i + 1) % n]) for i in range(n)]
    efaces = [next(f for f in e.link_faces if f in fs) for e in edges]

    def turn(i):   # угол поворота контура в вершине verts[i], градусы
        a = verts[i].co - verts[i - 1].co
        b = verts[(i + 1) % n].co - verts[i].co
        if a.length < 1e-9 or b.length < 1e-9:
            return 0.0
        return math.degrees(a.angle(b))

    cut = set()
    caps_len = []
    for end, dist_end, other in ((A, dA, B), (B, dB, A)):
        # направление полосы у этого конца: от центра ближайших граней к концу
        near = [f.calc_center_median() for f, d in dist_end.items() if d <= 6]
        cen = sum(near, near[0] * 0) / len(near)
        axis = end.calc_center_median() - cen
        if axis.length < 1e-9:
            axis = end.calc_center_median() - other.calc_center_median()
        axis.normalize()
        cand = [i for i in range(n) if dist_end[efaces[i]] <= 2]
        if not cand:
            return None
        mid = lambda i: (verts[i].co + verts[(i + 1) % n].co) * 0.5
        i0 = max(cand, key=lambda i: (mid(i) - cen).dot(axis))
        cap = {i0}
        acc = 0.0
        j = i0
        while len(cap) < n // 2:                     # вперёд
            t = turn((j + 1) % n)
            if t > 45.0 or acc + t > 120.0:
                break
            acc += t
            j = (j + 1) % n
            cap.add(j)
        j = i0
        while len(cap) < n // 2:                     # назад
            t = turn(j)
            if t > 45.0 or acc + t > 120.0:
                break
            acc += t
            j = (j - 1) % n
            cap.add(j)
        if cap & cut:
            return None
        cut |= cap
        caps_len.append(sum(edges[i].calc_length() for i in cap))

    sides = split_closed_chain(verts, {edges[i] for i in cut}, set())
    if len(sides) != 2:
        return None
    if max(caps_len) > 0.5 * min(chain_length(*c) for c in sides):
        return None          # не полоса, а «пятно» — оставляем весь контур
    return sides


def split_closed_chain(verts, cut_edges, cut_verts):
    """Режет замкнутую цепочку по рёбрам/вершинам торцов на открытые куски."""
    n = len(verts)
    is_cut_edge = [edge_between(verts[i], verts[(i + 1) % n]) in cut_edges for i in range(n)]
    # стартуем сразу после разреза
    start = None
    for i in range(n):
        if is_cut_edge[i]:
            start = (i + 1) % n
            break
        if verts[i] in cut_verts:
            start = i
            break
    if start is None:
        return [(verts, True)]
    out = []
    cur = [verts[start]]
    for k in range(n):
        i = (start + k) % n
        if is_cut_edge[i]:
            if len(cur) > 1:
                out.append((cur, False))
            cur = [verts[(i + 1) % n]]
            continue
        nxt = verts[(i + 1) % n]
        cur.append(nxt)
        if nxt in cut_verts:
            if len(cur) > 1:
                out.append((cur, False))
            cur = [nxt]
    if len(cur) > 1:
        out.append((cur, False))
    return out


def filter_boundaries(chains, region, mode):
    """Выбор края выделенных граней.
    Кольцо (есть дырка): внешний контур / контуры дырок.
    Полоса (один контур, два конца): длинная сторона / короткая сторона."""
    comp = region_components(region)
    groups = {}
    for ch in chains:
        verts, closed = ch
        e = edge_between(verts[0], verts[1])
        f = next(f for f in e.link_faces if f in region)
        groups.setdefault(comp[f], []).append(ch)
    out = []
    for cid, chs in groups.items():
        if len(chs) == 1 and chs[0][1]:
            faces = [f for f, c in comp.items() if c == cid]
            sides = strip_sides(chs[0][0], faces)
            if sides:
                chs = sides
        outer = max(chs, key=lambda c: chain_length(*c))
        if mode == 'OUTER':
            out.append(outer)
        else:
            out.extend(c for c in chs if c is not outer)
    return out


def edge_between(a, b):
    for e in a.link_edges:
        if e.other_vert(a) is b:
            return e
    return None


def left_loop(a, b, e):
    """Петля (BMLoop) грани слева от направленного ребра a->b."""
    for l in e.link_loops:
        if l.vert is a and l.link_loop_next.vert is b:
            return l
    return None


def right_face(a, b, e):
    for l in e.link_loops:
        if l.vert is b and l.link_loop_next.vert is a:
            return l.face
    return None


def walk_back(l, loop_edges, region=None):
    """От петли l (l.vert == v) обходит веер граней вокруг v в сторону
    предыдущего ребра, пока не упрётся в ребро петли или границу."""
    faces, inner = [], []
    seen = set()
    while l is not None and l.face not in seen:
        seen.add(l.face)
        faces.append(l.face)
        prev = l.link_loop_prev
        e = prev.edge
        if e in loop_edges:
            break
        inner.append(e)
        r = prev.link_loop_radial_next
        if r is prev:          # граница меша
            break
        if region is not None and r.face not in region:
            break              # край выделения (торец полосы)
        l = r                  # r.vert == v
    return faces, inner


def walk_fwd(l, loop_edges, region=None):
    """То же, но в другую сторону (l.vert == v, идём через l.edge)."""
    faces, inner = [], []
    seen = set()
    while l is not None and l.face not in seen:
        seen.add(l.face)
        faces.append(l.face)
        e = l.edge
        if e in loop_edges:
            break
        inner.append(e)
        r = l.link_loop_radial_next
        if r is l:
            break
        if region is not None and r.face not in region:
            break
        l = r.link_loop_next   # вершина v в соседней грани
    return faces, inner


# ---------------------------------------------------------------- core

def one_side_inset(bm, chains, dist, even, slide, flip, region_faces=None):
    limit = region_faces if (region_faces is not None and not flip) else None
    loop_edges_all = set()
    for verts, closed in chains:
        n = len(verts)
        for i in range(n if closed else n - 1):
            loop_edges_all.add(edge_between(verts[i], verts[(i + 1) % n]))

    face_repl = {}      # face -> {old_vert: new_vert}
    quads = []          # списки вершин новых граней-полосы
    new_verts = []

    for verts, closed in chains:
        n = len(verts)
        m = n if closed else n - 1

        # ориентация: для режима граней — внутрь выделенных граней
        if region_faces is not None:
            e0 = edge_between(verts[0], verts[1])
            l0 = left_loop(verts[0], verts[1], e0)
            if l0 is None or l0.face not in region_faces:
                verts.reverse()
        if flip:
            verts.reverse()

        edges = [edge_between(verts[i], verts[(i + 1) % n]) for i in range(m)]
        lefts = []
        for i, e in enumerate(edges):
            a, b = verts[i], verts[(i + 1) % n]
            if len(e.link_faces) > 2:
                raise ValueError("Неманифолдное ребро в выделении")
            l = left_loop(a, b, e)
            if l is None:
                raise ValueError("С этой стороны нет граней (граница меша или неконсистентные нормали) — попробуй Flip")
            lefts.append(l)

        # перпендикуляры рёбер в плоскости левой грани
        perps = []
        for i, e in enumerate(edges):
            a, b = verts[i], verts[(i + 1) % n]
            d = (b.co - a.co).normalized()
            p = lefts[i].face.normal.cross(d)
            if p.length < 1e-8:
                p = (lefts[i].face.calc_center_median() - a.co)
            perps.append(p.normalized())

        dup = [None] * n
        for i, v in enumerate(verts):
            inc = (i - 1) % n if (closed or i > 0) else None
            out = i if (closed or i < n - 1) else None

            # веер граней слева у вершины
            if out is not None:
                faces, inner = walk_back(lefts[out], loop_edges_all, limit)
            else:
                faces, inner = walk_fwd(lefts[inc].link_loop_next, loop_edges_all, limit)

            # внутренний конец открытой цепочки — обход замкнулся на другую сторону
            rf = set()
            for k in (inc, out):
                if k is not None:
                    f = right_face(verts[k], verts[(k + 1) % n], edges[k])
                    if f is not None:
                        rf.add(f)
            if rf & set(faces):
                continue

            ps = [perps[k] for k in (inc, out) if k is not None]
            avg = sum(ps, ps[0] * 0).normalized()
            if avg.length < 1e-8:
                avg = ps[0]
            miter = 1.0
            if even:
                miter = 1.0 / max(min(avg.dot(p) for p in ps), 0.25)

            if slide and len(inner) == 1:
                ov = inner[0].other_vert(v)
                ed = ov.co - v.co
                elen = ed.length
                ed.normalize()
                t = dist / max(ed.dot(avg), 0.1) if even else dist
                t = min(t, elen * 0.95)
                co = v.co + ed * t
            else:
                co = v.co + avg * dist * miter

            nv = bm.verts.new(co, v)
            new_verts.append(nv)
            dup[i] = nv
            for f in faces:
                face_repl.setdefault(f, {})[v] = nv

        for i in range(m):
            a, b = verts[i], verts[(i + 1) % n]
            na, nb = dup[i], dup[(i + 1) % n]
            if na is None and nb is None:
                continue
            q = [a, b] + [x for x in (nb, na) if x is not None]
            quads.append((q, lefts[i].face))

    # пересоздаём грани левой стороны с новыми вершинами
    rebuilt = {}
    old_faces = list(face_repl.keys())
    for f, repl in face_repl.items():
        loops = list(f.loops)
        vs = [repl.get(l.vert, l.vert) for l in loops]
        try:
            nf = bm.faces.new(vs, f)
        except ValueError:
            continue
        for nl, ol in zip(nf.loops, loops):
            nl.copy_from(ol)
        nf.select = f.select
        rebuilt[f] = nf

    orig_of = {}
    for repl in face_repl.values():
        for ov, nv in repl.items():
            orig_of[ov] = nv

    strip = []
    for q, src in quads:
        if len(set(q)) < 3:
            continue
        try:
            nf = bm.faces.new(q, src)
        except ValueError:
            continue
        # UV/атрибуты углов берём из соседней (левой) грани
        tgt = rebuilt.get(src, src)
        by_vert = {}
        for l in tgt.loops:
            by_vert[l.vert] = l
        for l in src.loops:
            by_vert.setdefault(l.vert, l)
        for l in nf.loops:
            s = by_vert.get(l.vert)
            if s is None and l.vert in orig_of:
                # исходная вершина -> берём угол её дубликата
                s = by_vert.get(orig_of[l.vert])
            if s is not None:
                l.copy_from(s)
        strip.append(nf)

    bmesh.ops.delete(bm, geom=[f for f in old_faces if f in rebuilt], context='FACES_ONLY')
    # рёбра, оставшиеся без граней после пересборки
    cand = set()
    for v in orig_of:
        for e in v.link_edges:
            if not e.link_faces:
                cand.add(e)
    if cand:
        bmesh.ops.delete(bm, geom=list(cand), context='EDGES')

    return new_verts, strip


def orient_chain(verts, closed, flip, region_faces):
    if region_faces is not None:
        e0 = edge_between(verts[0], verts[1])
        l0 = left_loop(verts[0], verts[1], e0)
        if l0 is None or l0.face not in region_faces:
            verts.reverse()
    if flip:
        verts.reverse()


def ray_hit_polyline(o, u, nrm, pts, idxs, closed, n):
    """Пересечение луча o+u*t (в плоскости с нормалью nrm) с отрезками
    pts[j]-pts[j+1], j из idxs. Возвращает (t, j) ближайшего или None."""
    ax = u
    ay = nrm.cross(u)
    best = None
    for j in idxs:
        a = pts[j]
        b = pts[(j + 1) % n]
        ex = 0.0
        if not closed:  # концы открытой цепочки продлеваем
            if j == 0:
                a = a + (a - b) * 1000.0
            if j == n - 2:
                b = b + (b - a) * 1000.0
        a2 = ((a - o).dot(ax), (a - o).dot(ay))
        b2 = ((b - o).dot(ax), (b - o).dot(ay))
        dy = b2[1] - a2[1]
        if abs(dy) < 1e-12:
            continue
        s = -a2[1] / dy
        if s < -1e-6 or s > 1 + 1e-6:
            continue
        t = a2[0] + (b2[0] - a2[0]) * s
        if t > 1e-7 and (best is None or t < best[0]):
            best = (t, j)
    return best


def one_side_cut(bm, chains, dist, flip, region_faces=None):
    """Одна новая линия параллельно петле (как Loop Cut): режет грани с одной
    стороны, вершины ставятся на существующие рёбра. Возвращает грани полосы
    между исходным краем и новой линией."""
    limit = region_faces if (region_faces is not None and not flip) else None
    loop_edges_all = set()
    for verts, closed in chains:
        n = len(verts)
        for i in range(n if closed else n - 1):
            loop_edges_all.add(edge_between(verts[i], verts[(i + 1) % n]))

    info = {}           # вершина петли -> (цепочка, индекс)
    fan_of = {}         # вершина петли -> грани веера с нужной стороны
    split_info = {}     # новая вершина -> (цепочка, индекс отрезка)
    chain_data = []
    split_done = set()
    clamped = 0
    W = 48              # окно поиска отрезков вокруг вершины

    for ci, (verts, closed) in enumerate(chains):
        orient_chain(verts, closed, flip, region_faces)
        n = len(verts)
        m = n if closed else n - 1
        edges = [edge_between(verts[i], verts[(i + 1) % n]) for i in range(m)]
        lefts = []
        for i, e in enumerate(edges):
            if len(e.link_faces) > 2:
                raise ValueError("Неманифолдное ребро в выделении")
            l = left_loop(verts[i], verts[(i + 1) % n], e)
            if l is None:
                raise ValueError("С этой стороны нет граней (граница меша или неконсистентные нормали) — попробуй Flip")
            lefts.append(l)
        perps = []
        for i, e in enumerate(edges):
            a, b = verts[i], verts[(i + 1) % n]
            p = lefts[i].face.normal.cross((b.co - a.co).normalized())
            if p.length < 1e-8:
                p = lefts[i].face.calc_center_median() - a.co
            perps.append(p.normalized())

        pts = []
        fans = []
        for i, v in enumerate(verts):
            info[v] = (ci, i)
            inc = (i - 1) % n if (closed or i > 0) else None
            out = i if (closed or i < n - 1) else None
            if out is not None:
                faces, inner = walk_back(lefts[out], loop_edges_all, limit)
            else:
                faces, inner = walk_fwd(lefts[inc].link_loop_next, loop_edges_all, limit)
            rf = set()
            for k in (inc, out):
                if k is not None:
                    f = right_face(verts[k], verts[(k + 1) % n], edges[k])
                    if f is not None:
                        rf.add(f)
            if rf & set(faces):
                pts.append(v.co.copy())   # внутренний конец открытой цепочки
                fans.append(None)
                continue
            fan_of[v] = set(faces)
            ps = [perps[k] for k in (inc, out) if k is not None]
            avg = sum(ps, ps[0] * 0)
            avg = avg.normalized() if avg.length > 1e-8 else ps[0]
            pts.append(v.co + avg * dist / max(min(avg.dot(p) for p in ps), 0.25))
            nrm = sum((f.normal for f in faces), faces[0].normal * 0).normalized()
            fans.append((inner, nrm))
        chain_data.append((verts, closed, pts))

        nseg = m
        for i, v in enumerate(verts):
            if fans[i] is None:
                continue
            inner, nrm = fans[i]
            if closed:
                idxs = sorted({(i + d) % n for d in range(-W, W)}) if n > 2 * W else range(nseg)
            else:
                idxs = range(max(0, i - W), min(nseg, i + W))
            for e in inner:
                if not e.is_valid or e in split_done:
                    continue
                x = e.other_vert(v)
                u = x.co - v.co
                elen = u.length
                if elen < 1e-9:
                    continue
                u /= elen
                hit = ray_hit_polyline(v.co, u, nrm, pts, idxs, closed, n)
                if hit is None:
                    clamped += 1
                    continue
                t, j = hit
                if t > elen * 0.98:
                    t = elen * 0.98
                    clamped += 1
                split_done.add(e)
                ne, nv = bmesh.utils.edge_split(e, v, 0.5)
                nv.co = v.co + u * t
                split_info[nv] = (ci, j)

    # задания на разрез граней
    loop_verts = set(fan_of)
    jobs = []
    for f in {f for fs in fan_of.values() for f in fs}:
        if not f.is_valid:
            continue
        loops = list(f.loops)
        nl = len(loops)
        in_run = [l.vert in loop_verts and f in fan_of[l.vert] for l in loops]
        for j, l in enumerate(loops):
            if not in_run[j]:
                continue
            pj = (j - 1) % nl
            if in_run[pj] and loops[pj].edge in loop_edges_all:
                continue   # не начало серии
            run = [l]
            k = j
            while loops[k].edge in loop_edges_all and in_run[(k + 1) % nl] and len(run) < nl:
                k = (k + 1) % nl
                run.append(loops[k])
            start = run[0].link_loop_prev.vert
            end = run[-1].link_loop_next.vert
            ci, i0 = info[run[0].vert]
            _, i1 = info[run[-1].vert]
            verts, closed, pts = chain_data[ci]
            n = len(verts)
            if start in split_info and split_info[start][0] == ci:
                js = split_info[start][1]
            elif start in info and info[start][0] == ci and start not in loop_verts:
                js = info[start][1]           # линия сходится в конец цепочки
            else:
                start, js = run[0].vert, i0
            if end in split_info and split_info[end][0] == ci:
                je = split_info[end][1]
            elif end in info and info[end][0] == ci and end not in loop_verts:
                je = info[end][1] - 1
            else:
                end, je = run[-1].vert, (i1 - 1) % n
            if start is end:
                continue
            coords = []
            k = (js + 1) % n
            steps = (je - js) % n if closed else je - js
            for _ in range(max(steps, 0)):
                coords.append(pts[k].copy())
                k = (k + 1) % n
            # убираем изломы, совпадающие с точками входа/выхода или друг с другом
            eps = max(dist * 1e-3, 1e-6)
            clean = []
            for c in coords:
                if (c - start.co).length < eps or (c - end.co).length < eps:
                    continue
                if clean and (c - clean[-1]).length < eps:
                    continue
                clean.append(c)
            jobs.append((start, end, clean, run[0].vert))

    strip = []
    cut_verts = set(split_info)
    for start, end, coords, anchor in jobs:
        f = next((f for f in start.link_faces if end in f.verts and anchor in f.verts), None)
        if f is None:
            continue
        if not coords and any(start in e.verts and end in e.verts for e in f.edges):
            strip.append(f)
            continue
        try:
            nf, nl = bmesh.utils.face_split(f, start, end, coords=coords, use_exist=True)
        except ValueError:
            continue
        for x in (nf, f):
            for v in x.verts:
                if v not in loop_verts and v not in split_info:
                    cut_verts.add(v)
        strip.append(f if anchor in f.verts else nf)

    return list(cut_verts), strip, clamped


# ---------------------------------------------------------------- operator

class MESH_OT_one_side_inset(bpy.types.Operator):
    """Параллельная петля рёбер только с одной стороны выделения"""
    bl_idname = "mesh.one_side_inset"
    bl_label = "One Side Inset"
    bl_options = {'REGISTER', 'UNDO'}

    distance: FloatProperty(name="Distance", default=0.1, min=0.0, soft_max=10.0,
                            unit='LENGTH', precision=4, step=1)
    method: EnumProperty(name="Method", default='CUT',
                         items=(('CUT', "Одна линия", "Как Loop Cut: одна новая линия режет грани, без поперечных рёбер"),
                                ('INSET', "Inset", "Полоса новых граней с поперечными рёбрами (старый режим)")))
    flip: BoolProperty(name="Flip Side", default=False,
                       description="Сделать петлю с другой стороны")
    even: BoolProperty(name="Even Thickness", default=True,
                       description="Держать одинаковую ширину на углах")
    slide: BoolProperty(name="Slide Along Edges", default=False,
                        description="Где возможно, ставить новую вершину на существующее ребро (сохраняет сетку)")
    boundary: EnumProperty(name="Side", default='OUTER',
                           description="Режим граней: вдоль какого края выделения сделать петлю",
                           items=(('OUTER', "Внешний край", "Петля только вдоль внешнего края выделения"),
                                  ('INNER', "Внутренний край", "Петля только вдоль внутреннего края (дырки)")))

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        face_mode = context.tool_settings.mesh_select_mode[2]
        any_done = False
        for ob in context.objects_in_mode_unique_data:
            me = ob.data
            bm = bmesh.from_edit_mesh(me)
            # слои создаём ДО сбора ссылок: новый слой инвалидирует BMVert/BMFace
            st = context.scene.one_side_inset
            tag = bm.verts.layers.int.get(TAG_LAYER) or bm.verts.layers.int.new(TAG_LAYER)
            if st.auto_color:
                get_color_layer(me, bm, st.attr_name)
            bm.normal_update()

            region = None
            if face_mode:
                region = {f for f in bm.faces if f.select}
                if not region:
                    continue
                edges = [e for e in bm.edges
                         if sum(1 for f in e.link_faces if f in region) == 1]
            else:
                edges = [e for e in bm.edges if e.select]
            if not edges:
                continue

            try:
                chains = build_chains(edges)
                if region is not None:
                    chains = filter_boundaries(chains, region, self.boundary)
                    if not chains:
                        self.report({'WARNING'}, "У выделения нет такого края — выбери другую сторону")
                        continue
                if self.method == 'CUT':
                    new_verts, strip, clamped = one_side_cut(bm, chains, self.distance,
                                                             self.flip, region)
                    if clamped:
                        self.report({'WARNING'}, f"Distance больше соседних граней — {clamped} точек прижато к рёбрам")
                else:
                    new_verts, strip = one_side_inset(bm, chains, self.distance,
                                                      self.even, self.slide, self.flip, region)
            except ValueError as ex:
                self.report({'ERROR'}, str(ex))
                return {'CANCELLED'}

            # выделяем только новые грани полосы
            for f in bm.faces:
                f.select = False
            for e in bm.edges:
                e.select = False
            for v in bm.verts:
                v.select = False
            for f in strip:
                f.select_set(True)

            # метки для градиента: 1 — исходный край, 2 — новая линия
            for v in bm.verts:
                v[tag] = 0
            for verts, closed in chains:
                for v in verts:
                    if v.is_valid:
                        v[tag] = 1
            for v in new_verts:
                if v.is_valid and v[tag] == 0:
                    v[tag] = 2

            if st.auto_color:
                paint_from(st, me, bm, strip)

            bmesh.update_edit_mesh(me)
            any_done = True

        if not any_done:
            self.report({'WARNING'}, "Выдели петлю рёбер (или грани)")
            return {'CANCELLED'}
        context.tool_settings.mesh_select_mode = (False, False, True)
        return {'FINISHED'}


# ---------------------------------------------------------------- vertex color

def srgb_from_linear(c):
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def get_color_layer(me, bm, name):
    """Слой цвета в bmesh: атрибут name, иначе активный, иначе создаётся
    'Color' (FLOAT_COLOR, Face Corner — как у madout_unity_vertex_colors)."""
    attr = me.color_attributes.get(name) if name else None
    if attr is None and not name:
        attr = me.color_attributes.active_color
    if attr is None:
        attr_name = name or "Color"
        layer = bm.loops.layers.float_color.new(attr_name)
        return layer, 'CORNER', False, attr_name
    byte = attr.data_type == 'BYTE_COLOR'
    if attr.domain == 'CORNER':
        layers = bm.loops.layers.color if byte else bm.loops.layers.float_color
    else:
        layers = bm.verts.layers.color if byte else bm.verts.layers.float_color
    return layers[attr.name], attr.domain, byte, attr.name


TAG_LAYER = ".one_side_inset_tag"


def apply_vertex_color(me, bm, faces, color, strength, name="",
                       mask=(True, True, True, True), raw=False,
                       only_verts=None, soften=False):
    """raw=True — значения каналов пишутся как есть (маски), без sRGB-конверсии.
    mask — какие каналы R, G, B, A менять."""
    layer, domain, byte, attr_name = get_color_layer(me, bm, name)
    col = list(color)
    if byte and not raw:  # BYTE_COLOR хранится в sRGB
        col = [srgb_from_linear(c) for c in col[:3]] + [col[3]]
    s = strength
    if only_verts is not None:
        # только углы на вершинах одного края -> градиент к другому краю
        if soften:
            faces = {f for v in only_verts for f in v.link_faces}
        if domain == 'CORNER':
            elems = [l for f in faces for l in f.loops if l.vert in only_verts]
        else:
            elems = list(only_verts)
    elif domain == 'CORNER':
        elems = [l for f in faces for l in f.loops]
    else:
        elems = list({v for f in faces for v in f.verts})
    for el in elems:
        old = el[layer]
        el[layer] = [old[i] * (1.0 - s) + col[i] * s if mask[i] else old[i] for i in range(4)]
    return attr_name


def target_verts(p, bm, faces):
    """Вершины, от которых идёт градиент (None — красить грани целиком)."""
    if p.paint_target == 'FACES':
        return None
    if p.paint_target == 'SELECTED_EDGES':
        return {v for e in bm.edges if e.select for v in e.verts}
    if p.paint_target == 'SIDE':
        region = set(faces)
        edges = [e for e in bm.edges if sum(1 for f in e.link_faces if f in region) == 1]
        if not edges:
            return set()
        try:
            chains = filter_boundaries(build_chains(edges), region, p.paint_side)
        except ValueError:
            return set()
        return {v for verts, closed in chains for v in verts}
    tag = bm.verts.layers.int.get(TAG_LAYER)
    if tag is None:
        return set()
    want = 1 if p.paint_target == 'EDGE' else 2
    return {v for f in faces for v in f.verts if v[tag] == want}


def paint_from(p, me, bm, faces):
    """Красит по настройкам p (оператор или настройки сцены)."""
    verts = target_verts(p, bm, faces)
    if p.paint_target == 'SELECTED_EDGES':
        faces = {f for v in verts for f in v.link_faces
                 if any(e.select for e in f.edges)}
    if verts is not None and not verts:
        return None
    if p.color_mode == 'CHANNELS':
        return apply_vertex_color(me, bm, faces, p.channels, p.strength, p.attr_name,
                                  mask=tuple(p.use_channels), raw=True,
                                  only_verts=verts, soften=p.soften)
    return apply_vertex_color(me, bm, faces, p.color, p.strength, p.attr_name,
                              only_verts=verts, soften=p.soften)


class MESH_OT_one_side_inset_color(bpy.types.Operator):
    """Покрасить выделенные грани в vertex color с заданной силой"""
    bl_idname = "mesh.one_side_inset_color"
    bl_label = "Apply Vertex Color"
    bl_options = {'REGISTER', 'UNDO'}

    color: FloatVectorProperty(name="Color", subtype='COLOR', size=4,
                               min=0.0, max=1.0, default=(1.0, 1.0, 1.0, 1.0))
    strength: FloatProperty(name="Strength", default=1.0, min=0.0, max=1.0,
                            subtype='FACTOR')
    color_mode: EnumProperty(name="Color Mode", default='CHANNELS',
                             items=(('CHANNELS', "Каналы RGBA", "Отдельно R, G, B, A со своей интенсивностью"),
                                    ('COLOR', "Цвет", "Обычный выбор цвета")))
    channels: FloatVectorProperty(name="Channels", size=4, min=0.0, max=1.0,
                                  default=(1.0, 0.0, 0.0, 1.0), subtype='NONE',
                                  description="Интенсивность каналов R, G, B, A")
    use_channels: BoolVectorProperty(name="Use Channels", size=4,
                                     default=(True, False, False, False),
                                     description="Какие каналы менять (остальные не трогаются)")
    paint_target: EnumProperty(name="Откуда красить", default='EDGE',
                               items=(('EDGE', "От края", "Цвет на исходном крае, затухает к новой линии"),
                                      ('LINE', "От линии", "Цвет на новой линии, затухает к исходному краю"),
                                      ('SIDE', "Край выделения", "Цвет на внешнем или внутреннем крае выделенных граней"),
                                      ('SELECTED_EDGES', "По рёбрам", "Цвет на вершинах выделенных рёбер"),
                                      ('FACES', "Грани", "Залить выделенные грани целиком")))
    paint_side: EnumProperty(name="Край", default='OUTER',
                             items=(('OUTER', "Внешний", "Внешний край выделения (у полосы — длинная сторона)"),
                                    ('INNER', "Внутренний", "Внутренний край выделения (у полосы — короткая сторона)")))
    soften: BoolProperty(name="Смягчить на соседние грани", default=False,
                         description="Красить вершины края и в соседних гранях снаружи полосы (без резкой границы)")
    attr_name: StringProperty(name="Attribute", default="")

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        done = 0
        for ob in context.objects_in_mode_unique_data:
            me = ob.data
            bm = bmesh.from_edit_mesh(me)
            get_color_layer(me, bm, self.attr_name)   # создать слой до сбора граней
            faces = [f for f in bm.faces if f.select]
            if not faces and self.paint_target != 'SELECTED_EDGES':
                continue
            name = paint_from(self, me, bm, faces)
            if name is None:
                self.report({'WARNING'}, "Не найдено вершин края: сначала сделай One Side Inset "
                                         "или выбери «По выделенным рёбрам»")
                return {'CANCELLED'}
            bmesh.update_edit_mesh(me)
            attr = me.color_attributes.get(name)
            if attr is not None:
                me.color_attributes.active_color = attr
            done += 1
        if not done:
            self.report({'WARNING'}, "Нет выделенных граней/рёбер")
            return {'CANCELLED'}
        self.report({'INFO'}, "Vertex color применён")
        return {'FINISHED'}


class OneSideInsetSettings(bpy.types.PropertyGroup):
    color: FloatVectorProperty(name="Color", subtype='COLOR', size=4,
                               min=0.0, max=1.0, default=(1.0, 1.0, 1.0, 1.0))
    strength: FloatProperty(name="Strength", default=1.0, min=0.0, max=1.0,
                            subtype='FACTOR', description="Сила (видимость) цвета: 0 — без изменений, 1 — полностью")
    attr_name: StringProperty(name="Attribute", default="",
                              description="Имя атрибута цвета; пусто — активный (или создать 'Color')")
    auto_color: BoolProperty(name="Красить сразу после Inset", default=False)
    color_mode: EnumProperty(name="Color Mode", default='CHANNELS',
                             items=(('CHANNELS', "Каналы RGBA", "Отдельно R, G, B, A со своей интенсивностью"),
                                    ('COLOR', "Цвет", "Обычный выбор цвета")))
    channels: FloatVectorProperty(name="Channels", size=4, min=0.0, max=1.0,
                                  default=(1.0, 0.0, 0.0, 1.0), subtype='NONE',
                                  description="Интенсивность каналов R, G, B, A")
    use_channels: BoolVectorProperty(name="Use Channels", size=4,
                                     default=(True, False, False, False),
                                     description="Какие каналы менять (остальные не трогаются)")
    paint_target: EnumProperty(name="Откуда красить", default='EDGE',
                               items=(('EDGE', "От края", "Цвет на исходном крае, затухает к новой линии"),
                                      ('LINE', "От линии", "Цвет на новой линии, затухает к исходному краю"),
                                      ('SIDE', "Край выделения", "Цвет на внешнем или внутреннем крае выделенных граней"),
                                      ('SELECTED_EDGES', "По рёбрам", "Цвет на вершинах выделенных рёбер"),
                                      ('FACES', "Грани", "Залить выделенные грани целиком")))
    paint_side: EnumProperty(name="Край", default='OUTER',
                             items=(('OUTER', "Внешний", "Внешний край выделения (у полосы — длинная сторона)"),
                                    ('INNER', "Внутренний", "Внутренний край выделения (у полосы — короткая сторона)")))
    soften: BoolProperty(name="Смягчить на соседние грани", default=False,
                         description="Красить вершины края и в соседних гранях снаружи полосы (без резкой границы)")
    distance: FloatProperty(name="Distance", default=0.1, min=0.0, soft_max=10.0,
                            unit='LENGTH', precision=4, step=1)
    method: EnumProperty(name="Method", default='CUT',
                         items=(('CUT', "Одна линия", "Как Loop Cut: одна новая линия режет грани, без поперечных рёбер"),
                                ('INSET', "Inset", "Полоса новых граней с поперечными рёбрами (старый режим)")))
    flip: BoolProperty(name="Flip Side", default=False,
                       description="Сделать петлю с другой стороны")
    even: BoolProperty(name="Even Thickness", default=True,
                       description="Держать одинаковую ширину на углах")
    slide: BoolProperty(name="Slide Along Edges", default=False,
                        description="Где возможно, ставить новую вершину на существующее ребро (сохраняет сетку)")
    boundary: EnumProperty(name="Side", default='OUTER',
                           description="Режим граней: вдоль какого края выделения сделать петлю",
                           items=(('OUTER', "Внешний край", "Петля только вдоль внешнего края выделения"),
                                  ('INNER', "Внутренний край", "Петля только вдоль внутреннего края (дырки)")))


class VIEW3D_PT_one_side_inset(bpy.types.Panel):
    bl_label = "One Side Inset"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_order = 1
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        if context.mode != 'EDIT_MESH':
            layout.label(text="Выбери меш и нажми Tab", icon='INFO')
            layout.label(text="(нужен Edit Mode)")
            return
        st = context.scene.one_side_inset
        layout.row().prop(st, "method", expand=True)
        col = layout.column(align=True)
        col.prop(st, "distance")
        face_mode = context.tool_settings.mesh_select_mode[2]
        box = layout.box()
        box.label(text="Сторона:")
        if face_mode:
            box.row().prop(st, "boundary", expand=True)
            box.prop(st, "flip", text="Наружу от выделения", toggle=True)
        else:
            box.prop(st, "flip", text="Сторона B" if st.flip else "Сторона A", toggle=True, icon='ARROW_LEFTRIGHT')
        if st.method == 'INSET':
            col = layout.column(align=True)
            col.prop(st, "even")
            col.prop(st, "slide")
        op = layout.operator(MESH_OT_one_side_inset.bl_idname, icon='MOD_SOLIDIFY')
        op.distance = st.distance
        op.method = st.method
        op.flip = st.flip
        op.even = st.even
        op.slide = st.slide
        op.boundary = st.boundary
        layout.label(text="Грани: петля вдоль одного края" if face_mode
                     else "Рёбра: выдели петлю", icon='INFO')

        box = layout.box()
        box.label(text="Vertex Color", icon='COLOR')
        box.row().prop(st, "color_mode", expand=True)
        if st.color_mode == 'CHANNELS':
            col = box.column(align=True)
            for i, name in enumerate("RGBA"):
                row = col.row(align=True)
                row.prop(st, "use_channels", index=i, text=name, toggle=True)
                sub = row.row(align=True)
                sub.active = st.use_channels[i]
                sub.prop(st, "channels", index=i, text="Интенсивность", slider=True)
        else:
            box.prop(st, "color", text="")
        box.prop(st, "strength", text="Сила смешивания")
        box.label(text="Откуда красить:")
        grid = box.grid_flow(columns=2, align=True)
        grid.prop_enum(st, "paint_target", 'EDGE')
        grid.prop_enum(st, "paint_target", 'LINE')
        grid.prop_enum(st, "paint_target", 'SIDE')
        grid.prop_enum(st, "paint_target", 'SELECTED_EDGES')
        grid.prop_enum(st, "paint_target", 'FACES')
        if st.paint_target == 'SIDE':
            box.row().prop(st, "paint_side", expand=True)
        if st.paint_target != 'FACES':
            box.prop(st, "soften")
        ob = context.edit_object
        if ob and ob.type == 'MESH':
            box.prop_search(st, "attr_name", ob.data, "color_attributes", text="")
        box.prop(st, "auto_color")
        op = box.operator(MESH_OT_one_side_inset_color.bl_idname, icon='BRUSH_DATA')
        op.color = st.color
        op.strength = st.strength
        op.color_mode = st.color_mode
        op.channels = st.channels
        op.use_channels = st.use_channels
        op.paint_target = st.paint_target
        op.paint_side = st.paint_side
        op.soften = st.soften
        op.attr_name = st.attr_name


def menu_func(self, context):
    self.layout.operator(MESH_OT_one_side_inset.bl_idname, icon='MOD_SOLIDIFY')


MENUS = (
    "VIEW3D_MT_edit_mesh_edges",
    "VIEW3D_MT_edit_mesh_faces",
    "VIEW3D_MT_edit_mesh_context_menu",
)


CLASSES = (OneSideInsetSettings, MESH_OT_one_side_inset, MESH_OT_one_side_inset_color,
           VIEW3D_PT_one_side_inset)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.one_side_inset = bpy.props.PointerProperty(type=OneSideInsetSettings)
    for m in MENUS:
        getattr(bpy.types, m).append(menu_func)


def unregister():
    for m in MENUS:
        getattr(bpy.types, m).remove(menu_func)
    del bpy.types.Scene.one_side_inset
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
