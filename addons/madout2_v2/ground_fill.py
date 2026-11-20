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
            return None, "Selection branches: each chain must be a simple edge path"
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


class MESH_OT_ground_fill(bpy.types.Operator):
    """Fill between two selected edge chains with a grid surface you can bulge up or down"""
    bl_idname = "mesh.ground_fill"
    bl_label = "Ground Fill"
    bl_options = {'REGISTER', 'UNDO'}

    rows: IntProperty(
        name="Rows", description="Lines between the two chains (parallel to them)",
        default=3, min=0, max=200)
    extra_along: IntProperty(
        name="Extra Along", description="Additional cross lines spread evenly along the length",
        default=0, min=0, max=500)
    merge_along: FloatProperty(
        name="Merge Along", description="Cross lines closer than this (fraction of length) snap together, avoiding slivers",
        default=0.02, min=0.0, max=0.2, subtype='FACTOR')
    bulge: FloatProperty(
        name="Bulge", description="Push the surface up (+) or down (-) in world Z, meters",
        default=0.0, soft_min=-20.0, soft_max=20.0, unit='LENGTH')
    shape: EnumProperty(
        name="Profile", default='SMOOTH',
        items=[
            ('SMOOTH', "Smooth", "Sine hill"),
            ('SPHERE', "Round", "Round, steep at the edges"),
            ('LINEAR', "Linear", "Ridge"),
            ('SHARP', "Sharp", "Narrow peak"),
            ('FLAT', "Plateau", "Flat top with slopes at the edges"),
        ])
    bias: FloatProperty(
        name="Peak Shift", description="Shift the peak toward chain A (-) or chain B (+)",
        default=0.0, min=-1.0, max=1.0, subtype='FACTOR')
    end_taper: FloatProperty(
        name="End Taper", description="Fade the bulge to zero near open ends (fraction of length)",
        default=0.1, min=0.0, max=0.5, subtype='FACTOR')
    flip: BoolProperty(name="Flip Normals", default=False)
    uv_strip: BoolProperty(
        name="Strip UV", description="Write UV: U across, V along (active UV map)", default=True)
    uv_scale: FloatProperty(name="UV per Meter", default=0.1, min=0.0001, soft_max=10.0)

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "rows")
        col.prop(self, "extra_along")
        col.prop(self, "merge_along")
        col.separator()
        col.prop(self, "bulge")
        col.prop(self, "shape")
        col.prop(self, "bias")
        col.prop(self, "end_taper")
        col.separator()
        col.prop(self, "flip")
        col.prop(self, "uv_strip")
        sub = col.column()
        sub.enabled = self.uv_strip
        sub.prop(self, "uv_scale")

    def execute(self, context):
        obj = context.edit_object
        me = obj.data
        mw = obj.matrix_world
        bm = bmesh.from_edit_mesh(me)

        chains, err = _chains_from_selection(bm)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        if chains is None or len(chains) != 2:
            self.report({'ERROR'}, "Select exactly two separate edge chains")
            return {'CANCELLED'}
        (a, a_closed), (b, b_closed) = chains
        if a_closed != b_closed:
            self.report({'ERROR'}, "Both chains must be open, or both closed")
            return {'CANCELLED'}
        closed = a_closed
        if len(a) < 2 or len(b) < 2:
            self.report({'ERROR'}, "Each chain needs at least one edge")
            return {'CANCELLED'}
        b = _align_chains(a, b, closed, mw)

        a, b = self._match_along(bm, a, b, closed, mw)
        if a is None:
            self.report({'ERROR'}, "Could not match the chains")
            return {'CANCELLED'}

        grid, faces = self._build_grid(bm, obj, a, b, closed)
        self._orient(bm, faces, a + b)
        self._assign_material(faces, a + b)
        if self.uv_strip:
            self._faces = faces
            self._write_uv(bm, grid, closed, mw)

        for f in bm.faces:
            f.select = False
        for f in faces:
            f.select = True
        bm.select_flush_mode()
        bmesh.update_edit_mesh(me)
        self.report({'INFO'}, f"Ground Fill: {len(faces)} faces")
        return {'FINISHED'}

    def _match_along(self, bm, a, b, closed, mw):
        pa, len_a = _arc_params([mw @ v.co for v in a], closed)
        pb, len_b = _arc_params([mw @ v.co for v in b], closed)
        last = len(pa) - (1 if closed else 0)

        items = [(p, 'A', i) for i, p in enumerate(pa[:len(a)])]
        items += [(p, 'B', i) for i, p in enumerate(pb[:len(b)])]
        n = self.extra_along + 1
        items += [(k / n, None, None) for k in range(1, n)]
        if not closed:
            items += [(1.0, None, None)]
        items.sort(key=lambda x: (x[0], x[1] is None))

        clusters = []
        for p, side, idx in items:
            cur = clusters[-1] if clusters else None
            if cur and p - cur["p0"] <= self.merge_along and (side is None or side not in cur):
                if side:
                    cur[side] = idx
                continue
            if cur and side is None and p - cur["p0"] <= self.merge_along:
                continue
            c = {"p0": p}
            if side:
                c[side] = idx
            clusters.append(c)
        if not closed:
            # Chain ends are always matched to each other.
            ends = {"A": len(a) - 1, "B": len(b) - 1}
            for side, last_idx in ends.items():
                for c in clusters[:-1]:
                    if c.get(side) == last_idx:
                        del c[side]
                clusters[-1][side] = last_idx
            clusters[0]["A"], clusters[0]["B"] = 0, 0
        for c in clusters:
            c["p"] = c["p0"]

        new_a = self._split_chain(bm, a, pa, [(c["p"], c.get("A")) for c in clusters], closed)
        new_b = self._split_chain(bm, b, pb, [(c["p"], c.get("B")) for c in clusters], closed)
        return new_a, new_b

    def _split_chain(self, bm, chain, params, targets, closed):
        """For each (param, existing_index or None), sorted by param, returns a vertex, splitting chain edges where needed."""
        verts = chain + ([chain[0]] if closed else [])
        result = []
        seg = 0
        start, start_p = verts[0], params[0]
        for p, idx in targets:
            if idx is not None:
                seg = idx
                start, start_p = chain[idx], params[idx]
                result.append(start)
                continue
            new_seg = seg
            while new_seg < len(verts) - 2 and p >= params[new_seg + 1]:
                new_seg += 1
            if new_seg != seg:
                seg = new_seg
                start, start_p = verts[seg], params[seg]
            end_p = params[seg + 1]
            edge = bm.edges.get((start, verts[seg + 1]))
            if edge is None:
                return None
            fac = (p - start_p) / max(1e-12, end_p - start_p)
            _, nv = bmesh.utils.edge_split(edge, start, min(max(fac, 1e-4), 1 - 1e-4))
            result.append(nv)
            start, start_p = nv, p
        return result

    def _build_grid(self, bm, obj, a, b, closed):
        mw = obj.matrix_world
        inv3 = mw.inverted().to_3x3()
        up_local = inv3 @ Vector((0.0, 0.0, 1.0))
        rows = self.rows + 1
        k_count = len(a)
        wa = [mw @ v.co for v in a]
        _, total = _arc_params(wa, closed)
        along, _ = _arc_params(wa, closed)

        grid = [a]
        for r in range(1, rows):
            t = r / rows
            h = self.bulge * _profile(_bias_t(t, self.bias), self.shape)
            row = []
            for k in range(k_count):
                taper = 1.0
                if not closed and self.end_taper > 0:
                    s = along[k]
                    edge_dist = min(s, 1.0 - s) / self.end_taper
                    taper = 0.5 - 0.5 * math.cos(math.pi * min(1.0, edge_dist))
                co = a[k].co.lerp(b[k].co, t) + up_local * (h * taper)
                row.append(bm.verts.new(co))
            grid.append(row)
        grid.append(b)

        faces = []
        k_last = k_count if closed else k_count - 1
        for r in range(rows):
            for k in range(k_last):
                k2 = (k + 1) % k_count
                quad = [grid[r][k], grid[r][k2], grid[r + 1][k2], grid[r + 1][k]]
                if len(set(quad)) < 3:
                    continue
                quad = list(dict.fromkeys(quad))
                if bm.faces.get(quad):
                    continue
                faces.append(bm.faces.new(quad))
        bm.normal_update()
        return grid, faces

    def _orient(self, bm, faces, a):
        new = set(faces)
        flip_all = None
        for v in a:
            for e in v.link_edges:
                olds = [f for f in e.link_faces if f not in new]
                news = [f for f in e.link_faces if f in new]
                if olds and news:
                    def winding(f):
                        for l in f.loops:
                            if l.edge is e:
                                return l.vert
                    flip_all = winding(olds[0]) is winding(news[0])
                    break
            if flip_all is not None:
                break
        if flip_all is None:
            avg = sum((f.normal for f in faces), Vector())
            flip_all = avg.z < 0
        if flip_all != self.flip:
            for f in faces:
                f.normal_flip()

    def _assign_material(self, faces, a):
        new = set(faces)
        src = None
        for v in a:
            src = next((f for f in v.link_faces if f not in new), None)
            if src:
                break
        for f in faces:
            if src:
                f.material_index = src.material_index
                f.smooth = src.smooth

    def _write_uv(self, bm, grid, closed, mw):
        layer = bm.loops.layers.uv.active
        if layer is None:
            return
        rows = len(grid) - 1
        a, b = grid[0], grid[-1]
        wa = [mw @ v.co for v in a]
        wb = [mw @ v.co for v in b]
        _, len_a = _arc_params(wa, closed)
        _, len_b = _arc_params(wb, closed)
        width = sum((x - y).length for x, y in zip(wa, wb)) / max(1, len(wa))
        along_a, _ = _arc_params(wa, closed)
        along_b, _ = _arc_params(wb, closed)
        length = 0.5 * (len_a + len_b)
        uv_of = {}
        for r, row in enumerate(grid):
            t = r / rows
            for k, v in enumerate(row):
                s = along_a[k] * (1 - t) + along_b[k] * t
                uv_of[v] = (t * width * self.uv_scale, s * length * self.uv_scale)
        seam_v = (length * self.uv_scale) if closed else None
        for f in self._faces:
            if not all(v in uv_of for v in f.verts):
                continue
            vs = [uv_of[l.vert][1] for l in f.loops]
            wrap = closed and max(vs) - min(vs) > 0.5 * seam_v
            for l in f.loops:
                u, vv = uv_of[l.vert]
                if wrap and vv < 0.5 * seam_v:
                    vv += seam_v
                l[layer].uv = (u, vv)


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
