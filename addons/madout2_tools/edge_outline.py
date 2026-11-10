bl_info = {
    "name": "Loop Edge & Vertex Color Marker",
    "author": "Benza Tools",
    "version": (20, 15, 2),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Edge Outline",
    "description": "Pure-script Loop Edge cutter. Select edges → click Add Selected → a parallel loop edge is cut into the host face IMMEDIATELY (no modifier, no overlay, no preview). Press F9 after to tweak Distance / Flip via Blender's redo panel.",
    "category": "Mesh",
}

import math

import bpy
import bmesh
from mathutils import Vector
from bpy.props import (
    FloatProperty, IntProperty, EnumProperty, BoolProperty,
    CollectionProperty, StringProperty, PointerProperty,
)


COLOR_MAP = {
    'RED':   (1.0, 0.0, 0.0, 1.0),
    'GREEN': (0.0, 1.0, 0.0, 1.0),
    'BLUE':  (0.0, 0.0, 1.0, 1.0),
}


# ---------------------------------------------------------------------------
# Storage / Settings
# ---------------------------------------------------------------------------
class EOVC_StoredEdge(bpy.types.PropertyGroup):
    """One source edge inside an EdgeSet, identified by canonical (v1,v2)
    vertex-index pair. Color is now derived from the parent set, but the
    `color` field is kept for backward-compat migration from v19.x."""
    v1: IntProperty()
    v2: IntProperty()
    color: StringProperty(default="RED")  # legacy, kept for migration


def _on_set_param_changed(self, context):
    """v20.2.2: triggered when set's color/intensity/distance/extra_lines/flip
    change. Re-bakes immediately if currently in Object Mode showing baked.
    In Edit Mode the change is stored silently — handler will rebake on
    next Tab back to Object Mode."""
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return
    ed = obj.eovc_data
    if not ed.has_modifier or ed.source_mesh is None:
        return
    if not ed.auto_bake:
        return
    # Only rebake if currently showing baked (= Object Mode after a bake).
    # Edit Mode shows source so cut params changes are silent until next
    # Object Mode toggle (handler picks it up).
    if context.mode == 'OBJECT' and ed.is_baked:
        try:
            bpy.ops.eovc.rebake('EXEC_DEFAULT')
        except Exception as exc:
            print(f"[eovc set-param-change] {exc!r}")


class EOVC_EdgeSet(bpy.types.PropertyGroup):
    """A named group of source edges sharing parameters (color, distance,
    extra lines, etc). Multiple sets per object — each baked independently
    into its own pass during Re-bake. Acts like a Layer/Preset."""
    name: StringProperty(
        name="Name", default="Set",
        description="User-facing label for this set",
    )
    edges: CollectionProperty(type=EOVC_StoredEdge)
    color: EnumProperty(
        name="Color",
        items=[('RED', "Red", ""), ('GREEN', "Green", ""), ('BLUE', "Blue", "")],
        default='RED',
        update=_on_set_param_changed,
    )
    intensity: FloatProperty(
        name="Intensity", default=1.0, min=0.0, max=1.0,
        description="Multiplier on the color RGB (1.0 = full color, 0.0 = black). Alpha unaffected.",
        update=_on_set_param_changed,
    )
    distance: FloatProperty(
        name="Distance", default=0.1, min=0.0001, soft_max=10.0, unit='LENGTH',
        description="Perpendicular offset of the loop edge cut for this set",
        update=_on_set_param_changed,
    )
    extra_lines: IntProperty(
        name="Extra Lines", default=0, min=0, max=2,
        description="Extra parallel lines between source and offset for this set",
        update=_on_set_param_changed,
    )
    flip: BoolProperty(
        name="Flip Direction", default=False,
        description="Reverse the outward direction for this set",
        update=_on_set_param_changed,
    )
    visible: BoolProperty(
        name="Visible", default=True,
        description="Show this set in viewport (UI list indicator only)",
    )
    bake_enabled: BoolProperty(
        name="Include in Bake", default=True,
        description="Include this set when running Re-bake. Disable to keep edges but skip cut/paint.",
        update=_on_set_param_changed,
    )
    host_face_index: IntProperty(
        name="Host Face", default=-1,
        description="Explicit host face index for this set's cuts. -1 = auto-pick by algorithm.",
        update=_on_set_param_changed,
    )
    # v20.7: algorithm choice. PER_EDGE = legacy Phase 1-6 (independent per
    # source edge, weld at corners). MITERED = new vertex-centric: precompute
    # bisector miter target per corner, project onto boundary, then weld &
    # snap all corner offsets to the true miter position. Better for chains
    # whose corners share host face — produces single L-shape vertex instead
    # of a fan. Falls back to PER_EDGE behaviour for chain endpoints.
    algorithm: EnumProperty(
        name="Algorithm",
        items=[
            ('PER_EDGE', "Per-Edge",
             "Legacy: independent cut per source edge, post-weld at corners"),
            ('MITERED',  "Loop Cut + Miter",
             "PER_EDGE-style source preservation + Bevel-quality miters. "
             "Limited by topology — fails on chains where adjacent edges "
             "pick different host faces. Use EDGENET instead."),
            ('BEVEL',    "Bevel (Blender)",
             "Use Blender's native bevel — clean miter joins, strip width = 2*distance, replaces source edges"),
            ('EDGENET',  "OffsetLoop (Source Exact)",
             "Use Blender's offset_edgeloops primitive, then move only the "
             "new duplicate loop to the miter target. Source edges/verts "
             "stay exact; strip faces are painted on the selected side."),
        ],
        default='PER_EDGE',
        update=_on_set_param_changed,
    )


def _on_active_set_changed(self, context):
    """Auto-select the active set's edges in viewport (Edit Mode only).
    Triggered when user clicks a set in the UIList. v20.1: empty sets do NOT
    deselect — that would clobber the user's pre-selection during an Add
    Edges flow. Only sets WITH edges trigger the auto-select."""
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return
    if context.mode != 'EDIT_MESH':
        return
    if not self.has_modifier:
        return
    if len(self.edge_sets) == 0:
        return
    idx = self.active_set_index
    if idx < 0 or idx >= len(self.edge_sets):
        return
    active = self.edge_sets[idx]
    if len(active.edges) == 0:
        return  # empty set — don't touch user's selection
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        keys = {(min(e.v1, e.v2), max(e.v1, e.v2)) for e in active.edges}
        for v in bm.verts: v.select = False
        for e in bm.edges: e.select = False
        for f in bm.faces: f.select = False
        for e in bm.edges:
            a, b = e.verts[0].index, e.verts[1].index
            if (min(a, b), max(a, b)) in keys:
                e.select_set(True)
        bm.select_flush(True)
        bmesh.update_edit_mesh(obj.data)
    except Exception as exc:
        print(f"[eovc active-set-change] {exc!r}")


class EOVC_ObjectData(bpy.types.PropertyGroup):
    # Legacy flat list — preserved across versions for migration.
    # New code reads/writes via edge_sets.
    stored_edges: CollectionProperty(type=EOVC_StoredEdge)

    # v20.0: edge sets (Layers / Presets)
    edge_sets: CollectionProperty(type=EOVC_EdgeSet)
    active_set_index: IntProperty(
        default=0,
        update=_on_active_set_changed,  # v20.1: auto-select edges on click
    )

    # v19.0+: pseudo-modifier state
    has_modifier: BoolProperty(
        name="Modifier Active",
        description="True when this mesh has a Loop Edge modifier.",
        default=False,
    )
    auto_bake: BoolProperty(
        name="Auto Bake",
        description="Enter Edit Mode → swap to source. Exit Edit Mode → re-bake. Disable for manual control.",
        default=True,
    )
    source_mesh: PointerProperty(
        type=bpy.types.Mesh,
        name="Source Mesh",
        description="Pre-cut mesh datablock owned by the modifier.",
    )
    is_baked: BoolProperty(default=False)
    # Legacy "global cut params" — kept for migration to per-set params
    cut_distance: FloatProperty(default=0.1, min=0.0001)
    cut_flip: BoolProperty(default=False)
    cut_extra_lines: IntProperty(default=0, min=0, max=2)
    cut_color: StringProperty(default="RED")


_redo_state = {"in_progress": False}


def _live_redo_callback(self, context):
    """N-panel slider update: if last op was our cut, undo + re-cut.
    Defers via timer (operator calls aren't allowed from RNA update
    callbacks directly). Always unregister + re-register the timer so a
    fresh schedule fires for every slider tick — `is_registered` was
    flaky in v13.0."""
    if _redo_state["in_progress"]:
        return
    obj = context.object
    if obj is None or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
        return
    wm = context.window_manager
    if not wm.operators:
        return
    last_id = wm.operators[-1].bl_idname.lower()
    if "add_loop_cut" not in last_id:
        return
    try:
        bpy.app.timers.unregister(_deferred_redo)
    except Exception:
        pass
    bpy.app.timers.register(_deferred_redo, first_interval=0.01)


def _deferred_redo():
    if _redo_state["in_progress"]:
        return None
    _redo_state["in_progress"] = True
    try:
        s = bpy.context.scene.eovc_settings
        try:
            bpy.ops.ed.undo()
        except Exception as exc:
            print(f"[live redo] undo failed: {exc!r}")
            return None
        try:
            bpy.ops.eovc.add_loop_cut_edge(
                'EXEC_DEFAULT',
                distance=float(s.distance),
                flip_direction=bool(s.flip_direction),
                extra_lines=int(s.extra_lines),
            )
        except Exception as exc:
            print(f"[live redo] re-cut failed: {exc!r}")
        # Force a viewport refresh so the change is visible
        try:
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        except Exception:
            pass
    finally:
        _redo_state["in_progress"] = False
    return None  # don't repeat


class EOVC_Settings(bpy.types.PropertyGroup):
    distance: FloatProperty(
        name="Distance",
        description="Perpendicular offset of the loop edge. Live: if last op was Add Selected, slider drag undoes + re-cuts with new value.",
        default=0.1, min=0.0001, soft_max=10.0, unit='LENGTH',
        update=_live_redo_callback,
    )
    flip_direction: BoolProperty(
        name="Flip Direction",
        description="Reverse the auto-detected outward direction (live re-cut)",
        default=False,
        update=_live_redo_callback,
    )
    extra_lines: IntProperty(
        name="Extra Lines",
        description="Additional parallel lines BETWEEN source edge and the main offset cut. 0 = single cut at Distance. 1 = 2 lines (½D, D). 2 = 3 lines (⅓D, ⅔D, D).",
        default=0, min=0, max=2,
        update=_live_redo_callback,
    )
    color: EnumProperty(
        name="Color",
        items=[('RED', "Red", ""), ('GREEN', "Green", ""), ('BLUE', "Blue", "")],
        default='RED',
    )
    vc_layer_name: StringProperty(
        name="VC Layer", default="Col",
        description="Vertex color attribute name (created if missing)",
    )


# ---------------------------------------------------------------------------
# Direction math (host face by area heuristic)
# v20.4.3: per-edge flag — True if `flip` was already used at host-pick level
# (= host was switched to alt). Caller checks this to avoid double-flipping
# outward direction in _compute_outward. Keyed by id(BMEdge).
_eovc_flip_state = {}

# v20.15.0: host pinned per source edge by Phase 0 (trapped-corner split).
# Splitting the host changes chain-shared scoring in _pick_host_other, which
# could flip the host to the face on the other side. BMEdge → (face, flip_state).
_eovc_host_override = {}

# ---------------------------------------------------------------------------
def _pick_host_other(e, target_set=None, flip=False, explicit_host=None):
    """Pick host face = open plane the loop edge will live in.
    STRICT: host must be horizontal AND its centroid must lie within 5cm of
    the source edge's plane. This rejects slanted "almost horizontal" faces.

    v20.4.1: when multiple valid hosts exist AND `target_set` is provided
    (= the full set of source edges being cut), prefer the host that SHARES
    boundaries with other source edges in the set — ensures consistent host
    selection across a chain.

    v20.4.2: when `flip=True` and 2+ valid hosts remain, pick the OTHER one
    (smaller area / different face). Lets user switch which side of source
    edge the cut ends up on, even when adjacent faces are both valid hosts."""
    faces = list(e.link_faces)
    if not faces:
        return None, None
    pinned = _eovc_host_override.get(e)
    if pinned is not None and pinned[0] in faces and _face_is_valid(pinned[0]):
        _eovc_flip_state[id(e)] = pinned[1]
        return pinned[0], next((f for f in faces if f is not pinned[0]), None)
    edge_z = (e.verts[0].co.z + e.verts[1].co.z) * 0.5

    def is_valid_host(f):
        if abs(f.normal.z) <= 0.5:
            return False
        # v20.14.11: was `abs(centroid_z - edge_z) < 0.05` — too strict
        # for wide curb-top faces whose centroid sits 8-15 cm above the
        # edge. Now check the face's z-range covers edge_z (with 5 cm
        # slack). Topologically the edge is already on face's boundary
        # (we only iterate e.link_faces), so vert-range membership is
        # the right test.
        try:
            face_zs = [v.co.z for v in f.verts]
        except (ReferenceError, AttributeError):
            return False
        if not face_zs:
            return False
        return (min(face_zs) - 0.05) <= edge_z <= (max(face_zs) + 0.05)

    valid = [f for f in faces if is_valid_host(f)]
    if not valid:
        return None, None

    # v20.5.2: explicit host — only when directly adjacent to source edge.
    # Non-adjacent picked face is ignored (avoids problems with directional-
    # reference picking wrong host on chains with mixed adjacency).
    if explicit_host is not None and explicit_host in valid:
        other = next((f for f in faces if f is not explicit_host), None)
        _eovc_flip_state[id(e)] = False
        return explicit_host, other

    # v20.4.3: chain-consistent host (no-flip) + alt host on flip.
    # Algorithm:
    #  1. Score candidates by chain-shared count + area
    #  2. Without flip → take top-scored (most shared, then largest)
    #  3. With flip → take 2nd-scored (switches to "other side" candidate)
    if len(valid) > 1:
        if target_set:
            def shared_count(f):
                return sum(1 for ed in f.edges if ed in target_set and ed is not e)
            scored = [(shared_count(f), f.calc_area(), f) for f in valid]
        else:
            scored = [(0, f.calc_area(), f) for f in valid]
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        # Mark in module dict whether flip was used at host level
        _eovc_flip_state[id(e)] = flip and len(scored) > 1
        host = scored[1][2] if (flip and len(scored) > 1) else scored[0][2]
    else:
        host = valid[0]
        _eovc_flip_state[id(e)] = False

    other = next((f for f in faces if f is not host), None)
    return host, other


def _compute_outward(e, host, other, flip=False):
    edge_dir = e.verts[1].co - e.verts[0].co
    if edge_dir.length < 1e-9:
        return None
    edge_dir.normalize()

    if host is None:
        out = edge_dir.cross(Vector((0.0, 0.0, 1.0)))
        if out.length < 1e-8:
            out = edge_dir.cross(Vector((0.0, 1.0, 0.0)))
        out.normalize()
        return -out if flip else out

    host_n = host.normal.normalized()
    perp = edge_dir.cross(host_n)
    if perp.length < 1e-8:
        perp = edge_dir.cross(Vector((0.0, 0.0, 1.0)))
        if perp.length < 1e-8:
            perp = edge_dir.cross(Vector((0.0, 1.0, 0.0)))
    perp.normalize()

    # v18.5: orient toward HOST face interior, not "away from other face".
    # The other_face heuristic broke when host and other shared a side
    # (curb-wall lip topology) — outward ended up pointing OUT of host's
    # quad → cut plane outside face → bisect produced 0 verts → silent fail.
    edge_mid = (e.verts[0].co + e.verts[1].co) * 0.5
    if perp.dot(host.calc_center_median() - edge_mid) < 0.0:
        perp = -perp
    return -perp if flip else perp


# ---------------------------------------------------------------------------
# Loop-cut primitives
# ---------------------------------------------------------------------------
def _find_face_edge_through(face, vert, exclude_edge):
    """Legacy: returns FIRST non-source boundary edge through vert."""
    for ed in face.edges:
        if ed is exclude_edge:
            continue
        if vert in ed.verts:
            return ed
    return None


def _find_outward_boundary(face, vert, exclude_edge, outward, min_dot=0.3, target_set=None):
    """v20.3: pick the boundary edge through `vert` (in `face`, excluding
    `exclude_edge`) that's MOST aligned with `outward` direction.

    Used to avoid 'spike' artifacts: when host face has multiple edges through
    the source vert, picking one that goes off-plane (e.g., curb-wall lip going
    UP in Z while outward is HORIZONTAL into road) creates a vert at high Z.

    Selection criterion:
      cos(angle) = unit_edge_dir · outward
    Higher dot = better aligned with cut direction. Reject edges below
    `min_dot` (= angle > 73° from outward = nearly parallel to source).

    Returns (best_edge, dot) or (None, 0.0) if no acceptable edge."""
    best_edge = None
    best_dot = min_dot
    for ed in face.edges:
        if ed is exclude_edge:
            continue
        # v20.15.0: never land an offset on a neighbouring SOURCE edge —
        # that made each strip run into the next one at a corner and the
        # two strips overlapped into a square instead of a miter line.
        if target_set is not None and ed in target_set:
            continue
        if vert not in ed.verts:
            continue
        other_v = ed.other_vert(vert)
        if other_v is None:
            continue
        ed_dir = (other_v.co - vert.co)
        if ed_dir.length < 1e-9:
            continue
        ed_unit = ed_dir.normalized()
        dot = ed_unit.dot(outward)
        if dot > best_dot:
            best_dot = dot
            best_edge = ed
    return best_edge, best_dot


# v20.3.8: relaxed elongation cap.
# Was meant to catch boundaries going off-plane (curb-wall lip up) where
# perpendicular projection got amplified. But v20.3+ outward-aligned
# selection already filters those (dot > 0.3 = angle < 73° from outward).
# A long well-aligned boundary may legitimately host an offset at t_len
# > 2×distance (e.g. 3.5m boundary at 67° from outward → t_len = 2.56×d).
_MAX_BOUNDARY_ELONGATION = 5.0


def _insert_offset_vert(bm, boundary_edge, anchor, outward, distance):
    """Split `boundary_edge` so a new vert sits at perpendicular `distance`
    from `anchor` in the `outward` direction. Returns None if the boundary
    edge is too short / parallel to outward / too oblique — caller should
    fall back to bisect-based cut.

    v20.2.2: also rejects "spikes" — when boundary is at a shallow angle to
    outward direction, t_len gets amplified (e.g. 84° angle → t_len = 10×
    distance) → vert ended up far from anchor → visible spike artifact."""
    other_v = boundary_edge.other_vert(anchor)
    if other_v is None:
        return None
    ed_dir = other_v.co - anchor.co
    ed_len = ed_dir.length
    if ed_len < 1e-9:
        return None
    ed_unit = ed_dir / ed_len
    perp_component = ed_unit.dot(outward)
    if perp_component < 1e-6:
        return None
    t_len = distance / perp_component
    if t_len <= 1e-7:
        return None
    if t_len >= ed_len:
        return None  # boundary too short
    if t_len > distance * _MAX_BOUNDARY_ELONGATION:
        return None  # boundary too oblique — would create a spike
    fac = t_len / ed_len
    _new_e, new_v = bmesh.utils.edge_split(boundary_edge, anchor, fac)
    return new_v


def _is_boundary_long_enough(boundary_edge, anchor, outward, distance, margin=1.0):
    """True if a perpendicular `distance` offset fits inside `boundary_edge`
    AND the boundary is reasonably perpendicular (no spike). Used to
    pre-check both side boundaries before splitting either.

    v20.3.1: margin defaults to 1.0 (no slack) — aligned with the actual
    cutoff in `_insert_offset_vert` (`t_len >= ed_len` returns None). With
    margin=0.95 we caused method-switching at threshold values (e.g.
    distance 0.23 vs 0.24 producing different vert positions on the same
    corner → ugly extra triangle from corner-weld mismatch)."""
    other_v = boundary_edge.other_vert(anchor)
    if other_v is None:
        return False
    ed_dir = other_v.co - anchor.co
    ed_len = ed_dir.length
    if ed_len < 1e-9:
        return False
    perp = ed_dir.normalized().dot(outward)
    if perp < 1e-6:
        return False
    t_len = distance / perp
    if t_len > distance * _MAX_BOUNDARY_ELONGATION:
        return False  # too oblique — would create a spike
    return t_len < (ed_len * margin)


def _cut_via_bisect(bm, e, distance, flip, created_with_src, target_set=None, explicit_host=None):
    """Bisect-plane fallback for warped hosts or short boundary edges.
    Cuts the host face with a plane parallel to source edge at perpendicular
    `distance`, restricted to the host face only. Robust against any
    boundary edge length — the offset position is computed in 3D, not as
    a fraction along an existing edge.

    New verts are paired with the closer source vert (v1 or v2) so Phase 4
    weld can merge them with same-corner offsets from other source edges."""
    trace = _trace_for(e)
    host, other = _pick_host_other(e, target_set, flip, explicit_host)
    if host is None:
        if trace: print(f"[trace {trace}] _cut_via_bisect: NO HOST")
        return False
    # v20.4.3: if host was already switched (flip consumed there), don't
    # double-flip outward; otherwise apply flip to negate outward direction.
    flip_for_outward = flip and not _eovc_flip_state.get(id(e), False)
    outward = _compute_outward(e, host, other, flip_for_outward)
    if outward is None:
        if trace: print(f"[trace {trace}] _cut_via_bisect: NO OUTWARD")
        return False
    if trace: print(f"[trace {trace}] _cut_via_bisect: host=#{host.index} outward=({outward.x:.3f},{outward.y:.3f},{outward.z:.3f})")

    edge_mid = (e.verts[0].co + e.verts[1].co) * 0.5
    plane_co = edge_mid + outward * distance
    plane_no = outward.copy()

    geom = list(host.verts) + list(host.edges) + [host]

    try:
        result = bmesh.ops.bisect_plane(
            bm,
            geom=geom,
            dist=1e-4,
            plane_co=plane_co,
            plane_no=plane_no,
            clear_inner=False,
            clear_outer=False,
        )
    except Exception as exc:
        print(f"[loop_edge bisect] failed: {exc!r}")
        return False

    new_verts = [g for g in result.get('geom_cut', [])
                 if isinstance(g, bmesh.types.BMVert) and g.is_valid]
    if trace: print(f"[trace {trace}] _cut_via_bisect: bisect_plane returned {len(new_verts)} new verts")
    if len(new_verts) < 1:
        return False

    # v20.2.5: validate bisect result. Two failure modes to catch:
    # (A) Off-plane: new vert lands far from cut plane (perpendicular offset
    #     should ≈ distance). Happens on non-planar hosts.
    # (B) Far-along: new vert is on the cut plane geometrically but FAR
    #     ALONG the source edge axis — host's side boundary was very long
    #     and the plane intersected it deep inside. Visible as a long
    #     stretchy face going from source corner to the bisect vert.
    # Either failure mode → reject and let boundary-split fallback handle.
    v1, v2 = e.verts[0], e.verts[1]
    edge_mid = (v1.co + v2.co) * 0.5
    edge_dir = (v2.co - v1.co).normalized() if (v2.co - v1.co).length > 1e-9 else None
    PERPENDICULAR_TOL = max(0.05, distance * 0.5)
    # Max acceptable distance from source corner to bisect vert.
    # Bisect vert should be "near" v1 or v2; allow up to 2.5 × distance.
    PROXIMITY_MAX = distance * 2.5

    bad_bisect = False
    bad_reason = ""
    for nv in new_verts:
        # (A) Off-plane check
        offset_along = (nv.co - edge_mid).dot(outward)
        if abs(offset_along - distance) > PERPENDICULAR_TOL:
            bad_bisect = True
            bad_reason = f"off-plane offset_along={offset_along:.3f} expected≈{distance:.3f} tol={PERPENDICULAR_TOL:.3f}"
            break
        # (B) Far-along check — must be within proximity of v1 or v2
        d1 = (nv.co - v1.co).length
        d2 = (nv.co - v2.co).length
        if min(d1, d2) > PROXIMITY_MAX:
            bad_bisect = True
            bad_reason = f"far-along min(d1={d1:.3f},d2={d2:.3f})>{PROXIMITY_MAX:.3f}"
            break

    if bad_bisect:
        if trace: print(f"[trace {trace}] _cut_via_bisect: BAD bisect → {bad_reason}")
        try:
            bmesh.ops.delete(bm,
                             geom=[v for v in new_verts if v.is_valid],
                             context='VERTS')
        except Exception as exc:
            print(f"[loop_edge bisect] cleanup failed: {exc!r}")
        return False

    for nv in new_verts:
        d1 = (nv.co - v1.co).length
        d2 = (nv.co - v2.co).length
        src_v = v1 if d1 < d2 else v2
        # v20.3.5: tag with host so Phase 4 can detect cross-face corners
        created_with_src.append((nv, src_v, host))

    return True


_EOVC_TRACE_KEYS = {
    # TestCorner failing edges (debug trace) — remove after bug fixed
    (681, 682), (682, 681),
    (771, 682), (682, 771),
    (1041, 771), (771, 1041),
    (707, 1041), (1041, 707),
}


def _trace_for(e):
    try:
        a, b = e.verts[0].index, e.verts[1].index
    except Exception:
        return None
    k1 = (a, b)
    k2 = (b, a)
    if k1 in _EOVC_TRACE_KEYS:
        return k1
    if k2 in _EOVC_TRACE_KEYS:
        return k2
    return None


def _cut_one_edge_per_face(bm, e, distance, flip, created_with_src, target_set=None, explicit_host=None):
    """v20.3.0: outward-aligned boundary selection.

    For each source corner (v1, v2), we pick the boundary edge of host face
    most aligned with outward direction (highest dot product). This naturally
    rejects boundaries going off-plane (e.g., curb-wall lip going up in Z
    while outward is horizontal). Standard CAD/mesh-offset technique.

    If no acceptable boundary at either corner → skip this source edge
    (no spike, no fallback — clean refusal)."""
    trace = _trace_for(e)
    host, other = _pick_host_other(e, target_set, flip, explicit_host)
    if host is None:
        if trace: print(f"[trace {trace}] _cut_one_edge_per_face: NO HOST")
        return False
    # v20.4.3: if host was already switched (flip consumed there), don't
    # double-flip outward; otherwise apply flip to negate outward direction.
    flip_for_outward = flip and not _eovc_flip_state.get(id(e), False)
    outward = _compute_outward(e, host, other, flip_for_outward)
    if outward is None:
        if trace: print(f"[trace {trace}] _cut_one_edge_per_face: NO OUTWARD (host=#{host.index})")
        return False
    if trace: print(f"[trace {trace}] _cut_one_edge_per_face: host=#{host.index} outward=({outward.x:.3f},{outward.y:.3f},{outward.z:.3f})")

    v1, v2 = e.verts[0], e.verts[1]

    # Pick best-aligned boundary at each corner
    e_left, dot_left = _find_outward_boundary(host, v1, e, outward, target_set=target_set)
    e_right, dot_right = _find_outward_boundary(host, v2, e, outward, target_set=target_set)
    if trace:
        l = f"#{e_left.index}" if e_left else "None"
        r = f"#{e_right.index}" if e_right else "None"
        print(f"[trace {trace}] boundary v1=#{v1.index}→{l}(dot={dot_left:.3f}) v2=#{v2.index}→{r}(dot={dot_right:.3f})")
    if e_left is None or e_right is None:
        # No good boundary at one corner (chain endpoint, irregular face).
        # v20.3.7: fall back to bisect-plane which doesn't need alignned
        # boundaries — it picks intersection points naturally.
        if trace: print(f"[trace {trace}] → fallback to _cut_via_bisect (no boundary)")
        return _cut_via_bisect(bm, e, distance, flip, created_with_src, target_set, explicit_host)

    # Both boundaries must be long enough for the offset
    long_l = _is_boundary_long_enough(e_left, v1, outward, distance)
    long_r = _is_boundary_long_enough(e_right, v2, outward, distance)
    if not (long_l and long_r):
        if trace: print(f"[trace {trace}] boundary too short L={long_l} R={long_r} → fallback to _cut_via_bisect")
        # Try bisect-plane fallback (handles warped hosts that boundary can't)
        return _cut_via_bisect(bm, e, distance, flip, created_with_src, target_set, explicit_host)

    nv1 = _insert_offset_vert(bm, e_left, v1, outward, distance)
    if nv1 is None:
        if trace: print(f"[trace {trace}] _insert_offset_vert v1 FAILED → fallback to _cut_via_bisect")
        return _cut_via_bisect(bm, e, distance, flip, created_with_src, target_set, explicit_host)
    nv2 = _insert_offset_vert(bm, e_right, v2, outward, distance)
    if nv2 is None:
        if trace: print(f"[trace {trace}] _insert_offset_vert v2 FAILED")
        return False  # nv1 is stray, but at least no spike

    created_with_src.append((nv1, v1, host))
    created_with_src.append((nv2, v2, host))

    fs = bmesh.utils.face_split(host, nv1, nv2)
    ok = fs is not None and fs[0] is not None
    if trace: print(f"[trace {trace}] face_split result={ok}")
    return ok


def _corner_outward(incident, target_set, flip, explicit_host):
    """Averaged outward direction at a source vert (same convention as
    Phase 1). Returns (unit_avg, miter_scale) or (None, 1.0).

    miter_scale = 1 / |mean(outward_i)| — how much farther than `distance`
    the offset must sit along `unit_avg` so it is `distance` away from EVERY
    incident source line (true miter: 90° corner → ×1.414)."""
    acc = Vector((0.0, 0.0, 0.0))
    n = 0
    for src_e in incident:
        h, o = _pick_host_other(src_e, target_set, flip, explicit_host)
        # v20.15.1: same flip rule as the per-edge path — if flip already
        # switched the host, don't negate again (old code always negated,
        # so Phase 1 searched the wrong side whenever flip changed host).
        flip_for_outward = flip and not _eovc_flip_state.get(id(src_e), False)
        outward = _compute_outward(src_e, h, o, flip_for_outward)
        if outward is not None:
            acc += outward
            n += 1
    if n == 0 or acc.length < 1e-8:
        return None, 1.0
    mean_len = acc.length / n
    return acc.normalized(), 1.0 / max(mean_len, 0.35)


def _dir_in_corner_wedge(face, v, direction):
    """True if `direction` from `v` points into `face`'s corner at `v`
    (angular test in the face plane — robust to non-planar quads, unlike a
    point-in-polygon probe)."""
    lp = next((l for l in face.loops if l.vert is v), None)
    if lp is None:
        return False
    n = face.normal
    if n.length < 1e-8:
        return False
    n = n.normalized()

    def flat(vec):
        return vec - n * vec.dot(n)

    d_next = flat(lp.link_loop_next.vert.co - v.co)
    d_prev = flat(lp.link_loop_prev.vert.co - v.co)
    d = flat(direction)
    if min(d_next.length, d_prev.length, d.length) < 1e-9:
        return False
    # Face loops wind CCW around n: interior is from d_next rotating to d_prev.
    a_wedge = math.atan2(d_next.cross(d_prev).dot(n), d_next.dot(d_prev)) % (2 * math.pi)
    a_dir = math.atan2(d_next.cross(d).dot(n), d_next.dot(d)) % (2 * math.pi)
    return 1e-6 < a_dir < a_wedge - 1e-6


def _collect_corner_fan(v, e_a, e_b, host_a, host_b, target_set,
                        max_faces=8, min_normal_dot=0.995):
    """Walk around `v` from host_a (starting past e_a) across non-source
    edges until e_b. Returns the list of faces if every face on the way is a
    near-coplanar floor face (≤ ~6° apart), the walk ends in host_b and the
    fan has 3+ faces around a real corner (not a ~180° straight run).
    Read-only — the caller joins."""
    ref_n = host_a.normal.normalized()
    fan = [host_a]
    f = host_a
    prev = e_a
    for _ in range(max_faces):
        nxt = next((ed for ed in f.edges
                    if v in ed.verts and ed is not prev), None)
        if nxt is None:
            return None
        if nxt is e_b:
            break
        if nxt in target_set or len(nxt.link_faces) != 2:
            return None
        f = next(lf for lf in nxt.link_faces if lf is not f)
        if f in fan:
            return None
        if abs(f.normal.z) <= 0.5 or f.normal.normalized().dot(ref_n) < min_normal_dot:
            return None
        fan.append(f)
        prev = nxt
    else:
        return None
    # 2 faces share an edge through v → Phase 1 already puts the offset on
    # it. Only 3+ faces leave the corner without a common boundary.
    if f is not host_b or len(fan) < 3:
        return None
    # Any sweep qualifies: inner corner (<180°), outer corner (>180°) and
    # a straight run (~180°) whose far side is split into 3+ faces — there
    # the split runs perpendicular and the offset sits at plain `distance`.
    return fan


def _split_trapped_corner(bm, v, incident, target_set, flip, explicit_host, distance):
    """v20.15.0: Phase 0 for a "trapped" corner — a source vert where two
    source edges meet and the host face has NO other edge through v (e.g.
    floor quad whose corner is bounded by two walls). Phase 1 cannot place
    an offset there, and the per-edge fallback used to put each strip's
    offset on the neighbouring source edge → strips overlap into a square.

    Fix: cut the host from v along the corner bisector to its far boundary.
    That diagonal is a regular non-source edge, so Phase 1 then inserts the
    single miter offset on it and the two strips meet in a clean corner line.
    Source edges are never split. Returns (far_vert, was_edge_split) if the
    host was split, else None — caller trims the diagonal tail afterwards."""
    if len(incident) != 2:
        return None
    avg, scale = _corner_outward(incident, target_set, flip, explicit_host)
    if avg is None:
        return None
    e_a, e_b = incident
    picked = {}
    for src_e in incident:
        h, _o = _pick_host_other(src_e, target_set, flip, explicit_host)
        picked[src_e] = (h, _eovc_flip_state.get(id(src_e), False))
    if picked[e_a][0] is None or picked[e_b][0] is None:
        return None
    if picked[e_a][0] is not picked[e_b][0]:
        # v20.15.1: corner interior is a FAN of faces (each source edge sits
        # on its own host, non-source edges between them through v). No
        # single face can hold the miter offset → strips never stitched →
        # visible gap. Merge the fan into one face first (only if it's flat
        # floor), then it's an ordinary trapped corner.
        fan = _collect_corner_fan(v, e_a, e_b, picked[e_a][0], picked[e_b][0], target_set)
        if fan is None:
            return None
        if not any(_dir_in_corner_wedge(f, v, avg) for f in fan):
            return None  # bisector doesn't go into the fan
        try:
            joined = bmesh.utils.face_join(fan, True)
        except Exception as exc:
            print(f"[loop_edge phase0] fan join failed at v#{v.index}: {exc!r}")
            return None
        if joined is None:
            return None
        print(f"[loop_edge phase0] joined {len(fan)}-face fan at corner v#{v.index}")
        picked = {src_e: (joined, state) for src_e, (_h, state) in picked.items()}
    host = picked[e_a][0]
    host_edges = set(host.edges)
    if e_a not in host_edges or e_b not in host_edges:
        return None
    if any(ed not in target_set for ed in v.link_edges if ed in host_edges):
        return None  # host already has a boundary through v — Phase 1 copes
    if not _dir_in_corner_wedge(host, v, avg):
        return None  # bisector points away from the host (flip side)

    basis = _edgenet_face_basis(host)
    if basis is None:
        return None
    anchor, _n, u, w, _verts = basis
    o2 = _edgenet_project_2d(v.co, anchor, u, w)
    d2 = (avg.dot(u), avg.dot(w))
    dl = (d2[0] ** 2 + d2[1] ** 2) ** 0.5
    if dl < 1e-6:
        return None
    d2 = (d2[0] / dl, d2[1] / dl)

    # Nearest hit of the bisector ray with a host edge not touching v.
    best = None  # (t_ray, edge, s_on_edge)
    for ed in host.edges:
        if v in ed.verts:
            continue
        a2 = _edgenet_project_2d(ed.verts[0].co, anchor, u, w)
        b2 = _edgenet_project_2d(ed.verts[1].co, anchor, u, w)
        sx, sy = b2[0] - a2[0], b2[1] - a2[1]
        denom = _edgenet_cross_2d(d2, (sx, sy))
        if abs(denom) < 1e-9:
            continue
        qx, qy = a2[0] - o2[0], a2[1] - o2[1]
        t = _edgenet_cross_2d((qx, qy), (sx, sy)) / denom
        s = _edgenet_cross_2d((qx, qy), d2) / denom
        if t <= 1e-6 or s < -1e-6 or s > 1.0 + 1e-6:
            continue
        if best is None or t < best[0]:
            best = (t, ed, s)
    if best is None:
        return None
    t_ray, far_edge, s = best
    # Need room for the miter offset on the new diagonal (plus margin).
    if t_ray <= distance * scale * 1.05:
        return None

    va, vb = far_edge.verts
    was_split = False
    SNAP = 1e-3
    seg_len = (vb.co - va.co).length
    if s * seg_len <= SNAP:
        target = va
    elif (1.0 - s) * seg_len <= SNAP:
        target = vb
    else:
        if far_edge in target_set:
            return None  # never split a source edge
        _ne, target = bmesh.utils.edge_split(far_edge, va, s)
        was_split = True
    if target is v or any(ed.other_vert(v) is target for ed in v.link_edges):
        return None
    try:
        res = bmesh.utils.face_split(host, v, target)
    except Exception as exc:
        print(f"[loop_edge phase0] face_split failed at v#{v.index}: {exc!r}")
        return None
    if res is None or res[0] is None:
        return None
    halves = (host, res[0])
    for src_e, (h, state) in picked.items():
        if h is host:
            h = next((f for f in halves if f in src_e.link_faces), None)
        if h is not None:
            _eovc_host_override[src_e] = (h, state)
    return target, was_split


def _trim_corner_diagonal(bm, v, far_v, was_split):
    """v20.15.0: after the cut, the Phase 0 diagonal beyond the miter
    offset (offset → far boundary) is just helper topology. Dissolve it so
    only the corner line source → miter stays, then dissolve the far vert
    if Phase 0 created it on a straight boundary."""
    if not (_vert_is_valid(v) and _vert_is_valid(far_v)):
        return
    axis = far_v.co - v.co
    if axis.length < 1e-9:
        return
    axis.normalize()
    tail = None
    for ed in far_v.link_edges:
        x = ed.other_vert(far_v)
        if x is v:
            return  # no offset landed on the diagonal — keep it as is
        rel = x.co - v.co
        if rel.length < 1e-9:
            continue
        if rel.normalized().dot(axis) > 0.9999:
            tail = ed
            break
    if tail is None or len(tail.link_faces) != 2:
        return
    try:
        bmesh.ops.dissolve_edges(bm, edges=[tail], use_verts=False)
    except Exception as exc:
        print(f"[loop_edge phase0] diagonal trim failed: {exc!r}")
        return
    if was_split and _vert_is_valid(far_v) and len(far_v.link_edges) == 2:
        try:
            bmesh.ops.dissolve_verts(bm, verts=[far_v])
        except Exception as exc:
            print(f"[loop_edge phase0] far vert dissolve failed: {exc!r}")


def _do_loop_cut(bm, target_edges, distance, flip, explicit_host=None):
    """v13 algorithm — shared boundary edge + post-cut weld by source vert.

      Phase 1: For each unique source vert v, find a non-source boundary edge
        through v shared by 2+ incident hosts. Insert ONE offset vert there
        → smooth miter when shared edge exists.
      Phase 2: face_split using Phase 1's offsets.
      Phase 3: Per-edge fallback for skipped edges. Tracks offsets per source
        vert so we can stitch them later.
      Phase 4: Weld offset verts that originated from the SAME source vert.
        Uses remove_doubles WITHOUT moving positions — kept vert keeps its
        boundary-edge position so faces don't deform. The deleted offsets'
        face references rewire to the kept vert → loop becomes continuous."""
    _eovc_host_override.clear()
    try:
        return _do_loop_cut_impl(bm, target_edges, distance, flip, explicit_host)
    finally:
        _eovc_host_override.clear()


def _do_loop_cut_impl(bm, target_edges, distance, flip, explicit_host=None):
    target_set = set(target_edges)
    created_with_src = []  # [(offset_vert, source_vert_index), ...] for Phase 4 weld
    _eovc_flip_state.clear()  # v20.4.3: reset per-edge flip tracking

    vert_to_targets = {}
    for e in target_edges:
        for v in e.verts:
            vert_to_targets.setdefault(v, []).append(e)

    # v20.14.13 added a Phase 0 diagonal pre-split for "trapped" chain
    # corners (vert with only source edges through it in some host).
    # Reverted in v20.14.14 — it caused regressions where Phase 1 didn't
    # insert an offset for chain endpoint verts (e.g. v#681 in user's
    # building corner test), leaving the strip cut short. The cause:
    # face_split during Phase 0 can invalidate edge references that
    # Phase 1 then re-fetches under a different topology, so the
    # boundary candidate that Phase 1 found before Phase 0 may no longer
    # be the same edge. Cleaner approach is leaving the host topology
    # alone here; trapped-corner cases need the user to manually add a
    # diagonal in source, or use BEVEL on those specific sets.
    #
    # v20.15.0: Phase 0 is back in a narrow, safe form — it only touches
    # trapped corners (2 source edges, same host, no other host edge through
    # the vert), never splits a source edge, and runs before Phase 1 looks
    # up any boundary, so no stale references. See _split_trapped_corner.
    corner_splits = []  # [(source_vert, far_vert, was_edge_split), ...]
    for v, incident in vert_to_targets.items():
        try:
            res = _split_trapped_corner(bm, v, incident, target_set, flip,
                                        explicit_host, distance)
            if res is not None:
                corner_splits.append((v, res[0], res[1]))
        except Exception as exc:
            print(f"[loop_edge phase0] corner v#{v.index} failed: {exc!r}")
    if corner_splits:
        print(f"[loop_edge phase0] split {len(corner_splits)} trapped corners along bisector")
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

    # ---- Phase 1: insert ONE offset vert per source vert ----
    offset_for_vert = {}  # source_vert -> offset_vert
    for v, incident in vert_to_targets.items():
        # Averaged outward direction across incident source edges.
        # v20.15.0: plus miter scale so a corner offset is `distance` from
        # BOTH source lines (was only `distance` along the bisector →
        # strips pinched at every corner).
        avg, miter_scale = _corner_outward(incident, target_set, flip, explicit_host)
        if avg is None:
            continue

        # v20.3: pick boundary candidates aligned with avg outward.
        # Score each candidate by dot(unit_dir, avg). Reject negative dots
        # (= edge points opposite outward) and very low dots (off-plane).
        candidates = []  # (edge, dot, host_count)
        edge_freq = {}
        for src_e in incident:
            host, _ = _pick_host_other(src_e, target_set, flip, explicit_host)
            if host is None:
                continue
            for ed in host.edges:
                if ed in target_set or ed is src_e:
                    continue
                if v not in ed.verts:
                    continue
                ov = ed.other_vert(v)
                if ov is None:
                    continue
                ed_dir = (ov.co - v.co)
                if ed_dir.length < 1e-9:
                    continue
                dot = ed_dir.normalized().dot(avg)
                if dot < 0.3:  # off-plane / wrong-direction
                    continue
                edge_freq[ed] = edge_freq.get(ed, 0) + 1
        if not edge_freq:
            continue
        # Pick edge with highest dot · frequency (= aligned + shared across hosts)
        def _score(ed):
            ov = ed.other_vert(v)
            d = (ov.co - v.co).normalized()
            return d.dot(avg) * edge_freq[ed]
        best_edge = max(edge_freq.keys(), key=_score)

        nv = _insert_offset_vert(bm, best_edge, v, avg, distance * miter_scale)
        if nv is not None:
            offset_for_vert[v] = nv
            # Phase 1 doesn't have a clear single host (shared across multiple
            # incidents) — use None as marker.
            created_with_src.append((nv, v, None))

    # ---- Phase 2: face_split using shared offsets ----
    cut_count = 0
    edges_done = set()
    for src_e in target_edges:
        try:
            if not src_e.is_valid:
                continue
        except ReferenceError:
            continue
        v1, v2 = src_e.verts[0], src_e.verts[1]
        keys = (v1.index, v2.index)
        nv1 = offset_for_vert.get(v1)
        nv2 = offset_for_vert.get(v2)
        if nv1 is None or nv2 is None:
            print(f"[loop_edge phase2] edge {keys} SKIP — Phase1 missing offset(s) v1={'OK' if nv1 else 'MISS'} v2={'OK' if nv2 else 'MISS'}")
            continue
        # Find host face containing source edge + both offsets
        host = None
        for h in src_e.link_faces:
            try:
                if nv1 in h.verts and nv2 in h.verts:
                    host = h
                    break
            except ReferenceError:
                continue
        if host is None:
            print(f"[loop_edge phase2] edge {keys} SKIP — no host contains both offsets (link_faces={[f.index for f in src_e.link_faces]})")
            continue
        try:
            fs = bmesh.utils.face_split(host, nv1, nv2)
            if fs is not None and fs[0] is not None:
                cut_count += 1
                edges_done.add(src_e)
                print(f"[loop_edge phase2] edge {keys} CUT host=#{host.index}")
            else:
                print(f"[loop_edge phase2] edge {keys} face_split returned None host=#{host.index}")
        except Exception as exc:
            print(f"[loop_edge phase2] face_split failed: {exc!r}")

    # ---- Phase 3: per-edge fallback for skipped edges ----
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    for src_e in target_edges:
        if src_e in edges_done:
            continue
        try:
            if not src_e.is_valid:
                continue
        except ReferenceError:
            continue
        try:
            keys = (src_e.verts[0].index, src_e.verts[1].index)
            ok = _cut_one_edge_per_face(bm, src_e, distance, flip, created_with_src, target_set, explicit_host)
            print(f"[loop_edge phase3] edge {keys} → {'CUT' if ok else 'FAIL'}")
            if ok:
                cut_count += 1
        except Exception as exc:
            print(f"[loop_edge phase3] fallback EXC for {keys}: {exc!r}")

    # ---- Phase 4: weld offsets sharing source vert via explicit targetmap ----
    # v20.3.6: reverted to plain weld (no mitre move) — moving verts before
    # weld broke face planarity → some faces collapsed → missing geometry.
    src_to_offsets = {}
    for entry in created_with_src:
        # Tuple may be (nv, src_v) legacy or (nv, src_v, host) v20.3.5+
        if len(entry) == 3:
            nv, src_v, _host = entry
        else:
            nv, src_v = entry
        try:
            if not nv.is_valid or not src_v.is_valid:
                continue
        except ReferenceError:
            continue
        src_to_offsets.setdefault(src_v, []).append(nv)

    targetmap = {}
    for src_v, offsets in src_to_offsets.items():
        valid = [v for v in offsets if v.is_valid]
        if len(valid) <= 1:
            continue
        keep = valid[0]
        for v in valid[1:]:
            if v is not keep:
                targetmap[v] = keep

    if targetmap:
        try:
            bmesh.ops.weld_verts(bm, targetmap=targetmap)
        except Exception as exc:
            print(f"[loop_edge phase4] weld_verts failed: {exc!r}")

    # ---- Phase 5: close-pair weld for missed corner stitches ----
    # When two adjacent source edges share a corner v_c and each cut produced
    # an offset vert near v_c, those should weld via Phase 4. But edge cases
    # (bisect single-vert results, tagging mismatches) leave orphans; this
    # post-pass merges close pairs near same corner.
    bm.verts.ensure_lookup_table()
    PAIR_TOL = distance * 1.0
    CORNER_RADIUS = distance * 1.8

    offsets_alive = []
    for entry in created_with_src:
        nv = entry[0]
        try:
            if nv.is_valid:
                offsets_alive.append(nv)
        except ReferenceError:
            pass
    offsets_alive = list({id(v): v for v in offsets_alive}.values())

    corner_verts = [v for v, incident in vert_to_targets.items()
                    if v.is_valid and len(incident) >= 2]

    post_targetmap = {}
    for c in corner_verts:
        nearby_offsets = [o for o in offsets_alive
                          if o.is_valid and (o.co - c.co).length < CORNER_RADIUS]
        if len(nearby_offsets) < 2:
            continue
        nearby_offsets.sort(key=lambda v: v.index)
        for i in range(len(nearby_offsets)):
            for j in range(i + 1, len(nearby_offsets)):
                a, b = nearby_offsets[i], nearby_offsets[j]
                if not a.is_valid or not b.is_valid:
                    continue
                if any(e.other_vert(a) is b for e in a.link_edges):
                    continue
                if (a.co - b.co).length < PAIR_TOL:
                    keep = a
                    drop = b
                    while keep in post_targetmap:
                        keep = post_targetmap[keep]
                    if drop is not keep and drop not in post_targetmap:
                        post_targetmap[drop] = keep

    if post_targetmap:
        try:
            bmesh.ops.weld_verts(bm, targetmap=post_targetmap)
            print(f"[loop_edge phase5] welded {len(post_targetmap)} orphan corner offsets")
        except Exception as exc:
            print(f"[loop_edge phase5] post-weld failed: {exc!r}")

    # ---- Phase 6 (v20.4): chain-end taper — collapse last offset to source endpoint ----
    # User wants the cut chain to "close" with a diagonal taper, not just a
    # perpendicular cap. Achieved by welding the chain-end offset vert INTO
    # the source endpoint. The strip face becomes a triangle = visible taper.
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()

    offsets_alive_set = set()
    for entry in created_with_src:
        nv = entry[0]
        try:
            if nv.is_valid:
                offsets_alive_set.add(nv)
        except ReferenceError:
            pass

    cap_targetmap = {}
    CAP_RADIUS = distance * 4.0
    for src_v, incident in vert_to_targets.items():
        try:
            if not src_v.is_valid:
                continue
        except ReferenceError:
            continue
        if len(incident) != 1:
            continue
        closest = None
        closest_d = CAP_RADIUS
        for nv in offsets_alive_set:
            if not nv.is_valid or nv is src_v:
                continue
            d = (nv.co - src_v.co).length
            if d < closest_d:
                closest_d = d
                closest = nv
        if closest is None:
            continue
        if closest in cap_targetmap:
            continue
        cap_targetmap[closest] = src_v

    if cap_targetmap:
        try:
            bmesh.ops.weld_verts(bm, targetmap=cap_targetmap)
            print(f"[loop_edge phase6] tapered {len(cap_targetmap)} chain endpoints")
        except Exception as exc:
            print(f"[loop_edge phase6] taper weld failed: {exc!r}")

    # ---- Phase 7 (v20.15.0): drop Phase 0 diagonal tails past the miter ----
    for v, far_v, was_split in corner_splits:
        _trim_corner_diagonal(bm, v, far_v, was_split)
    if corner_splits:
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

    return cut_count


# ---------------------------------------------------------------------------
# v20.7 — Mitered (vertex-centric) loop cut
# ---------------------------------------------------------------------------
# Inspired by classical 2D polygon-offset / straight-skeleton theory:
#   At a corner shared by two source edges with outward normals n1, n2,
#   the mitered offset point sits along the angular bisector at
#       miter = corner + (n1 + n2) * d / (1 + n1 · n2)
# That formula gives perpendicular distance d from BOTH source lines
# simultaneously. Per-edge cutting can't achieve that — it places one
# offset per edge on different boundaries → fan / weld mess at non-aligned
# corners. The vertex-centric pass commits to ONE miter target per corner,
# performs the cut per source edge using boundary-projection, then
# physically SNAPS all sibling offsets to the true miter target before
# welding them into one vert. Visual L-shape miter, even when the host's
# boundary edges aren't aligned with the bisector.
#
# References:
#   CGAL Straight Skeleton & Polygon Offsetting (mitered offset theory)
#   https://doc.cgal.org/latest/Straight_skeleton_2/index.html
#   Cacciola — A Survey of Polygon Offsetting Strategies
#   http://fcacciola.50webs.com/Offseting%20Methods.htm
#   Held — Computing Mitered Offset Curves Based on Straight Skeletons
#   https://www.cad-journal.net/files/vol_12/CAD_12(4)_2015_414-424.pdf
#
def _do_loop_cut_mitered(bm, target_edges, distance, flip, explicit_host=None):
    target_set = set(target_edges)
    _eovc_flip_state.clear()

    # Build vert→incident map
    vert_to_targets = {}
    for e in target_edges:
        for v in e.verts:
            vert_to_targets.setdefault(v, []).append(e)

    # Per-edge host + outward.
    # v20.13: flip into _pick_host_other so it can pick F2 as host when
    # the strip should live on the opposite side. _compute_outward's
    # flip arg is gated by _eovc_flip_state to avoid double-negation
    # when host swap already absorbed the flip.
    edge_host = {}
    edge_outward = {}
    for e in target_edges:
        h, o = _pick_host_other(e, target_set, flip, explicit_host)
        if h is None:
            continue
        flip_for_outward = flip and not _eovc_flip_state.get(id(e), False)
        out = _compute_outward(e, h, o, flip_for_outward)
        if out is None:
            continue
        edge_host[e] = h
        for v in e.verts:
            edge_outward[(e, v)] = out

    # Phase A — miter target per corner (true 3D position)
    miter_target = {}
    for v, incident in vert_to_targets.items():
        outs = [edge_outward[(e, v)] for e in incident if (e, v) in edge_outward]
        if not outs:
            continue
        if len(outs) == 1:
            miter_target[v] = v.co + outs[0] * distance
        elif len(outs) == 2:
            n1, n2 = outs
            denom = 1.0 + n1.dot(n2)
            if denom < 0.1:
                # near-180° (straight or reflex) — clamp to 2× distance
                miter_target[v] = v.co + (n1 + n2).normalized() * distance * 2.0
            else:
                miter_target[v] = v.co + (n1 + n2) * (distance / denom)
        else:
            # 3+ incident: use averaged direction at perpendicular distance
            avg = Vector((0.0, 0.0, 0.0))
            for n in outs:
                avg += n
            if avg.length < 1e-8:
                continue
            avg.normalize()
            miter_target[v] = v.co + avg * distance

    # Phase B0 — diagonal pre-split for L-corners trapped in 4-vert host
    # quads. v20.13: relaxed condition. Original v20.7 only fired when
    # both incident edges shared the SAME host (h1 == h2). With flip-aware
    # host swap, hosts often differ per edge, so the original check never
    # fired and B1 had no boundaries to split. Now we walk every face the
    # corner participates in: if a 4-vert host quad has the corner with
    # only source edges through it (= trapped), insert the diagonal to
    # the opposite quad vertex. The bisector at v aligns with that
    # diagonal, so B1 picks it up cleanly.
    for v, incident in list(vert_to_targets.items()):
        if len(incident) < 2:
            continue
        try:
            link_faces = list(v.link_faces)
        except ReferenceError:
            continue
        for host in link_faces:
            try:
                host_verts = list(host.verts)
            except ReferenceError:
                continue
            if len(host_verts) != 4:
                continue
            # Count NON-source edges of host through v
            try:
                v_edges_in_host = [ed for ed in host.edges if v in ed.verts]
            except ReferenceError:
                continue
            non_src_through_v = sum(1 for ed in v_edges_in_host
                                    if ed not in target_set)
            if non_src_through_v > 0:
                continue  # already has a non-source boundary, no diagonal needed
            # Find opposite corner of this quad (vert not v, not adjacent to v
            # via any edge of host)
            adj_verts = {ed.other_vert(v) for ed in v_edges_in_host
                         if ed.other_vert(v) is not None}
            opposite = next((vv for vv in host_verts
                             if vv is not v and vv not in adj_verts), None)
            if opposite is None:
                continue
            # Skip if already directly linked
            already_linked = any(ed.other_vert(v) is opposite
                                 for ed in v.link_edges)
            if already_linked:
                continue
            try:
                bmesh.utils.face_split(host, v, opposite)
            except Exception as exc:
                print(f"[mitered B0] diagonal split failed: {exc!r}")

    # B0 may have invalidated stored host references for some edges →
    # re-pick host for each source edge from current bm state.
    # v20.13: pass flip to _pick_host_other so for flip=True it picks
    # the OPPOSITE adjacent face (F2) as the strip's home. This is
    # essential for MITERED — boundary-edge search in Phase B1 looks at
    # host's edges; if host=F1 but outward points into F2, every
    # non-source boundary fails score>0 (they all go INTO F1, outward
    # goes OUT of it). Result: 0 cuts. Pre-v20.13 worked because Phase
    # B3 fallback covered the gap; B3 was removed to kill bleed/fan.
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    edge_host.clear()
    edge_outward.clear()
    for e in target_edges:
        try:
            if not e.is_valid:
                continue
        except ReferenceError:
            continue
        h, o = _pick_host_other(e, target_set, flip, explicit_host)
        if h is None:
            continue
        # _pick_host_other already accounts for flip via host swap →
        # _compute_outward gets flip=False to avoid double-negating.
        flip_for_outward = flip and not _eovc_flip_state.get(id(e), False)
        out = _compute_outward(e, h, o, flip_for_outward)
        if out is None:
            continue
        edge_host[e] = h
        for v in e.verts:
            edge_outward[(e, v)] = out

    # Phase B1 — GLOBALLY insert ONE offset vert per corner BEFORE any
    # face_split. Per-edge code does the same: face_split would invalidate
    # neighbouring host faces and break subsequent edge_splits at shared
    # corners, leaving most cuts unsplit (= big painted blobs).
    created_with_src = []  # [(offset_vert, source_vert, host_face), ...]
    offset_for_vert = {}   # source_vert -> single offset vert (global)

    def _best_boundary_for_vert(v, target_dir, target_len, hosts_to_try):
        """Among the boundary edges of `hosts_to_try` going through v, pick
        the one most aligned with target_dir AND with enough length to
        host a projection. Returns (edge, t_proj, ed_len) or (None, 0, 1).

        Dedup uses BMEdge.index (NOT id()) — Python recycles id() values
        for transient CAPI wrappers, which produced false-positive dup hits
        and silently rejected the only valid candidate."""
        best_edge = None
        best_score = 0.1
        best_t = 0.0
        best_len = 1.0
        seen_indices = set()
        for host in hosts_to_try:
            try:
                host_edges = list(host.edges)
            except ReferenceError:
                continue
            for ed in host_edges:
                if ed.index in seen_indices:
                    continue
                seen_indices.add(ed.index)
                if ed in target_set:
                    continue
                if v not in ed.verts:
                    continue
                ov = ed.other_vert(v)
                if ov is None:
                    continue
                ed_vec = ov.co - v.co
                ed_len = ed_vec.length
                if ed_len < 1e-9:
                    continue
                ed_dir = ed_vec / ed_len
                score = ed_dir.dot(target_dir / target_len)
                if score <= best_score:
                    continue
                t_proj = target_dir.dot(ed_dir)
                if t_proj < 1e-6 or t_proj > ed_len * 0.99:
                    continue
                best_score = score
                best_edge = ed
                best_t = t_proj
                best_len = ed_len
        return best_edge, best_t, best_len

    b1_success = 0
    b1_no_hosts = 0
    b1_no_boundary = 0
    b1_split_fail = 0
    for v, target_pos in miter_target.items():
        try:
            if not v.is_valid:
                continue
        except ReferenceError:
            continue
        target_dir = target_pos - v.co
        target_len = target_dir.length
        if target_len < 1e-9:
            continue
        # Hosts to consider = host faces of incident source edges + ALL link_faces
        # of v. v20.13: include link_faces because after B0 diagonal pre-split
        # the host registry may not reference the post-split triangle that
        # contains the bisector-aligned diagonal.
        hosts = []
        seen_face = set()
        for src_e in vert_to_targets.get(v, []):
            h = edge_host.get(src_e)
            if h is not None and h.index not in seen_face:
                hosts.append(h); seen_face.add(h.index)
        try:
            for f in v.link_faces:
                if f.index not in seen_face and abs(f.normal.z) > 0.5:
                    hosts.append(f); seen_face.add(f.index)
        except ReferenceError:
            pass
        if not hosts:
            b1_no_hosts += 1
            continue
        best_edge, best_t, best_len = _best_boundary_for_vert(
            v, target_dir, target_len, hosts)
        if best_edge is None:
            b1_no_boundary += 1
            continue
        ratio = max(1e-4, min(0.9999, best_t / best_len))
        try:
            _new_edge, new_vert = bmesh.utils.edge_split(best_edge, v, ratio)
            offset_for_vert[v] = new_vert
            created_with_src.append((new_vert, v, hosts[0]))
            b1_success += 1
        except Exception as exc:
            print(f"[mitered] edge_split failed: {exc!r}")
            b1_split_fail += 1
    print(f"[mitered B1] success={b1_success}/{len(miter_target)}  "
          f"no_hosts={b1_no_hosts}  no_boundary={b1_no_boundary}  "
          f"split_fail={b1_split_fail}")

    # Phase B2 — face_split per source edge, track strip faces.
    # Both source-edge endpoints share the same global offset corner so
    # face_split cleanly carves the strip out of the host face. Strip
    # face = the half containing the source edge. The OTHER half is the
    # remainder (untouched mesh interior).
    cut_count = 0
    new_strip_faces = set()
    b2_skip_no_offset = 0
    b2_skip_no_face = 0
    b2_split_fail = 0
    for e in target_edges:
        try:
            if not e.is_valid:
                continue
        except ReferenceError:
            continue
        v1, v2 = e.verts[0], e.verts[1]
        nv1 = offset_for_vert.get(v1)
        nv2 = offset_for_vert.get(v2)
        if nv1 is None or nv2 is None:
            b2_skip_no_offset += 1
            continue
        target_face = None
        try:
            for f in e.link_faces:
                if nv1 in f.verts and nv2 in f.verts:
                    target_face = f
                    break
        except ReferenceError:
            target_face = None
        if target_face is None:
            b2_skip_no_face += 1
            continue
        try:
            fs = bmesh.utils.face_split(target_face, nv1, nv2)
            if fs is not None and fs[0] is not None:
                cut_count += 1
                new_face = fs[0]
                strip = None
                if v1 in new_face.verts and v2 in new_face.verts:
                    strip = new_face
                else:
                    # new_face is the remainder side; locate strip half
                    # via the shared edge between the two halves.
                    try:
                        for adj in nv1.link_faces:
                            if (adj is not new_face and adj.is_valid
                                    and v1 in adj.verts and v2 in adj.verts
                                    and nv2 in adj.verts):
                                strip = adj
                                break
                    except ReferenceError:
                        strip = None
                if strip is not None:
                    new_strip_faces.add(strip)
            else:
                b2_split_fail += 1
        except Exception as exc:
            print(f"[mitered] face_split failed: {exc!r}")
            b2_split_fail += 1

    print(f"[mitered B2] cuts={cut_count}/{len(target_edges)}  "
          f"skip_no_offset={b2_skip_no_offset}  "
          f"skip_no_face={b2_skip_no_face}  split_fail={b2_split_fail}")

    # v20.13: Phase B3 (PER_EDGE per-edge bisect-plane fallback) REMOVED.
    # It was responsible for the visual fan-pattern + paint bleed user
    # complained about: when bevel-style boundary insertion failed for a
    # corner, the bisect-plane fallback created independent offsets per
    # source edge, and the unified painting via `src_verts` extended to
    # the whole untouched host face → big blue blobs. Without B3, source
    # edges that can't get a clean cut are simply skipped (no paint at
    # all in their region) — a graceful degradation. To recover those
    # cases, user can either pre-split the host topology by hand or
    # re-add edges in smaller sub-sets.

    # Phase C — weld sibling offsets at same corner WITHOUT moving them
    # first. Moving before weld breaks face planarity / inverts normals
    # (proven multiple times in v20.3.5/v20.4.0/v20.6.0 attempts). The weld
    # alone keeps the kept vert at its original boundary-projection
    # position and rewires references — already produces a usable miter
    # because B1's projection direction was the bisector. Position fidelity
    # to true miter_target is sacrificed to topological stability.
    bm.verts.ensure_lookup_table()

    src_to_offsets = {}
    for entry in created_with_src:
        nv = entry[0]
        src_v = entry[1]
        try:
            if nv.is_valid and src_v.is_valid:
                src_to_offsets.setdefault(src_v, []).append(nv)
        except ReferenceError:
            continue

    targetmap = {}
    for src_v, offs in src_to_offsets.items():
        valid = [v for v in offs if v.is_valid]
        if len(valid) <= 1:
            continue
        keep = valid[0]
        for v in valid[1:]:
            if v is not keep:
                targetmap[v] = keep
    if targetmap:
        try:
            bmesh.ops.weld_verts(bm, targetmap=targetmap)
        except Exception as exc:
            print(f"[mitered] corner weld failed: {exc!r}")

    # Phase D — chain endpoint taper (mirrors per-edge Phase 6)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()

    offsets_alive_set = set()
    for entry in created_with_src:
        nv = entry[0]
        try:
            if nv.is_valid:
                offsets_alive_set.add(nv)
        except ReferenceError:
            continue

    cap_targetmap = {}
    CAP_RADIUS = distance * 4.0
    for src_v, incident in vert_to_targets.items():
        try:
            if not src_v.is_valid:
                continue
        except ReferenceError:
            continue
        if len(incident) != 1:
            continue
        closest = None
        closest_d = CAP_RADIUS
        for nv in offsets_alive_set:
            if not nv.is_valid or nv is src_v:
                continue
            d = (nv.co - src_v.co).length
            if d < closest_d:
                closest_d = d
                closest = nv
        if closest is None:
            continue
        if closest in cap_targetmap:
            continue
        cap_targetmap[closest] = src_v

    if cap_targetmap:
        try:
            bmesh.ops.weld_verts(bm, targetmap=cap_targetmap)
            print(f"[mitered] tapered {len(cap_targetmap)} chain endpoints")
        except Exception as exc:
            print(f"[mitered] taper weld failed: {exc!r}")

    # v20.13: filter out faces invalidated by Phase C/D welds.
    bm.faces.ensure_lookup_table()
    surviving = set()
    for f in new_strip_faces:
        try:
            if f.is_valid:
                surviving.add(f)
        except ReferenceError:
            continue

    return cut_count, surviving


# ---------------------------------------------------------------------------
# v20.8 — Bevel-based loop cut (uses Blender's native bmesh.ops.bevel)
# ---------------------------------------------------------------------------
# Different semantics vs PER_EDGE/MITERED:
#   PER_EDGE/MITERED keep source edges in place and cut a parallel offset
#   line at perpendicular distance d into the host face — strip width = d,
#   between source and offset.
#   BEVEL replaces each source edge with a strip of width 2*d centred on
#   the original edge position. Source edges no longer exist in the bake;
#   the strip itself IS the painted outline. Native Blender bevel handles
#   miter joins, sharp corners, MiterLimit clamping out of the box.
#
# v20.10 — Flip support via uniform post-bevel shift (Variant B).
#   bmesh.ops.bevel produces a strip centred on the source line spanning
#   [-d, +d]. We then shift every new vert by `outward * d` so the strip
#   becomes one-sided: [0, +2d] in the user-chosen direction. This moves
#   the strip as a rigid block — no per-vert classification, so corner
#   miter quads don't get distorted (the bug that broke v20.9).
#       Flip = False → strip on host side, inner edge AT source line.
#       Flip = True  → strip on opposite side.
#   `outward` is averaged per source corner from incident edges' outward
#   directions. Verts created by bevel between two corners are matched
#   to their nearest source corner via closest-position lookup.
def _do_loop_cut_bevel(bm, target_edges, distance, flip, explicit_host=None):
    target_set = set(target_edges)

    # ----- Pre-bevel snapshot: source corner positions + outward dirs -----
    # Critical: flip ONLY goes to _compute_outward (negates direction).
    # _pick_host_other gets flip=False so it always returns the primary
    # host. Passing flip to BOTH was a v20.10 bug — when multiple valid
    # hosts exist, _pick_host_other swaps to the alternate host, which
    # already inverts the outward; _compute_outward then negates again,
    # double-flip cancels itself, Flip toggle had no visible effect.
    #
    # v20.11: also snapshot host face plane (centroid + normal). After
    # bevel + shift, every new vert is projected onto that plane so the
    # strip stays on the host surface — same behaviour as Blender's Loop
    # Cut: the cut follows the geometry, never raises/lowers it. Without
    # this, bevel verts that sat between host F1 and other F2 (e.g. at
    # curb-wall joints where F1/F2 aren't coplanar) end up floating in
    # mid-air after the shift.
    src_pos = {}             # vert.index -> Vector(co)
    edge_outward = {}        # edge -> outward Vector (one per source edge)
    plane_per_vert = {}      # vert.index -> (point Vector, normal Vector)
    plane_acc = {}           # vert.index -> [(centroid, normal), ...]
    for e in target_edges:
        try:
            if not e.is_valid:
                continue
        except ReferenceError:
            continue
        h, o = _pick_host_other(e, target_set, False, explicit_host)
        if h is None:
            continue
        out = _compute_outward(e, h, o, flip)
        if out is None:
            continue
        try:
            plane_pt = h.calc_center_median()
            plane_n = h.normal.normalized()
        except ReferenceError:
            continue
        edge_outward[e] = out
        for v in e.verts:
            src_pos[v.index] = v.co.copy()
            plane_acc.setdefault(v.index, []).append((plane_pt.copy(), plane_n.copy()))

    # v20.14.6 — chain-walk outward propagation.
    # Earlier per-vert dot-product alignment failed at 90° corners (dot ≈ 0
    # on perpendicular outwards → no flip → averaged outward 90° off true
    # bisector). Symptom: src#73 came out NW while src#77 (same source
    # edge) came out SE. New approach: walk source-edge chain BFS-style
    # and align via HANDEDNESS computed from the SHARED vertex's edge
    # directions. Why "from shared vertex": if we used the bmesh canonical
    # edge_dir (verts[1] - verts[0]), the answer would depend on internal
    # vertex ordering and flip arbitrarily for adjacent edges on the same
    # corner. Computing edge_dir AWAY FROM the shared vert puts both edges
    # on the same coordinate system, so "same handedness" reliably means
    # "same side of the source-edge chain".
    UP = Vector((0.0, 0.0, 1.0))

    def _edge_dir_away_from(e_local, v_local):
        """Direction along e_local pointing AWAY from v_local, normalized."""
        try:
            other = e_local.other_vert(v_local)
            if other is None:
                return None
            ed_vec = other.co - v_local.co
        except ReferenceError:
            return None
        if ed_vec.length < 1e-9:
            return None
        return ed_vec.normalized()

    aligned_edges = set()
    if edge_outward:
        seed = next((e for e in target_edges if e in edge_outward), None)
        if seed is not None:
            aligned_edges.add(seed)
            queue = [seed]
            while queue:
                cur = queue.pop(0)
                # For each shared vertex, walk through neighbour edges and
                # align using handedness measured from that shared vertex.
                for v in cur.verts:
                    cur_dir_from_v = _edge_dir_away_from(cur, v)
                    if cur_dir_from_v is None:
                        continue
                    cur_right_at_v = cur_dir_from_v.cross(UP)
                    if cur_right_at_v.length < 1e-9:
                        continue
                    cur_right_at_v.normalize()
                    cur_side_at_v = edge_outward[cur].dot(cur_right_at_v)

                    for nbr in v.link_edges:
                        if nbr is cur or nbr not in edge_outward:
                            continue
                        if nbr in aligned_edges:
                            continue
                        nbr_dir_from_v = _edge_dir_away_from(nbr, v)
                        if nbr_dir_from_v is None:
                            aligned_edges.add(nbr); queue.append(nbr); continue
                        nbr_right_at_v = nbr_dir_from_v.cross(UP)
                        if nbr_right_at_v.length < 1e-9:
                            aligned_edges.add(nbr); queue.append(nbr); continue
                        nbr_right_at_v.normalize()
                        nbr_side_at_v = edge_outward[nbr].dot(nbr_right_at_v)
                        # IMPORTANT inverted-condition explanation:
                        # When both edge directions are taken AWAY from
                        # shared vertex v, going "along chain" means cur
                        # is INCOMING (chain direction = -dir_from_v) and
                        # nbr is OUTGOING (chain direction = +dir_from_v).
                        # An outward on the same chain side has OPPOSITE
                        # signs in the two frames (e.g. cur side <0 while
                        # nbr side >0). Therefore: SAME sign = outwards
                        # on OPPOSITE chain sides → flip nbr.
                        if abs(cur_side_at_v) > 1e-6 and abs(nbr_side_at_v) > 1e-6:
                            if (cur_side_at_v > 0) == (nbr_side_at_v > 0):
                                edge_outward[nbr] = -edge_outward[nbr]
                        aligned_edges.add(nbr)
                        queue.append(nbr)

    # Now build outward_per_vert by averaging the (now-aligned) per-edge
    # outwards. With chain walk, all incident edges at a vert agree on
    # side → simple sum gives the right bisector.
    outward_per_vert = {}
    for e, out in edge_outward.items():
        try:
            verts = list(e.verts)
        except ReferenceError:
            continue
        for v in verts:
            outward_per_vert.setdefault(v.index, []).append(out)
    for vi in list(outward_per_vert.keys()):
        outs = outward_per_vert[vi]
        avg = Vector((0.0, 0.0, 0.0))
        for o in outs:
            avg += o
        if avg.length < 1e-8:
            outward_per_vert.pop(vi, None)
            continue
        avg.normalize()
        outward_per_vert[vi] = avg
    # Average host plane per source corner. Use the corner's own position
    # as plane point (more accurate than centroid of one of the hosts)
    # plus the averaged normal across incident hosts.
    for vi, planes in plane_acc.items():
        avg_n = Vector((0.0, 0.0, 0.0))
        for _pt, n in planes:
            avg_n += n
        if avg_n.length < 1e-8:
            continue
        avg_n.normalize()
        # Anchor plane at the source corner itself — guarantees the
        # original source position lies exactly on the projection plane.
        anchor = src_pos.get(vi)
        if anchor is None:
            anchor = planes[0][0]
        plane_per_vert[vi] = (anchor.copy(), avg_n)

    # ----- Run bevel -----
    # v20.12: bevel(offset=d/2) gives a symmetric strip of total width d.
    # We then shift by outward * d/2 so one side ends up exactly at the
    # source line and the other at perpendicular distance d on the chosen
    # side. End result: strip width = `distance` (matches PER_EDGE
    # semantics — user expected width = d, not 2d).
    half_d = distance * 0.5
    geom = [e for e in target_edges if _edge_is_valid(e)]
    if not geom:
        return 0, set()
    # v20.14.5: miter_outer='SHARP' (was 'PATCH'). PATCH created multi-vert
    # corner quads on L-corners, leaving F2-side verts ~1.74*half_d from
    # source — outside threshold snap, so source line drifted at corners.
    # SHARP yields a single intersection point per corner, which after
    # the outward shift sits ~0.21*d from source (for 90° L) — well
    # within snap threshold. Source line is preserved exactly without
    # the geometry deformation that explicit snap caused for PATCH.
    try:
        result = bmesh.ops.bevel(
            bm,
            geom=geom,
            offset=half_d,
            offset_type='OFFSET',
            segments=1,
            profile=0.5,
            affect='EDGES',
            clamp_overlap=False,
            miter_outer='SHARP',
            miter_inner='SHARP',
        )
    except Exception as exc:
        print(f"[bevel] op failed: {exc!r}")
        return 0, set()

    new_verts_raw = list(result.get('verts', []))
    new_faces_raw = list(result.get('faces', []))

    # ----- Shift every bevel-created vert by outward*d to one-side the strip -----
    # ----- + project onto host plane so strip lies on the surface (Loop Cut behaviour)
    bm.verts.ensure_lookup_table()
    src_pos_items = list(src_pos.items())

    # v20.11.1: opt-in verbose diagnostics. Set EOVC_BEVEL_VERBOSE in env
    # OR via window manager prop to dump per-vert offsets and dip stats.
    import os
    verbose = os.environ.get('EOVC_BEVEL_VERBOSE', '0') == '1' or \
              bool(getattr(bpy.context.window_manager, 'eovc_bevel_verbose', False))

    shifted = 0
    projected = 0
    z_dev_samples = []   # (vert_idx, src_idx, src_z, final_z, dz)
    big_dips = []        # entries with |dz| > 0.5 * distance
    if verbose:
        print(f"[bevel diag] === flip={flip}  d={distance:.4f}  new_verts={len(new_verts_raw)}  source_corners={len(src_pos)} ===")
        print(f"[bevel diag] outwards captured: {len(outward_per_vert)}  planes captured: {len(plane_per_vert)}")
        for vi, src_co in src_pos_items:
            ow = outward_per_vert.get(vi)
            pl = plane_per_vert.get(vi)
            ow_str = f"({ow.x:.3f},{ow.y:.3f},{ow.z:.3f})" if ow else "None"
            pl_n_str = f"({pl[1].x:.3f},{pl[1].y:.3f},{pl[1].z:.3f})" if pl else "None"
            print(f"[bevel diag] src#{vi} co=({src_co.x:.3f},{src_co.y:.3f},{src_co.z:.3f})  outward={ow_str}  plane_n={pl_n_str}")

    for nv in new_verts_raw:
        try:
            if not nv.is_valid:
                continue
        except ReferenceError:
            continue
        co_pre = nv.co.copy()
        # Match this new vert to the closest source corner — its bevel outward
        # comes from there.
        best_src = None
        best_dist = float('inf')
        for src_idx, src_co in src_pos_items:
            dist = (nv.co - src_co).length
            if dist < best_dist:
                best_dist = dist
                best_src = src_idx
        if best_src is None:
            if verbose:
                print(f"[bevel diag]   nv#{nv.index} co={tuple(round(x,3) for x in co_pre)} → NO src match")
            continue
        outward = outward_per_vert.get(best_src)
        if outward is None:
            if verbose:
                print(f"[bevel diag]   nv#{nv.index} → no outward at src#{best_src}")
            continue
        # Step 1: shift to one-side the strip. Half-distance because
        # bevel was run at offset = d/2; total displacement equals d/2,
        # placing one strip edge AT source line (collapsed inward side)
        # and the other at perpendicular distance d on the outward side.
        nv.co = nv.co + outward * half_d
        co_post_shift = nv.co.copy()
        shifted += 1
        # Step 2: snap onto host plane → strip lies on the surface like a
        # native Loop Cut. Skips if no plane was captured for this corner.
        plane = plane_per_vert.get(best_src)
        proj_amount = 0.0
        if plane is not None:
            plane_pt, plane_n = plane
            offset_from_plane = (nv.co - plane_pt).dot(plane_n)
            if abs(offset_from_plane) > 1e-6:
                nv.co = nv.co - plane_n * offset_from_plane
                proj_amount = offset_from_plane
                projected += 1

        # Step 3: snap inward-side verts EXACTLY to their source corner.
        # bevel's miter math places F2-side miter quads at offsets like
        # corner - bisector * (d/2 / cos(half_angle)) — at sharp corners
        # this lands several cm away from source, not at it. After our
        # shift the residual is up to half_d for 90° L-corners while the
        # outer side sits at ~1.85 * half_d. The "halfway" threshold
        # half_d * 1.0 cleanly separates inner (snap) from outer (keep).
        # For straight chains the inner side is essentially at source
        # already (sub-mm) so the snap is a no-op. The user requires
        # source edge lines to hold their height/position exactly.
        src_corner_co = src_pos.get(best_src)
        if src_corner_co is not None:
            d_to_src = (nv.co - src_corner_co).length
            if d_to_src < half_d:
                nv.co = src_corner_co.copy()

        src_z = src_pos[best_src].z
        dz = nv.co.z - src_z
        z_dev_samples.append((nv.index, best_src, src_z, nv.co.z, dz))
        if abs(dz) > 0.5 * distance:
            big_dips.append((nv.index, best_src, dz, best_dist))

        if verbose:
            print(f"[bevel diag]   nv#{nv.index} src#{best_src} dist={best_dist:.3f}  "
                  f"pre=({co_pre.x:.3f},{co_pre.y:.3f},{co_pre.z:.3f}) "
                  f"shift→({co_post_shift.x:.3f},{co_post_shift.y:.3f},{co_post_shift.z:.3f}) "
                  f"proj={proj_amount:+.4f} "
                  f"final_z={nv.co.z:.4f}  src_z={src_z:.4f}  dz={dz:+.4f}")

    print(f"[bevel] shifted {shifted}  projected {projected}  big_dips {len(big_dips)} (|dz|>{0.5*distance:.4f})  flip={flip}")
    if big_dips:
        print(f"[bevel] BIG DIP samples (|dz|>{0.5*distance:.4f}):")
        for nv_idx, src_idx, dz, src_dist in big_dips[:20]:
            print(f"[bevel]   nv#{nv_idx} src#{src_idx}  dz={dz:+.4f}  closest-src-dist={src_dist:.3f}")

    # v20.12.1 — fuse the duplicates created by Step 3's snap-to-source.
    # At every inner corner the bevel's PATCH miter contributed several
    # F2-side verts; after snapping each onto the source corner they all
    # share the same position, so a small-tolerance remove_doubles pass
    # welds them into one true corner vertex.
    bm.verts.ensure_lookup_table()
    try:
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=0.0001)
    except Exception as exc:
        print(f"[bevel] remove_doubles after snap failed: {exc!r}")
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    # v20.12.4 was an explicit per-source-corner snap (move closest vert
    # to source) — it preserved 44/44 source positions but distorted
    # PATCH miter quads at L-corners (collapsing their inner verts to
    # source created near-degenerate quads with bad shading). Reverted
    # in v20.12.5. Achieving both source preservation AND clean miter
    # quads requires a fundamentally different approach (manual strip
    # construction without bevel — see proposal in chat).

    # v20.11.2 had a triangulate pass to fix non-planar quad rendering
    # (rare ~2 mm planar deviation when corner planes differed). User
    # disliked the visible triangle diagonals → reverted v20.12.0. The
    # non-planar quad either renders fine in solid view or, if a visible
    # crease still appears in some cases, we fix it differently (e.g.
    # snap each strip's 4 corners to a single per-source-edge plane).

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    new_faces = set()
    for f in new_faces_raw:
        try:
            if f.is_valid:
                new_faces.add(f)
        except ReferenceError:
            continue

    return len(new_faces), new_faces


def _edge_is_valid(e):
    try:
        return e.is_valid
    except ReferenceError:
        return False


def _face_is_valid(f):
    try:
        return f.is_valid
    except ReferenceError:
        return False


def _vert_is_valid(v):
    try:
        return v.is_valid
    except ReferenceError:
        return False


# ---------------------------------------------------------------------------
# v20.14 — EdgeNet algorithm (per GPT architectural plan)
# ---------------------------------------------------------------------------
# The model: instead of edge_split on existing host boundaries +
# face_split per source edge (which fails when adjacent source edges
# pick different host faces — see MITERED v20.13 dead-end), build the
# offset polyline as a WIRE EDGENET first, then call
# `bmesh.utils.face_split_edgenet(face, edgenet)` to embed and slice.
#
#   1. Snapshot source positions (assert/restore as a guard).
#   2. Compute miter target per source vert via existing helpers.
#   3. Create one offset BMVert per source corner with bm.verts.new (no
#      attempt to land on a boundary — they are interior wire verts).
#   4. Per source edge build the strip outline (v1→ov1, ov1→ov2, ov2→v2)
#      as wire edges. Accumulate them per affected face.
#   5. For each face: face_split_edgenet(face, accumulated edges). This
#      is the operation that lets us cross face boundaries and resolve
#      shared offset verts at corners cleanly.
#   6. Identify strip faces by walking source edges and picking the
#      adjacent face whose centroid sits on the outward side.
#
# Source edges/verts are NEVER touched, so source preservation is
# structural, not enforced by snap. One offset vert per source corner
# means clean miter join (no fan). Bleed is impossible: only faces
# explicitly identified as strip get painted.
#
# Reference: GPT-prototype walkthrough in chat dated v20.14.
# Blender API for face_split_edgenet:
#   https://docs.blender.org/api/current/bmesh.utils.html


def _edgenet_chain_align_outwards(outs):
    """Chain-consistency: align all outward vectors to share half-plane.
    Negate any subsequent outward whose dot with the first is negative.
    Returns the aligned list (does not mutate input)."""
    if not outs:
        return []
    ref = outs[0]
    aligned = [ref]
    for o in outs[1:]:
        aligned.append(-o if o.dot(ref) < 0.0 else o)
    return aligned


def _edgenet_compute_miter_targets(target_edges, distance, flip, explicit_host):
    """Return (miter_pos: src_v→Vector, edge_host: e→Face, edge_outward: (e,v)→Vec)."""
    target_set = set(target_edges)
    vert_to_edges = {}
    for e in target_edges:
        for v in e.verts:
            vert_to_edges.setdefault(v, []).append(e)

    edge_outward = {}
    edge_host = {}
    for e in target_edges:
        h, o = _pick_host_other(e, target_set, flip, explicit_host)
        if h is None:
            continue
        flip_for_outward = flip and not _eovc_flip_state.get(id(e), False)
        out = _compute_outward(e, h, o, flip_for_outward)
        if out is None:
            continue
        edge_host[e] = h
        for v in e.verts:
            edge_outward[(e, v)] = out

    miter_pos = {}
    for v, incident in vert_to_edges.items():
        outs = [edge_outward[(e, v)] for e in incident if (e, v) in edge_outward]
        if not outs:
            continue
        if len(outs) == 1:
            miter_pos[v] = v.co + outs[0] * distance
            continue
        if len(outs) == 2:
            n1, n2 = outs
            if n1.dot(n2) < 0.0:
                n2 = -n2
            denom = 1.0 + n1.dot(n2)
            if denom < 0.1:
                avg = n1 + n2
                if avg.length < 1e-8:
                    avg = n1.copy()
                avg.normalize()
                miter_pos[v] = v.co + avg * distance * 2.0
            else:
                miter_pos[v] = v.co + (n1 + n2) * (distance / denom)
            continue
        aligned = _edgenet_chain_align_outwards(outs)
        avg = Vector((0.0, 0.0, 0.0))
        for n in aligned:
            avg += n
        if avg.length > 1e-8:
            avg.normalize()
            miter_pos[v] = v.co + avg * distance

    return miter_pos, edge_host, edge_outward


def _edgenet_face_accepts_point(face, p, plane_tol=0.005, edge_tol=0.001):
    """Proper point-in-face: planar distance + 2D point-in-polygon.
    Projects face polygon and `p` onto face plane, then uses signed area
    method to test inside. Plane_tol = 5mm accommodates numerical drift
    from miter formula on slanted faces. edge_tol = 1mm gives a small
    inside-edge slack so verts exactly on a boundary count as inside."""
    try:
        n = face.normal
        if n.length < 1e-7:
            return False
        n = n.normalized()
        verts = list(face.verts)
        if len(verts) < 3:
            return False
        anchor = verts[0].co
    except (ReferenceError, IndexError):
        return False
    plane_d = (p - anchor).dot(n)
    if abs(plane_d) > plane_tol:
        return False
    # Project p onto face plane
    p_on_plane = p - n * plane_d

    # Build 2D basis on the face plane (axis-aligned to first edge).
    e0 = (verts[1].co - verts[0].co)
    if e0.length < 1e-9:
        return False
    u = e0.normalized()
    v = n.cross(u).normalized()

    def to2d(co):
        rel = co - anchor
        return (rel.dot(u), rel.dot(v))

    poly2d = [to2d(vv.co) for vv in verts]
    px, py = to2d(p_on_plane)

    # Crossing-number point-in-polygon with edge slack.
    inside = False
    n_pts = len(poly2d)
    for i in range(n_pts):
        ax, ay = poly2d[i]
        bx, by = poly2d[(i + 1) % n_pts]
        if (ay > py) != (by > py):
            x_cross = ax + (py - ay) * (bx - ax) / (by - ay)
            if px < x_cross + edge_tol:
                inside = not inside
    return inside


def _edgenet_face_basis(face):
    """Return (anchor, normal, u, v, verts) for stable face-plane projection."""
    try:
        verts = list(face.verts)
        if len(verts) < 3:
            return None
        n = face.normal
        if n.length < 1e-8:
            return None
        n = n.normalized()
        anchor = verts[0].co.copy()
    except ReferenceError:
        return None

    # Prefer the longest face edge as projection axis to reduce precision loss.
    best = None
    best_len = 0.0
    for i, va in enumerate(verts):
        vb = verts[(i + 1) % len(verts)]
        vec = vb.co - va.co
        ln = vec.length
        if ln > best_len:
            best_len = ln
            best = vec
    if best is None or best_len < 1e-9:
        return None
    u = best / best_len
    v = n.cross(u)
    if v.length < 1e-9:
        return None
    v.normalize()
    return anchor, n, u, v, verts


def _edgenet_project_2d(co, anchor, u, v):
    rel = co - anchor
    return (rel.dot(u), rel.dot(v))


def _edgenet_project_to_face(face, co):
    basis = _edgenet_face_basis(face)
    if basis is None:
        return co.copy()
    anchor, n, _u, _v, _verts = basis
    return co - n * (co - anchor).dot(n)


def _edgenet_dist_point_seg_2d(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    vx = bx - ax; vy = by - ay
    denom = vx * vx + vy * vy
    if denom < 1e-18:
        dx = px - ax; dy = py - ay
        return (dx * dx + dy * dy) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / denom))
    qx = ax + vx * t; qy = ay + vy * t
    dx = px - qx; dy = py - qy
    return (dx * dx + dy * dy) ** 0.5


def _edgenet_point_in_poly_2d(p, poly, edge_tol=1e-6):
    """Crossing-number point-in-polygon with explicit boundary acceptance."""
    if len(poly) < 3:
        return False
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        if _edgenet_dist_point_seg_2d(p, a, b) <= edge_tol:
            return True
    px, py = p
    inside = False
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        if (ay > py) != (by > py):
            x_cross = ax + (py - ay) * (bx - ax) / (by - ay)
            if px < x_cross:
                inside = not inside
    return inside


def _edgenet_cross_2d(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _edgenet_seg_intersection_t_values(a, b, c, d, eps=1e-8):
    """Return t-values on segment a->b where it meets c->d."""
    rx = b[0] - a[0]; ry = b[1] - a[1]
    sx = d[0] - c[0]; sy = d[1] - c[1]
    r = (rx, ry); s = (sx, sy)
    qmp = (c[0] - a[0], c[1] - a[1])
    denom = _edgenet_cross_2d(r, s)
    out = []
    rr = rx * rx + ry * ry
    if rr < eps * eps:
        return out
    if abs(denom) < eps:
        # Parallel. If collinear, add projected endpoints as interval cuts.
        if abs(_edgenet_cross_2d(qmp, r)) > eps:
            return out
        for p in (c, d):
            t = ((p[0] - a[0]) * rx + (p[1] - a[1]) * ry) / rr
            if -eps <= t <= 1.0 + eps:
                out.append(max(0.0, min(1.0, t)))
        return out
    t = _edgenet_cross_2d(qmp, s) / denom
    u = _edgenet_cross_2d(qmp, r) / denom
    if -eps <= t <= 1.0 + eps and -eps <= u <= 1.0 + eps:
        out.append(max(0.0, min(1.0, t)))
    return out


def _edgenet_unique_sorted(values, eps=1e-6):
    values = sorted(max(0.0, min(1.0, v)) for v in values)
    out = []
    for v in values:
        if not out or abs(v - out[-1]) > eps:
            out.append(v)
    return out


def _edgenet_clip_segment_to_face(face, a3, b3, edge_tol=1e-5):
    """Clip a 3D segment to one face polygon. Returns [(t0, t1), ...]."""
    basis = _edgenet_face_basis(face)
    if basis is None:
        return []
    anchor, _n, u, v, verts = basis
    poly = [_edgenet_project_2d(vv.co, anchor, u, v) for vv in verts]
    a = _edgenet_project_2d(a3, anchor, u, v)
    b = _edgenet_project_2d(b3, anchor, u, v)

    t_values = [0.0, 1.0]
    for i, c in enumerate(poly):
        d = poly[(i + 1) % len(poly)]
        t_values.extend(_edgenet_seg_intersection_t_values(a, b, c, d))
    t_values = _edgenet_unique_sorted(t_values)

    intervals = []
    for i in range(len(t_values) - 1):
        t0 = t_values[i]
        t1 = t_values[i + 1]
        if t1 - t0 < 1e-6:
            continue
        tm = (t0 + t1) * 0.5
        pm = (a[0] + (b[0] - a[0]) * tm,
              a[1] + (b[1] - a[1]) * tm)
        if _edgenet_point_in_poly_2d(pm, poly, edge_tol):
            intervals.append((t0, t1))
    # Keep fully-inside zero-intersection segments.
    if not intervals and _edgenet_point_in_poly_2d(a, poly, edge_tol) \
            and _edgenet_point_in_poly_2d(b, poly, edge_tol):
        intervals.append((0.0, 1.0))
    return intervals


def _edgenet_face_bbox_overlaps(face, points, pad=0.01):
    try:
        fverts = list(face.verts)
    except ReferenceError:
        return False
    if not fverts:
        return False
    minx = min(p.x for p in points) - pad
    miny = min(p.y for p in points) - pad
    minz = min(p.z for p in points) - pad
    maxx = max(p.x for p in points) + pad
    maxy = max(p.y for p in points) + pad
    maxz = max(p.z for p in points) + pad
    fminx = min(v.co.x for v in fverts); fmaxx = max(v.co.x for v in fverts)
    fminy = min(v.co.y for v in fverts); fmaxy = max(v.co.y for v in fverts)
    fminz = min(v.co.z for v in fverts); fmaxz = max(v.co.z for v in fverts)
    return not (fmaxx < minx or fminx > maxx or
                fmaxy < miny or fminy > maxy or
                fmaxz < minz or fminz > maxz)


def _edgenet_face_intersects_strip_segments(face, v1_co, v2_co, off1_co, off2_co):
    segments = ((v1_co, off1_co), (off1_co, off2_co), (off2_co, v2_co))
    for a, b in segments:
        if _edgenet_clip_segment_to_face(face, a, b):
            return True
    return False


def _edgenet_pick_split_faces(e, host, off1_co, off2_co):
    """Pick faces that should receive edgenet for this source edge.

    v20.14.1: walk a small face corridor from the source edge and accept
    every face intersected by one of the three strip edgenet segments. This
    catches the common case where the miter target lies outside the immediate
    host quad but the strip crosses into a neighbouring coplanar face."""
    v1_co = e.verts[0].co
    v2_co = e.verts[1].co
    strip_points = (v1_co, v2_co, off1_co, off2_co)
    accepted = []
    seen = set()
    queue = []
    if host is not None and _face_is_valid(host):
        queue.append((host, 0)); seen.add(host.index)
    try:
        for f in e.link_faces:
            if f.index not in seen and f.is_valid:
                queue.append((f, 0)); seen.add(f.index)
    except ReferenceError:
        pass

    q_index = 0
    max_depth = 4
    max_faces = 48
    while q_index < len(queue) and len(seen) <= max_faces:
        f, depth = queue[q_index]
        q_index += 1
        if not _face_is_valid(f):
            continue
        if _edgenet_face_bbox_overlaps(f, strip_points) and \
                _edgenet_face_intersects_strip_segments(
                    f, v1_co, v2_co, off1_co, off2_co):
            accepted.append(f)
        if depth >= max_depth:
            continue
        try:
            for ed in f.edges:
                for nf in ed.link_faces:
                    if nf is f or not nf.is_valid:
                        continue
                    if nf.index in seen:
                        continue
                    if not _edgenet_face_bbox_overlaps(nf, strip_points, pad=0.05):
                        continue
                    seen.add(nf.index)
                    queue.append((nf, depth + 1))
        except ReferenceError:
            continue
    return accepted


def _edgenet_point_key(co, scale=100000.0):
    return (round(co.x * scale), round(co.y * scale), round(co.z * scale))


def _edgenet_vert_for_face_point(bm, face, co, preferred=None,
                                 target_set=None, point_cache=None):
    """Return/create a BMVert on `face` at `co` without moving source verts."""
    if point_cache is None:
        point_cache = {}
    co = _edgenet_project_to_face(face, co)
    key = _edgenet_point_key(co)
    cached = point_cache.get(key)
    if cached is not None:
        try:
            if cached.is_valid:
                return cached
        except ReferenceError:
            pass

    if preferred is not None:
        try:
            if preferred.is_valid and (preferred.co - co).length <= 1e-4:
                point_cache[key] = preferred
                return preferred
        except ReferenceError:
            pass

    try:
        for vv in face.verts:
            if (vv.co - co).length <= 1e-4:
                point_cache[key] = vv
                return vv
    except ReferenceError:
        return None

    # Boundary point: split the current boundary edge unless it is one of the
    # source edges. Source edges must remain exactly as selected.
    try:
        for ed in list(face.edges):
            va, vb = ed.verts
            ab = vb.co - va.co
            denom = ab.dot(ab)
            if denom < 1e-12:
                continue
            t = (co - va.co).dot(ab) / denom
            if t <= 1e-5 or t >= 1.0 - 1e-5:
                continue
            closest = va.co + ab * t
            if (closest - co).length > 1e-4:
                continue
            if target_set is not None and ed in target_set:
                return None
            try:
                _new_edge, nv = bmesh.utils.edge_split(ed, va, t)
                nv.co = co
                point_cache[key] = nv
                return nv
            except Exception as exc:
                print(f"[edgenet clip] boundary edge_split failed: {exc!r}")
                return None
    except ReferenceError:
        return None

    # Interior point. It remains a wire vert until face_split_edgenet embeds it.
    try:
        nv = bm.verts.new(co)
        point_cache[key] = nv
        return nv
    except Exception as exc:
        print(f"[edgenet clip] interior vert create failed: {exc!r}")
        return None


def _edgenet_add_clipped_segment(bm, face, a_co, b_co, a_vert, b_vert,
                                 target_set, point_cache, created_verts):
    edges = []
    for t0, t1 in _edgenet_clip_segment_to_face(face, a_co, b_co):
        p0 = a_co.lerp(b_co, t0)
        p1 = a_co.lerp(b_co, t1)
        pref0 = a_vert if t0 <= 1e-5 else (b_vert if t0 >= 1.0 - 1e-5 else None)
        pref1 = a_vert if t1 <= 1e-5 else (b_vert if t1 >= 1.0 - 1e-5 else None)
        v0 = _edgenet_vert_for_face_point(
            bm, face, p0, pref0, target_set, point_cache)
        v1 = _edgenet_vert_for_face_point(
            bm, face, p1, pref1, target_set, point_cache)
        if v0 is None or v1 is None or v0 is v1:
            continue
        ed = _edgenet_get_or_new_edge(bm, v0, v1)
        if ed is not None:
            edges.append(ed)
            if v0 is not a_vert and v0 is not b_vert:
                created_verts.add(v0)
            if v1 is not a_vert and v1 is not b_vert:
                created_verts.add(v1)
    return edges


def _edgenet_poly_area_on_face(face, coords):
    basis = _edgenet_face_basis(face)
    if basis is None or len(coords) < 3:
        return 0.0
    anchor, _n, u, v, _verts = basis
    pts = [_edgenet_project_2d(co, anchor, u, v) for co in coords]
    area = 0.0
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        area += a[0] * b[1] - b[0] * a[1]
    return abs(area) * 0.5


def _edgenet_face_centroid_inside_strip(face, strip_coords, strip_area):
    basis = _edgenet_face_basis(face)
    if basis is None:
        return False
    anchor, _n, u, v, _verts = basis
    try:
        c = face.calc_center_median()
        face_area = face.calc_area()
    except ReferenceError:
        return False
    poly2d = [_edgenet_project_2d(co, anchor, u, v) for co in strip_coords]
    c2d = _edgenet_project_2d(c, anchor, u, v)
    if not _edgenet_point_in_poly_2d(c2d, poly2d, edge_tol=0.002):
        return False
    if strip_area > 1e-8 and face_area > strip_area * 1.75 + 0.0005:
        return False
    return True



def _edgenet_get_or_new_edge(bm, va, vb):
    """Idempotent edge factory — returns existing or creates new."""
    if va is vb:
        return None
    try:
        existing = bm.edges.get((va, vb))
    except (TypeError, ValueError):
        existing = None
    if existing is not None:
        return existing
    try:
        return bm.edges.new((va, vb))
    except ValueError:
        try:
            return bm.edges.get((va, vb))
        except (TypeError, ValueError):
            return None


def _do_loop_cut_offsetloops(bm, target_edges, distance, flip, explicit_host=None):
    """v20.14.2 — source-preserving loop cut using Blender offset_edgeloops.

    `offset_edgeloops` duplicates the selected edge loop in-place and creates
    connected faces while leaving the original source edges untouched. We then
    move only the new duplicate verts to the miter targets and weld duplicate
    copies per source corner.
    """
    target_edges = [e for e in target_edges if _edge_is_valid(e)]
    if not target_edges:
        return 0, set()
    _eovc_flip_state.clear()

    source_pos = {}
    for e in target_edges:
        for v in e.verts:
            source_pos[v] = v.co.copy()

    miter_pos, _edge_host, edge_outward = _edgenet_compute_miter_targets(
        target_edges, distance, flip, explicit_host)
    if not miter_pos:
        return 0, set()

    strip_specs = []
    for e in target_edges:
        try:
            v1, v2 = e.verts[0], e.verts[1]
        except ReferenceError:
            continue
        if v1 not in miter_pos or v2 not in miter_pos:
            continue
        strip_specs.append((e, v1, v2,
                            (v1.co.copy(), v2.co.copy(),
                             miter_pos[v2].copy(), miter_pos[v1].copy())))

    orig_verts = set(bm.verts)
    try:
        result = bmesh.ops.offset_edgeloops(
            bm,
            edges=target_edges,
            use_cap_endpoint=True,
        )
    except Exception as exc:
        print(f"[offsetloop] op failed: {exc!r}")
        return 0, set()

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    new_verts = [v for v in bm.verts if v not in orig_verts]
    # New verts are born at the exact source positions. Match by rounded
    # coordinate so source BMVert indices are irrelevant after the op.
    source_by_coord = {
        _edgenet_point_key(src_v.co, scale=100000.0): src_v
        for src_v in miter_pos.keys()
    }
    offsets_by_src = {}
    for nv in new_verts:
        try:
            key = _edgenet_point_key(nv.co, scale=100000.0)
            src_v = source_by_coord.get(key)
            if src_v is None:
                continue
            offsets_by_src.setdefault(src_v, []).append(nv)
            nv.co = miter_pos[src_v].copy()
        except ReferenceError:
            continue

    targetmap = {}
    offset_for_src = {}
    for src_v, offsets in offsets_by_src.items():
        valid = [v for v in offsets if _vert_is_valid(v)]
        if not valid:
            continue
        keep = valid[0]
        offset_for_src[src_v] = keep
        for nv in valid[1:]:
            if nv is not keep:
                targetmap[nv] = keep
    if targetmap:
        try:
            bmesh.ops.weld_verts(bm, targetmap=targetmap)
        except Exception as exc:
            print(f"[offsetloop] duplicate weld failed: {exc!r}")

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.normal_update()

    restored = 0
    for src_v, co in source_pos.items():
        try:
            if src_v.is_valid and (src_v.co - co).length > 1e-8:
                src_v.co = co
                restored += 1
        except ReferenceError:
            continue
    if restored:
        print(f"[offsetloop] source guard restored {restored} verts")

    strip_faces = set()
    for e, v1, v2, _strip_coords in strip_specs:
        try:
            if not e.is_valid:
                continue
        except ReferenceError:
            continue
        ov1 = offset_for_src.get(v1)
        ov2 = offset_for_src.get(v2)
        offset_candidates = [v for v in (ov1, ov2) if _vert_is_valid(v)]
        if not offset_candidates:
            continue
        out = edge_outward.get((e, v1)) or edge_outward.get((e, v2))
        mid = (v1.co + v2.co) * 0.5
        candidates = []
        try:
            link_faces = list(e.link_faces)
        except ReferenceError:
            link_faces = []
        for f in link_faces:
            if not _face_is_valid(f):
                continue
            try:
                fverts = set(f.verts)
            except ReferenceError:
                continue
            if v1 not in fverts or v2 not in fverts:
                continue
            if not any(ov in fverts for ov in offset_candidates):
                continue
            dot = (f.calc_center_median() - mid).dot(out) if out is not None else 0.0
            candidates.append((f, dot))
        if candidates:
            strip_faces.add(max(candidates, key=lambda item: item[1])[0])

    bad_faces = 0
    for f in bm.faces:
        try:
            if f.is_valid and f.calc_area() < 1e-10:
                bad_faces += 1
        except ReferenceError:
            continue
    print(f"[offsetloop] cuts={len(strip_faces)}/{len(target_edges)}  "
          f"returned_edges={len(result.get('edges', []))}  "
          f"new_verts={len(new_verts)}  bad_faces={bad_faces}")

    return len(strip_faces), strip_faces


def _do_loop_cut_edgenet(bm, target_edges, distance, flip, explicit_host=None):
    """v20.14 — EdgeNet-based loop cut. Returns (cut_count, strip_faces).

    Source edges/verts remain UNTOUCHED throughout. Offset verts are
    created as interior wire verts and embedded into faces via
    face_split_edgenet. After split, strip faces are identified by
    proximity + outward direction relative to source edges."""
    # v20.14.2: the literal face_split_edgenet implementation below is kept
    # for reference, but production dispatch uses Blender's native
    # offset_edgeloops primitive. It preserves source topology and produces a
    # continuous duplicate loop that can be moved to the miter targets.
    return _do_loop_cut_offsetloops(
        bm, target_edges, distance, flip, explicit_host)

    target_edges = [e for e in target_edges if _edge_is_valid(e)]
    if not target_edges:
        return 0, set()
    target_set = set(target_edges)
    _eovc_flip_state.clear()

    # ----- Snapshot source positions (guard against accidental moves) -----
    source_pos = {}
    for e in target_edges:
        for v in e.verts:
            source_pos[v] = v.co.copy()

    # ----- Phase 1: compute miter targets via existing helpers -----
    miter_pos, edge_host, edge_outward = _edgenet_compute_miter_targets(
        target_edges, distance, flip, explicit_host)

    if not miter_pos:
        return 0, set()

    # ----- Phase 2: create offset BMVerts as interior wire verts -----
    offset_vert = {}
    for src_v, pos in miter_pos.items():
        try:
            if src_v.is_valid:
                offset_vert[src_v] = bm.verts.new(pos)
        except ReferenceError:
            continue
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()

    # ----- Phase 3: build per-face clipped edgenet -----
    face_to_edges = {}  # BMFace -> set of BMEdge
    split_jobs = []     # [(source_edge, face, [BMEdge, ...]), ...]
    created_clip_verts = set()
    point_cache = {}
    strip_specs = []  # [(v1, v2, ov1, ov2, strip_coords), ...]
    for e in target_edges:
        v1, v2 = e.verts[0], e.verts[1]
        ov1 = offset_vert.get(v1)
        ov2 = offset_vert.get(v2)
        if ov1 is None or ov2 is None:
            continue
        strip_coords = (v1.co.copy(), v2.co.copy(), ov2.co.copy(), ov1.co.copy())
        strip_specs.append((v1, v2, ov1, ov2, strip_coords))
        host = edge_host.get(e)
        candidate_faces = _edgenet_pick_split_faces(
            e, host, ov1.co, ov2.co)
        if not candidate_faces:
            continue
        for f in candidate_faces:
            f_edges = []
            f_edges.extend(_edgenet_add_clipped_segment(
                bm, f, v1.co, ov1.co, v1, ov1,
                target_set, point_cache, created_clip_verts))
            f_edges.extend(_edgenet_add_clipped_segment(
                bm, f, ov1.co, ov2.co, ov1, ov2,
                target_set, point_cache, created_clip_verts))
            f_edges.extend(_edgenet_add_clipped_segment(
                bm, f, ov2.co, v2.co, ov2, v2,
                target_set, point_cache, created_clip_verts))
            if f_edges:
                face_to_edges.setdefault(f, set()).update(f_edges)
                split_jobs.append((e, f, f_edges))

    # ----- Phase 4: face_split_edgenet per affected face -----
    cut_count = 0
    new_faces_all = set()
    fse_returned_faces = 0
    fse_returned_empty = 0
    fse_threw = 0
    for _src_edge, face, edgenet in split_jobs:
        if not _face_is_valid(face):
            continue
        valid_edges = [ed for ed in edgenet if _edge_is_valid(ed)]
        if not valid_edges:
            continue
        face_idx_before = face.index
        face_n_verts_before = len(face.verts)
        try:
            made = bmesh.utils.face_split_edgenet(face, valid_edges)
            if made:
                cut_count += 1
                fse_returned_faces += 1
                for nf in made:
                    if nf is not None and _face_is_valid(nf):
                        new_faces_all.add(nf)
                print(f"[edgenet]   face#{face_idx_before} (verts={face_n_verts_before}) → SPLIT into {len(made)} pieces (edges={len(valid_edges)})")
            else:
                fse_returned_empty += 1
                print(f"[edgenet]   face#{face_idx_before} (verts={face_n_verts_before}) → EMPTY (edgenet={len(valid_edges)} edges)")
        except Exception as exc:
            fse_threw += 1
            print(f"[edgenet]   face#{face_idx_before} → THREW: {exc!r}")
    print(f"[edgenet] face_split_edgenet stats: split={fse_returned_faces}  empty={fse_returned_empty}  threw={fse_threw}")

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    # ----- Phase 5: source preservation guard -----
    restored = 0
    for v, co in source_pos.items():
        try:
            if v.is_valid and (v.co - co).length > 1e-8:
                v.co = co
                restored += 1
        except ReferenceError:
            continue
    if restored:
        print(f"[edgenet] source guard restored {restored} verts")

    # ----- Phase 6: identify strip faces.
    # v20.14.1: clipped strips can be triangles/quads/ngons depending on
    # where the strip crosses host boundaries. The old n_verts<=4 gate was
    # too strict. Use centroid-in-strip with an area cap instead: it paints
    # faces that geometrically sit inside a source-edge strip while rejecting
    # large unsplit host faces whose centroid happens to fall on the strip.
    offset_verts_set = set(offset_vert.values())
    created_verts_set = set(created_clip_verts) | offset_verts_set
    strip_faces = set()
    candidate_faces = set(new_faces_all)
    for f in face_to_edges.keys():
        if _face_is_valid(f):
            candidate_faces.add(f)
    for ov in created_verts_set:
        try:
            if not ov.is_valid:
                continue
            for f in ov.link_faces:
                if _face_is_valid(f):
                    candidate_faces.add(f)
        except ReferenceError:
            continue

    for f in candidate_faces:
        try:
            if not f.is_valid:
                continue
            face_verts = list(f.verts)
        except ReferenceError:
            continue
        if not any(v in created_verts_set for v in face_verts):
            continue
        for _v1, _v2, _ov1, _ov2, strip_coords in strip_specs:
            strip_area = _edgenet_poly_area_on_face(f, strip_coords)
            if _edgenet_face_centroid_inside_strip(f, strip_coords, strip_area):
                strip_faces.add(f)
                break

    # Remove unused loose offset verts left by clipped-away miter targets.
    loose_offsets = []
    for ov in offset_verts_set:
        try:
            if ov.is_valid and len(ov.link_edges) == 0:
                loose_offsets.append(ov)
        except ReferenceError:
            continue
    if loose_offsets:
        try:
            bmesh.ops.delete(bm, geom=loose_offsets, context='VERTS')
        except Exception as exc:
            print(f"[edgenet] loose offset cleanup failed: {exc!r}")

    print(f"[edgenet] cuts={cut_count}/{len(target_edges)}  "
          f"strip_faces={len(strip_faces)}  "
          f"face_split_targets={len(face_to_edges)}  "
          f"new_faces={len(new_faces_all)}  "
          f"candidates={len(candidate_faces)}")
    return cut_count, strip_faces


# ---------------------------------------------------------------------------
# v20.0 — Multi-set bake helper (mesh-level cut without edit mode)
# ---------------------------------------------------------------------------
def _filter_cuttable_edges(bm_edges):
    """Apply slope+host filter (same as Add Selected operator)."""
    out = []
    # v20.14.15: SLOPE_MAX raised 0.30→0.70. Host filter (_pick_host_other,
    # |normal.z|>0.5 ≈ up to 60° tilt) is the real validity gate; the
    # 0.30 cap was rejecting sloped-curb edges (e.g. 0.330) whose host
    # face was clearly horizontal (normal.z≈0.944). 0.70 still rejects
    # truly vertical wall edges (slope>0.9) while letting sloped curb
    # tops through.
    SLOPE_MAX = 0.70
    for e in bm_edges:
        try:
            if not e.is_valid:
                continue
        except ReferenceError:
            continue
        z_diff = abs(e.verts[0].co.z - e.verts[1].co.z)
        edge_len = (e.verts[1].co - e.verts[0].co).length
        slope = (z_diff / edge_len) if edge_len > 1e-9 else 0.0
        if slope > SLOPE_MAX:
            continue
        host, _ = _pick_host_other(e)
        if host is None:
            continue
        out.append(e)
    return out


def _apply_sets_to_mesh(mesh, edge_sets, vc_layer_name):
    """Run the full pipeline for a list of EdgeSets on a Mesh datablock.
    Each set produces its own cut pass (with its own distance / extra_lines /
    flip) and its own paint pass (with its color × intensity).

    Sets with bake_enabled=False are skipped entirely.
    Sets are processed in collection order — later sets overlay earlier on
    overlapping verts in the vertex color attribute.

    Returns aggregate stats {cuts, painted, sets_baked, missing_total}."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    color_layer = bm.loops.layers.color.get(vc_layer_name)
    if color_layer is None:
        color_layer = bm.loops.layers.color.new(vc_layer_name)

    total_cuts = 0
    total_painted = 0
    sets_baked = 0
    missing_total = 0

    for s in edge_sets:
        if not s.bake_enabled:
            continue
        # Resolve edges by canonical key
        key_set = {(min(e.v1, e.v2), max(e.v1, e.v2)) for e in s.edges}
        if not key_set:
            continue

        bm.edges.ensure_lookup_table()
        sel = []
        for e in bm.edges:
            a, b = e.verts[0].index, e.verts[1].index
            if (min(a, b), max(a, b)) in key_set:
                sel.append(e)
        missing_total += len(key_set) - len(sel)

        filtered = _filter_cuttable_edges(sel)
        if not filtered:
            continue

        # Multi-line cut for this set
        n_total = max(1, min(3, s.extra_lines + 1))
        distances = [s.distance * (n_total - i) / n_total for i in range(n_total)]

        # v20.5: resolve explicit host face if user picked one
        explicit_host = None
        if s.host_face_index >= 0:
            bm.faces.ensure_lookup_table()
            if s.host_face_index < len(bm.faces):
                explicit_host = bm.faces[s.host_face_index]

        # v20.7+: dispatch on per-set algorithm choice. Mitered uses bisector
        # corner targets; per-edge keeps the legacy multi-phase pipeline;
        # bevel delegates to bmesh.ops.bevel and paints returned faces.
        algo = getattr(s, "algorithm", 'PER_EDGE')

        # Paint colour for this set (computed once)
        rgba = COLOR_MAP.get(s.color, COLOR_MAP['RED'])
        intensity = max(0.0, min(1.0, s.intensity))
        rgba = (rgba[0] * intensity, rgba[1] * intensity, rgba[2] * intensity, rgba[3])

        if algo == 'BEVEL':
            # v20.8: bevel produces strip faces directly. extra_lines is
            # ignored (bevel makes a single strip per pass; extra parallel
            # lines would require subdividing the strip — out of scope).
            n_cut, new_faces = _do_loop_cut_bevel(bm, filtered, s.distance,
                                                   s.flip, explicit_host)
            total_cuts += n_cut
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            for f in new_faces:
                try:
                    if not f.is_valid:
                        continue
                except ReferenceError:
                    continue
                for loop in f.loops:
                    loop[color_layer] = rgba
                    total_painted += 1
            sets_baked += 1
            continue

        if algo == 'MITERED':
            # v20.13: MITERED returns (cut_count, strip_faces). Source
            # edges/verts remain at their original positions (PER_EDGE
            # semantics). Painting the returned strip faces directly
            # avoids the v20.7 bleed where the `src_verts`-based painter
            # filled the entire host face when face_split was skipped.
            for d in distances:
                n_cut, strip_faces = _do_loop_cut_mitered(
                    bm, filtered, d, s.flip, explicit_host)
                total_cuts += n_cut
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                for f in strip_faces:
                    try:
                        if not f.is_valid:
                            continue
                    except ReferenceError:
                        continue
                    for loop in f.loops:
                        loop[color_layer] = rgba
                        total_painted += 1
            sets_baked += 1
            continue

        if algo == 'EDGENET':
            # v20.14: GPT-architectural plan. Source UNTOUCHED, miter
            # built as wire edgenet, embedded via face_split_edgenet.
            for d in distances:
                n_cut, strip_faces = _do_loop_cut_edgenet(
                    bm, filtered, d, s.flip, explicit_host)
                total_cuts += n_cut
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                for f in strip_faces:
                    try:
                        if not f.is_valid:
                            continue
                    except ReferenceError:
                        continue
                    for loop in f.loops:
                        loop[color_layer] = rgba
                        total_painted += 1
            sets_baked += 1
            continue

        # PER_EDGE legacy
        for d in distances:
            total_cuts += _do_loop_cut(bm, filtered, d, s.flip, explicit_host)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

        # v20.4: skip chain endpoints (source verts with only 1 incident
        # edge). After Phase 6 taper-weld, endpoint inherits offset's
        # connectivity → painting it bleeds color into surrounding faces.
        vert_count = {}
        for e in filtered:
            try:
                if not e.is_valid:
                    continue
            except ReferenceError:
                continue
            for v in e.verts:
                vert_count[v] = vert_count.get(v, 0) + 1
        # Skip endpoint only if chain has 2+ edges; single-edge sets paint both
        is_chain = len(filtered) >= 2
        src_verts = set()
        for v, n in vert_count.items():
            if is_chain and n < 2:
                continue  # chain endpoint — skip
            src_verts.add(v)
        for f in bm.faces:
            for loop in f.loops:
                if loop.vert in src_verts:
                    loop[color_layer] = rgba
                    total_painted += 1
        sets_baked += 1

    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return {
        'cuts': total_cuts,
        'painted': total_painted,
        'sets_baked': sets_baked,
        'missing_total': missing_total,
    }


# Legacy wrapper — keeps the old API for v19.x compatibility paths
def _apply_cut_to_mesh(mesh, source_keys, distance, flip, extra_lines, color, vc_layer_name):
    """Legacy v19 wrapper — wraps a single (keys, params) call as a temp set."""
    # Build a temporary set-like object inline
    class _TempSet:
        bake_enabled = True
        algorithm = 'PER_EDGE'  # v20.7: legacy callers default to per-edge
        host_face_index = -1
        def __init__(self, keys, distance, flip, extra_lines, color):
            self.distance = distance
            self.flip = flip
            self.extra_lines = extra_lines
            self.color = color
            self.intensity = 1.0
            self.edges = [type('E', (), {'v1': a, 'v2': b})() for a, b in keys]
    temp = _TempSet(source_keys, distance, flip, extra_lines, color)
    result = _apply_sets_to_mesh(mesh, [temp], vc_layer_name)
    return {
        'cut_count': result['cuts'],
        'painted': result['painted'],
        'total_lines': max(1, extra_lines + 1),
        'missing_keys': result['missing_total'],
    }


def _recover_source_mesh(obj):
    """v20.3.4: PointerProperty to Mesh can become NULL after file save/load
    or some Blender internal events even though the snapshot mesh-data still
    exists in bpy.data. Try to recover by name matching `_eovc_source_<obj_name>`.
    Returns True if recovered or already valid."""
    ed = obj.eovc_data
    if ed.source_mesh is not None:
        return True
    # Try by name
    snapshot_name = f"_eovc_source_{obj.name}"
    snapshot = bpy.data.meshes.get(snapshot_name)
    if snapshot is not None:
        ed.source_mesh = snapshot
        print(f"[eovc] auto-recovered source_mesh pointer for {obj.name}")
        return True
    # If obj.data itself is the snapshot, link it
    if obj.data.name.startswith('_eovc_source_'):
        ed.source_mesh = obj.data
        print(f"[eovc] auto-recovered source_mesh from obj.data for {obj.name}")
        return True
    return False


def _capture_source_clone(obj):
    """Clone obj.data to a hidden source mesh datablock. Stored in
    eovc_data.source_mesh. Idempotent — overrides existing source.
    Use_fake_user keeps it alive across Blender sessions even with no
    user objects pointing at it."""
    src = obj.data.copy()
    src.name = f"_eovc_source_{obj.name}"
    src.use_fake_user = True
    # If there was an old source, release fake user so it can be cleaned up
    old = obj.eovc_data.source_mesh
    if old is not None and old is not src and old != obj.data:
        old.use_fake_user = False
    obj.eovc_data.source_mesh = src
    return src


def _swap_obj_data(obj, new_mesh):
    """Replace obj.data with new_mesh. Old data: if no other users, delete.
    Cannot be called while in EDIT mode for the swapped object."""
    if obj.data is new_mesh:
        return
    if obj.mode == 'EDIT':
        # Caller's responsibility — log and abort
        print(f"[eovc] cannot swap obj.data while in EDIT mode (obj={obj.name})")
        return
    old = obj.data
    obj.data = new_mesh
    # Cleanup: only delete old if no other user AND it's not the source we want to keep
    if old is not None and old.users == 0:
        try:
            bpy.data.meshes.remove(old)
        except Exception as exc:
            print(f"[eovc] failed to remove old mesh: {exc!r}")


def _apply_shade_smooth_and_weighted_normal(obj, sharp_angle_deg=30.0):
    """v20.14.10 — apply Shade Smooth + mark-sharp-by-angle + add/refresh
    WeightedNormal modifier so normals blend smoothly across the
    strip-to-host transition WHILE preserving sharp edges (e.g. building
    corners) intact.

    The mark-sharp pass is essential: WeightedNormal's `keep_sharp=True`
    only respects edges flagged as sharp. Without flagging, smoothing
    rounds out the geometry's natural sharp corners. We classify each
    interior edge by `calc_face_angle()` and mark it sharp when the
    fold exceeds the threshold (default 30°).

    Settings match the user's screenshot:
        Weighting Mode = Face Area
        Weight = 72
        Threshold = 0.01
        Keep Sharp = True
        Face Influence = False
    """
    import math
    mesh = obj.data
    if mesh is None:
        return

    # 1. Shade Smooth: every polygon → use_smooth = True
    for poly in mesh.polygons:
        poly.use_smooth = True

    # 2. Mark edges sharp by face-angle threshold so WeightedNormal's
    #    keep_sharp can preserve geometry corners.
    sharp_angle = math.radians(sharp_angle_deg)
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.edges.ensure_lookup_table()
    for e in bm.edges:
        if len(e.link_faces) != 2:
            # Boundary/non-manifold — leave smooth flag alone
            continue
        try:
            angle = e.calc_face_angle()
        except Exception:
            continue
        # bmesh.BMEdge.smooth: True = smooth edge, False = sharp.
        # Sharp when face fold exceeds threshold.
        e.smooth = (angle < sharp_angle)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()

    # 3. Find existing WeightedNormal modifier or create new
    mod = None
    for m in obj.modifiers:
        if m.type == 'WEIGHTED_NORMAL':
            mod = m
            break
    if mod is None:
        mod = obj.modifiers.new(name="WeightedNormal", type='WEIGHTED_NORMAL')
    # Match screenshot's settings.
    mod.mode = 'FACE_AREA'
    mod.weight = 72
    mod.thresh = 0.01
    mod.keep_sharp = True
    mod.use_face_influence = False


# ---------------------------------------------------------------------------
# v20.0 — Edge Set helpers
# ---------------------------------------------------------------------------
def _ensure_default_set(obj):
    """Guarantee at least one set exists. Migrates legacy stored_edges into
    a 'Default' set on first call."""
    ed = obj.eovc_data
    if len(ed.edge_sets) > 0:
        return ed.edge_sets[ed.active_set_index] if ed.active_set_index < len(ed.edge_sets) else ed.edge_sets[0]

    new_set = ed.edge_sets.add()
    new_set.name = "Default"
    new_set.color = ed.cut_color if ed.cut_color in ('RED', 'GREEN', 'BLUE') else 'RED'
    new_set.distance = ed.cut_distance if ed.cut_distance > 0 else 0.1
    new_set.flip = ed.cut_flip
    new_set.extra_lines = ed.cut_extra_lines
    new_set.intensity = 1.0

    # Migrate legacy stored_edges → Default set
    for legacy in ed.stored_edges:
        e = new_set.edges.add()
        e.v1, e.v2, e.color = legacy.v1, legacy.v2, legacy.color
    if len(ed.stored_edges) > 0:
        ed.stored_edges.clear()  # wipe legacy after migration

    ed.active_set_index = 0
    return new_set


def _active_set(obj):
    """Returns the currently-active EdgeSet, or None if no sets exist."""
    ed = obj.eovc_data
    if len(ed.edge_sets) == 0:
        return None
    idx = max(0, min(ed.active_set_index, len(ed.edge_sets) - 1))
    return ed.edge_sets[idx]


def _set_keys(edge_set):
    """Canonical (min,max) vert-index pair set for a given EdgeSet."""
    return {(min(e.v1, e.v2), max(e.v1, e.v2)) for e in edge_set.edges}


def _add_edges_to_set(edge_set, bm_edges):
    """Add iterable of BMEdges to a set (canonical, deduped). Returns count added."""
    existing = _set_keys(edge_set)
    added = 0
    for e in bm_edges:
        a, b = e.verts[0].index, e.verts[1].index
        key = (min(a, b), max(a, b))
        if key in existing:
            continue
        rec = edge_set.edges.add()
        rec.v1, rec.v2 = key
        rec.color = edge_set.color
        existing.add(key)
        added += 1
    return added


def _remove_edges_from_set(edge_set, bm_edges):
    """Remove iterable of BMEdges from a set. Returns count removed."""
    keys_to_remove = set()
    for e in bm_edges:
        a, b = e.verts[0].index, e.verts[1].index
        keys_to_remove.add((min(a, b), max(a, b)))

    indices_to_remove = []
    for i, rec in enumerate(edge_set.edges):
        key = (min(rec.v1, rec.v2), max(rec.v1, rec.v2))
        if key in keys_to_remove:
            indices_to_remove.append(i)
    for i in reversed(indices_to_remove):
        edge_set.edges.remove(i)
    return len(indices_to_remove)


def _all_set_keys(obj):
    """Aggregate of all (set, key) pairs — for whole-modifier diagnostics."""
    out = []
    for s in obj.eovc_data.edge_sets:
        for e in s.edges:
            out.append((s, (min(e.v1, e.v2), max(e.v1, e.v2))))
    return out


# ---------------------------------------------------------------------------
# Stored helpers (for vertex color step + Select Stored) — kept for legacy
# ---------------------------------------------------------------------------
def _stored_keys(obj):
    """Legacy: returns keys from old stored_edges. New code uses _set_keys."""
    return {(s.v1, s.v2) for s in obj.eovc_data.stored_edges}


def _resolve_stored_edges(obj, bm):
    keys = _stored_keys(obj)
    out = []
    for e in bm.edges:
        a, b = e.verts[0].index, e.verts[1].index
        if (min(a, b), max(a, b)) in keys:
            out.append(e)
    return out


def _purge_stale_stored(obj, bm):
    """Remove stored_edges entries that no longer exist in the mesh.
    Custom collection properties survive Undo, so a long Add → Undo →
    Add cycle accumulates stale references to edges that were split
    or merged out of existence by previous cut operations."""
    bm.edges.ensure_lookup_table()
    current_pairs = set()
    for e in bm.edges:
        a, b = e.verts[0].index, e.verts[1].index
        current_pairs.add((min(a, b), max(a, b)))

    store = obj.eovc_data.stored_edges
    stale = []
    for i, s in enumerate(store):
        key = (min(s.v1, s.v2), max(s.v1, s.v2))
        if key not in current_pairs:
            stale.append(i)
    for i in reversed(stale):
        store.remove(i)
    return len(stale)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------
class EOVC_OT_AddLoopCutEdge(bpy.types.Operator):
    bl_idname = "eovc.add_loop_cut_edge"
    bl_label = "Add Selected (Cut Loop Edge)"
    bl_description = (
        "Take the currently selected edges and CUT a parallel loop edge into "
        "the host face for each — IMMEDIATELY, in one step. No modifier, no "
        "overlay, no preview. Real Loop-Cut topology baked in. Press F9 after "
        "to tweak Distance / Flip (Blender will undo + re-run the operator)."
    )
    bl_options = {'REGISTER', 'UNDO'}

    distance: FloatProperty(
        name="Distance", default=0.1, min=0.0001, soft_max=10.0, unit='LENGTH',
        description="Perpendicular offset of the new loop edge",
    )
    flip_direction: BoolProperty(
        name="Flip Direction", default=False,
        description="Reverse the outward direction",
    )
    extra_lines: IntProperty(
        name="Extra Lines", default=0, min=0, max=2,
        description="Extra parallel lines between source and offset",
    )

    @classmethod
    def poll(cls, ctx):
        # v19.1: requires the modifier to exist on this mesh.
        return (ctx.object and ctx.object.type == 'MESH'
                and ctx.mode == 'EDIT_MESH'
                and ctx.object.eovc_data.has_modifier)

    def invoke(self, ctx, event):
        s = ctx.scene.eovc_settings
        self.distance = s.distance
        self.flip_direction = s.flip_direction
        self.extra_lines = s.extra_lines
        return self.execute(ctx)

    def execute(self, ctx):
        obj = ctx.object
        s = ctx.scene.eovc_settings
        s.distance = self.distance
        s.flip_direction = self.flip_direction
        s.extra_lines = self.extra_lines

        if self.distance <= 0.0:
            self.report({'WARNING'}, "Distance must be > 0")
            return {'CANCELLED'}

        # v19.1: modifier must exist (poll() ensures has_modifier=True).
        # If somehow source wasn't captured at Add Modifier time, capture now.
        if obj.eovc_data.source_mesh is None:
            _capture_source_clone(obj)

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        sel_all = [e for e in bm.edges if e.select]
        if not sel_all:
            self.report({'WARNING'}, "No edges selected")
            return {'CANCELLED'}

        # Auto-purge stale stored entries (left over from undone cuts whose
        # edges no longer exist in the mesh)
        purged = _purge_stale_stored(obj, bm)
        if purged:
            print(f"[loop_edge] purged {purged} stale stored entries")

        # Filter — only edges with a valid horizontal host AND not too steep.
        # Slope = Δz / edge_length. Curb walls are near-vertical (>>30%);
        # legitimate ramps (driveway cuts, kerb dips) are typically <30%
        # (~17°). Threshold rejects walls while accepting ramps.
        sel = []
        skipped_sloped = 0
        skipped_wall = 0
        SLOPE_MAX = 0.30
        for e in sel_all:
            z_diff = abs(e.verts[0].co.z - e.verts[1].co.z)
            edge_len = (e.verts[1].co - e.verts[0].co).length
            slope = (z_diff / edge_len) if edge_len > 1e-9 else 0.0
            if slope > SLOPE_MAX:
                skipped_sloped += 1
                continue
            host, _ = _pick_host_other(e)
            if host is None:
                skipped_wall += 1
                continue
            sel.append(e)

        if not sel:
            self.report({'WARNING'},
                        f"No valid edges. Skipped {skipped_sloped} sloped "
                        f"(Δz>1cm) + {skipped_wall} no-horizontal-host. "
                        f"Select flat edges along curb base.")
            return {'CANCELLED'}

        # v20.0: append edges to ACTIVE EdgeSet (auto-create Default if none).
        # Per-set color/distance/extra_lines override scene settings, but on
        # first add via UI we sync set's params to current scene settings so
        # the user gets what they see in the panel.
        active = _ensure_default_set(obj)
        # Sync set params to current scene settings (one-time on Add, like
        # the user editing them later via the set's controls)
        active.color = s.color
        active.distance = self.distance
        active.flip = self.flip_direction
        active.extra_lines = self.extra_lines

        added = _add_edges_to_set(active, sel)

        # v18.6: Multiple parallel lines.
        # extra_lines=0 → 1 cut at d.
        # extra_lines=1 → 2 cuts at d, d/2.
        # extra_lines=2 → 3 cuts at d, 2d/3, d/3.
        # MUST be cut in DESCENDING distance order: each cut splits the host
        # face into a strip + the rest, and the next cut goes into that strip.
        # If we cut smallest-first, the source's host_face becomes a tiny
        # strip and larger cuts won't fit inside it.
        n_extra = max(0, min(2, int(self.extra_lines)))
        n_total = n_extra + 1
        distances = [self.distance * (n_total - i) / n_total for i in range(n_total)]

        cut_count = 0
        for d in distances:
            cut_count += _do_loop_cut(bm, sel, d, self.flip_direction)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

        if skipped_sloped or skipped_wall:
            print(f"[loop_edge] filtered: {skipped_sloped} sloped + {skipped_wall} wall edges")

        # v18.7: auto-paint source edge verts with the selected vertex color.
        # Acts on the just-cut source edges only (not the entire history) —
        # multiple cuts with different colors then accumulate per-edge.
        rgba = COLOR_MAP[s.color]
        src_verts = set()
        for e in sel:
            try:
                if e.is_valid:
                    src_verts.add(e.verts[0])
                    src_verts.add(e.verts[1])
            except ReferenceError:
                pass

        painted = 0
        if src_verts:
            color_layer = bm.loops.layers.color.get(s.vc_layer_name)
            if color_layer is None:
                color_layer = bm.loops.layers.color.new(s.vc_layer_name)
            for f in bm.faces:
                for loop in f.loops:
                    if loop.vert in src_verts:
                        loop[color_layer] = rgba
                        painted += 1

        bm.normal_update()
        bmesh.update_edit_mesh(obj.data)

        # v19.0: stamp last-bake parameters so auto re-bake can reproduce
        obj.eovc_data.is_baked = True
        obj.eovc_data.cut_distance = self.distance
        obj.eovc_data.cut_flip = self.flip_direction
        obj.eovc_data.cut_extra_lines = self.extra_lines
        obj.eovc_data.cut_color = s.color

        line_word = "line" if n_total == 1 else "lines"
        self.report(
            {'INFO'} if cut_count else {'WARNING'},
            f"Loop Edge: {cut_count} cuts × {n_total} {line_word}, +{added} hist, "
            f"painted {painted} loops {s.color} "
            f"(d={self.distance:.3f}{', extra=' + str(n_extra) if n_extra else ''})"
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# v19.1 — Modifier lifecycle (add / remove per-mesh)
# ---------------------------------------------------------------------------
class EOVC_OT_AddModifier(bpy.types.Operator):
    """Add the Loop Edge modifier to this mesh. Captures current obj.data as
    the source snapshot. Until Add Selected is called the modifier is empty
    (no edges, no cut applied)."""
    bl_idname = "eovc.add_modifier"
    bl_label = "Add Modifier"
    bl_description = (
        "Add the Loop Edge pseudo-modifier to this mesh. Captures current "
        "geometry as 'source'. Required before Add Selected can run cuts."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH'
                and not obj.eovc_data.has_modifier)

    def execute(self, ctx):
        obj = ctx.object
        was_edit = (ctx.mode == 'EDIT_MESH')
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')

        _capture_source_clone(obj)
        obj.eovc_data.has_modifier = True
        obj.eovc_data.is_baked = False  # nothing baked yet — empty modifier

        if was_edit:
            bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"Modifier added to {obj.name}. Select edges → Add Selected.")
        return {'FINISHED'}


class EOVC_OT_RemoveModifier(bpy.types.Operator):
    """Remove the Loop Edge modifier. Restores source mesh as obj.data
    (baked geometry is discarded). History and source snapshot are cleared."""
    bl_idname = "eovc.remove_modifier"
    bl_label = "Remove Modifier"
    bl_description = (
        "Remove the modifier. Restores source mesh as obj.data and discards "
        "all baked cuts + history. The source mesh becomes the active mesh."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH'
                and obj.eovc_data.has_modifier)

    def execute(self, ctx):
        obj = ctx.object
        ed = obj.eovc_data
        was_edit = (ctx.mode == 'EDIT_MESH')
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')

        # Restore source as obj.data (so user keeps editable mesh).
        # Rename it back to the object's name for cleanliness.
        if ed.source_mesh is not None:
            ed.source_mesh.use_fake_user = False
            if obj.data is not ed.source_mesh:
                _swap_obj_data(obj, ed.source_mesh)
            # Rename mesh-data to match obj.name (drop _eovc_source_ prefix)
            try:
                obj.data.name = obj.name
            except Exception:
                pass

        # Clear all modifier state
        ed.stored_edges.clear()
        ed.edge_sets.clear()  # v20.1: also clear sets
        ed.active_set_index = 0
        ed.source_mesh = None
        ed.has_modifier = False
        ed.is_baked = False

        if was_edit:
            bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"Modifier removed from {obj.name}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# v19.0 — Pseudo-Modifier operators
# ---------------------------------------------------------------------------
class EOVC_OT_ToggleModifier(bpy.types.Operator):
    """Toggle between BAKED (cut visible) and SOURCE (raw mesh, editable).
    Equivalent to enabling/disabling a real modifier — but as a manual swap."""
    bl_idname = "eovc.toggle_modifier"
    bl_label = "Toggle Modifier"
    bl_description = (
        "Toggle between baked view (cut applied) and source view (pre-cut, "
        "editable). When swapping to source, edits propagate back through "
        "Re-bake. Cannot be used while in Edit Mode — exit first."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH'
                and ctx.mode == 'OBJECT'
                and obj.eovc_data.source_mesh is not None)

    def execute(self, ctx):
        obj = ctx.object
        ed = obj.eovc_data
        if ed.is_baked:
            # Currently showing baked → swap to source
            _swap_obj_data(obj, ed.source_mesh)
            ed.is_baked = False
            self.report({'INFO'}, "Modifier OFF — editing source mesh")
        else:
            # Currently showing source → re-bake. v20.14.1: don't blow up
            # when user deleted all edges — rebake.poll() requires
            # total_edges > 0 and would RuntimeError otherwise. Just
            # report a friendly message and stay in source mode.
            total_edges = sum(len(s.edges) for s in ed.edge_sets)
            if total_edges == 0:
                self.report(
                    {'WARNING'},
                    "No edges in any set — add edges before baking.")
                return {'CANCELLED'}
            return bpy.ops.eovc.rebake('EXEC_DEFAULT')
        return {'FINISHED'}


class EOVC_OT_Rebake(bpy.types.Operator):
    """Re-apply the cut + paint using current source mesh as input.
    Source is the editable raw geometry; baked = source + cut (a separate mesh).
    Run after editing the source to refresh the baked view."""
    bl_idname = "eovc.rebake"
    bl_label = "Re-bake Cut"
    bl_description = (
        "Clone the source mesh, apply cut + paint using stored history edges, "
        "and replace obj.data with the new baked mesh. Source mesh is preserved."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        if not (obj and obj.type == 'MESH'
                and ctx.mode == 'OBJECT'
                and obj.eovc_data.source_mesh is not None):
            return False
        # v20.0: at least one edge across all sets
        total_edges = sum(len(s.edges) for s in obj.eovc_data.edge_sets)
        return total_edges > 0

    def execute(self, ctx):
        obj = ctx.object
        ed = obj.eovc_data
        src = ed.source_mesh
        if src is None:
            self.report({'WARNING'}, "No source mesh — bake first via Add Selected")
            return {'CANCELLED'}

        # Clone source and apply each set's cut to the clone
        baked = src.copy()
        baked.name = f"_eovc_baked_{obj.name}"
        result = _apply_sets_to_mesh(
            baked,
            list(ed.edge_sets),
            ctx.scene.eovc_settings.vc_layer_name,
        )

        _swap_obj_data(obj, baked)
        ed.is_baked = True

        # v20.14.9 — apply shade smooth + WeightedNormal modifier to the
        # baked mesh so the outline strip blends smoothly with the host
        # surface (no visible facet seams between strip and surrounding
        # faces). Face Area weighting w/ Keep Sharp matches the screenshot
        # the user provided.
        try:
            _apply_shade_smooth_and_weighted_normal(obj)
        except Exception as exc:
            print(f"[eovc rebake] post-bake smooth failed: {exc!r}")

        if result['missing_total']:
            self.report({'WARNING'},
                f"Re-bake: {result['cuts']} cuts across {result['sets_baked']} sets, "
                f"missing {result['missing_total']} edges (verts moved/deleted)")
        else:
            self.report({'INFO'},
                f"Re-bake: {result['cuts']} cuts across {result['sets_baked']} sets, "
                f"painted {result['painted']} loops")
        return {'FINISHED'}


class EOVC_OT_ResetSource(bpy.types.Operator):
    """Drop the source snapshot. Next bake will re-capture from current state.
    Use when you want to permanently accept current edits as the new baseline."""
    bl_idname = "eovc.reset_source"
    bl_label = "Reset Source Snapshot"
    bl_description = (
        "Clear the saved source mesh. Next Add Selected will take a fresh "
        "snapshot of current obj.data. Use when current state should become "
        "the new editable baseline."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH'
                and obj.eovc_data.source_mesh is not None)

    def execute(self, ctx):
        obj = ctx.object
        ed = obj.eovc_data
        old = ed.source_mesh
        ed.source_mesh = None
        ed.is_baked = False
        if old and old is not obj.data:
            old.use_fake_user = False
            if old.users == 0:
                try:
                    bpy.data.meshes.remove(old)
                except Exception:
                    pass
        self.report({'INFO'}, "Source snapshot cleared")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# v19.0 — Auto Edit/Object mode swap (msgbus subscription)
# ---------------------------------------------------------------------------
_msgbus_owner = object()
_mode_state = {}  # obj_name -> last known mode

def _on_active_object_mode_change():
    """Fires when active object's mode changes. Implements modifier-like
    auto-swap: entering Edit Mode swaps obj.data to source_mesh; exiting
    Edit Mode triggers re-bake. Gated by has_modifier + auto_bake flags."""
    obj = bpy.context.active_object
    if obj is None or obj.type != 'MESH':
        return
    if not hasattr(obj, 'eovc_data'):
        return
    ed = obj.eovc_data
    if not ed.has_modifier:
        print(f"[eovc mode-change] obj={obj.name} skipped: has_modifier=False")
        return
    if not ed.auto_bake:
        print(f"[eovc mode-change] obj={obj.name} skipped: auto_bake=False")
        return
    if ed.source_mesh is None:
        print(f"[eovc mode-change] obj={obj.name} skipped: source_mesh=None")
        return

    last = _mode_state.get(obj.name)
    cur = obj.mode
    _mode_state[obj.name] = cur

    print(f"[eovc mode-change] obj={obj.name} last={last} cur={cur} "
          f"is_baked={ed.is_baked} obj.data={obj.data.name}")

    if last == cur:
        return  # no actual change

    if cur == 'EDIT' and ed.is_baked:
        # Entering Edit Mode while showing baked → swap to source for editing.
        # We need to be in OBJECT mode briefly to swap obj.data, so the user's
        # mode-set into EDIT happens AFTER swap. The msgbus fires AFTER mode
        # change, so we're already in EDIT — too late to swap cleanly. Use a
        # timer to defer: exit EDIT, swap, re-enter EDIT.
        print(f"[eovc mode-change] scheduling deferred_swap → source")
        def deferred_swap():
            try:
                print(f"[eovc deferred_swap] obj.mode={obj.mode} "
                      f"obj.data={obj.data.name} src={ed.source_mesh.name}")
                if obj.mode == 'EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                _swap_obj_data(obj, ed.source_mesh)
                ed.is_baked = False
                bpy.ops.object.mode_set(mode='EDIT')
                print(f"[eovc deferred_swap] DONE  obj.data={obj.data.name}  is_baked={ed.is_baked}")
            except Exception as exc:
                print(f"[eovc auto-swap to source] {exc!r}")
            return None  # don't repeat
        bpy.app.timers.register(deferred_swap, first_interval=0.01)

    elif cur == 'OBJECT' and not ed.is_baked and obj.data is ed.source_mesh:
        # Exiting Edit Mode and we were editing source → re-bake.
        print(f"[eovc mode-change] scheduling deferred_rebake")
        def deferred_rebake():
            try:
                bpy.ops.eovc.rebake('EXEC_DEFAULT')
            except Exception as exc:
                print(f"[eovc auto-rebake] {exc!r}")
            return None
        bpy.app.timers.register(deferred_rebake, first_interval=0.01)


def _register_msgbus():
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Object, "mode"),
        owner=_msgbus_owner,
        args=(),
        notify=_on_active_object_mode_change,
        options={'PERSISTENT'},
    )


def _unregister_msgbus():
    try:
        bpy.msgbus.clear_by_owner(_msgbus_owner)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v20.0 — Edge Set management operators
# ---------------------------------------------------------------------------
class EOVC_OT_SetAdd(bpy.types.Operator):
    """Create a new EdgeSet (Layer/Preset)."""
    bl_idname = "eovc.set_add"
    bl_label = "Add Set"
    bl_description = "Create a new edge set with default parameters"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        return ctx.object and ctx.object.type == 'MESH' and ctx.object.eovc_data.has_modifier

    def execute(self, ctx):
        ed = ctx.object.eovc_data
        new_set = ed.edge_sets.add()
        # Auto-name: Set 1, Set 2, ...
        existing_names = {s.name for s in ed.edge_sets}
        n = len(ed.edge_sets)
        candidate = f"Set {n}"
        while candidate in existing_names:
            n += 1
            candidate = f"Set {n}"
        new_set.name = candidate
        # v20.14.4: default color is always RED (was cycling RED/GREEN/BLUE
        # which made initial color unpredictable). User can re-pick from
        # the panel after creation.
        new_set.color = 'RED'
        ed.active_set_index = len(ed.edge_sets) - 1
        self.report({'INFO'}, f"Added '{new_set.name}'")
        return {'FINISHED'}


class EOVC_OT_SetRemove(bpy.types.Operator):
    """Remove the active EdgeSet (its edges are deleted with it).
    Auto re-bakes if currently baked, so removed set's cuts disappear."""
    bl_idname = "eovc.set_remove"
    bl_label = "Remove Set"
    bl_description = "Delete active set + its edges. Auto re-bakes to remove its cuts from baked mesh."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH'
                and obj.eovc_data.has_modifier
                and len(obj.eovc_data.edge_sets) > 0)

    def execute(self, ctx):
        obj = ctx.object
        ed = obj.eovc_data
        idx = ed.active_set_index
        if idx >= len(ed.edge_sets):
            return {'CANCELLED'}
        name = ed.edge_sets[idx].name
        edge_count = len(ed.edge_sets[idx].edges)
        ed.edge_sets.remove(idx)
        ed.active_set_index = max(0, min(idx, len(ed.edge_sets) - 1))

        # v20.14.4: also handle Edit Mode case — exit briefly, refresh
        # baked, then re-enter Edit Mode. Pre-fix: only OBJECT mode
        # refreshed baked, so deletion in Edit Mode left stale cuts on
        # the baked mesh until next mode toggle, and even then the
        # deferred-rebake could fail if no edges remained.
        was_edit = ctx.mode == 'EDIT_MESH'
        if ed.source_mesh is not None and ed.has_modifier:
            if was_edit:
                bpy.ops.object.mode_set(mode='OBJECT')

            still_have_edges = any(
                s.bake_enabled and len(s.edges) > 0 for s in ed.edge_sets)

            if still_have_edges:
                try:
                    bpy.ops.eovc.rebake('EXEC_DEFAULT')
                except Exception as exc:
                    print(f"[eovc set-remove] rebake failed: {exc!r}")
            else:
                # No edges left anywhere → show clean source.
                if obj.data is not ed.source_mesh:
                    _swap_obj_data(obj, ed.source_mesh)
                ed.is_baked = False

            if was_edit:
                bpy.ops.object.mode_set(mode='EDIT')

        suffix = " + refreshed bake" if ed.has_modifier else ""
        self.report({'INFO'},
                    f"Removed '{name}' ({edge_count} edges){suffix}")
        return {'FINISHED'}


class EOVC_OT_SetMove(bpy.types.Operator):
    """Move active set up/down in the list."""
    bl_idname = "eovc.set_move"
    bl_label = "Move Set"
    bl_description = "Reorder sets — affects bake order (later overlays earlier)"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        items=[('UP', "Up", ""), ('DOWN', "Down", "")],
        default='UP',
    )

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH'
                and obj.eovc_data.has_modifier
                and len(obj.eovc_data.edge_sets) > 1)

    def execute(self, ctx):
        ed = ctx.object.eovc_data
        idx = ed.active_set_index
        new_idx = idx - 1 if self.direction == 'UP' else idx + 1
        if 0 <= new_idx < len(ed.edge_sets):
            ed.edge_sets.move(idx, new_idx)
            ed.active_set_index = new_idx
        return {'FINISHED'}


class EOVC_OT_SetDuplicate(bpy.types.Operator):
    """Duplicate the active EdgeSet."""
    bl_idname = "eovc.set_duplicate"
    bl_label = "Duplicate Set"
    bl_description = "Copy the active set (edges + parameters)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH'
                and obj.eovc_data.has_modifier
                and len(obj.eovc_data.edge_sets) > 0)

    def execute(self, ctx):
        ed = ctx.object.eovc_data
        src = _active_set(ctx.object)
        if src is None:
            return {'CANCELLED'}
        dup = ed.edge_sets.add()
        dup.name = f"{src.name} (copy)"
        dup.color = src.color
        dup.intensity = src.intensity
        dup.distance = src.distance
        dup.extra_lines = src.extra_lines
        dup.flip = src.flip
        dup.visible = src.visible
        dup.bake_enabled = src.bake_enabled
        for e in src.edges:
            new_e = dup.edges.add()
            new_e.v1, new_e.v2, new_e.color = e.v1, e.v2, e.color
        ed.active_set_index = len(ed.edge_sets) - 1
        self.report({'INFO'}, f"Duplicated → '{dup.name}'")
        return {'FINISHED'}


def _trigger_immediate_bake(obj):
    """v20.2.1: after edge management op, exit Edit Mode → bake → stay in
    Object Mode so the user immediately sees the outline. Pressing Tab
    afterwards swaps to source and re-enters Edit Mode (via handler) for
    further editing.

    Why exit-and-stay (vs exit-bake-reenter): in Edit Mode obj.data must be
    SOURCE (raw), but baked outline lives on the BAKED mesh. They cannot
    be visible at the same time. So bake = Object Mode view; edit = Edit
    Mode source view. Toggle is the natural way to switch.

    Returns True if bake happened."""
    ed = obj.eovc_data
    if not ed.has_modifier:
        return False
    # v20.3.4: try to recover lost source_mesh pointer (e.g. after file load)
    if ed.source_mesh is None:
        if not _recover_source_mesh(obj):
            return False
    if not ed.auto_bake:
        return False  # user opted out — leave them in Edit Mode editing source

    # Exit Edit Mode → handler triggers rebake via timer in interactive
    # Blender. From script context, timers don't fire — call rebake directly.
    if bpy.context.mode == 'EDIT_MESH':
        bpy.ops.object.mode_set(mode='OBJECT')

    if any(s.bake_enabled and len(s.edges) > 0 for s in ed.edge_sets):
        try:
            bpy.ops.eovc.rebake('EXEC_DEFAULT')
        except Exception as exc:
            print(f"[eovc immediate-bake] rebake failed: {exc!r}")
    else:
        # No bake-enabled non-empty sets → swap to source so user sees clean
        if obj.data is not ed.source_mesh:
            _swap_obj_data(obj, ed.source_mesh)
        ed.is_baked = False
    return True


class EOVC_OT_SetAddSelected(bpy.types.Operator):
    """Add currently selected edges to the active set + immediate bake."""
    bl_idname = "eovc.set_add_selected"
    bl_label = "Add Edges"
    bl_description = (
        "Add selected edges to active set + immediately bake outline. "
        "Auto-toggles Edit/Object mode to apply cut. Disable Auto Bake to "
        "skip the immediate bake (use Re-bake later)."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH' and ctx.mode == 'EDIT_MESH'
                and obj.eovc_data.has_modifier)

    def execute(self, ctx):
        obj = ctx.object
        active = _ensure_default_set(obj)
        bm = bmesh.from_edit_mesh(obj.data)
        bm.edges.ensure_lookup_table()
        sel = [e for e in bm.edges if e.select]
        if not sel:
            self.report({'WARNING'}, "No edges selected")
            return {'CANCELLED'}
        added = _add_edges_to_set(active, sel)
        baked = _trigger_immediate_bake(obj)
        suffix = " + bake" if baked else ""
        self.report({'INFO'},
                    f"+{added} edges to '{active.name}' (now {len(active.edges)}){suffix}")
        return {'FINISHED'}


class EOVC_OT_SetDeleteSelected(bpy.types.Operator):
    """Remove currently selected edges from the active set + immediate bake."""
    bl_idname = "eovc.set_delete_selected"
    bl_label = "Delete Edges"
    bl_description = (
        "Remove selected edges from active set + immediately bake. "
        "Auto-toggles Edit/Object mode. Disable Auto Bake to skip."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH' and ctx.mode == 'EDIT_MESH'
                and obj.eovc_data.has_modifier
                and len(obj.eovc_data.edge_sets) > 0)

    def execute(self, ctx):
        obj = ctx.object
        active = _active_set(obj)
        if active is None:
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(obj.data)
        bm.edges.ensure_lookup_table()
        sel = [e for e in bm.edges if e.select]
        if not sel:
            self.report({'WARNING'}, "No edges selected")
            return {'CANCELLED'}
        removed = _remove_edges_from_set(active, sel)
        baked = _trigger_immediate_bake(obj)
        suffix = " + bake" if baked else ""
        self.report({'INFO'},
                    f"−{removed} edges from '{active.name}' (now {len(active.edges)}){suffix}")
        return {'FINISHED'}


class EOVC_OT_SetReplace(bpy.types.Operator):
    """Clear active set, then add currently selected edges."""
    bl_idname = "eovc.set_replace"
    bl_label = "Replace"
    bl_description = "Clear active set then add currently selected edges (replace contents)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH' and ctx.mode == 'EDIT_MESH'
                and obj.eovc_data.has_modifier)

    def execute(self, ctx):
        obj = ctx.object
        active = _ensure_default_set(obj)
        bm = bmesh.from_edit_mesh(obj.data)
        bm.edges.ensure_lookup_table()
        sel = [e for e in bm.edges if e.select]
        active.edges.clear()
        added = _add_edges_to_set(active, sel) if sel else 0
        self.report({'INFO'}, f"'{active.name}' → {added} edges (replaced)")
        return {'FINISHED'}


class EOVC_OT_SetPickHost(bpy.types.Operator):
    """Set the active set's host face from currently-selected face in Edit Mode.
    Use when auto-pick algorithm picks the wrong face for chains crossing
    irregular topology (interior edges, multi-face plates etc)."""
    bl_idname = "eovc.set_pick_host"
    bl_label = "Pick Host Face"
    bl_description = (
        "Use currently selected face as the host for active set's cuts. "
        "In Edit Mode: switch to Face select, click 1 face, then press this. "
        "Overrides auto-pick algorithm. Click 'Clear Host' to revert to auto."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH' and ctx.mode == 'EDIT_MESH'
                and obj.eovc_data.has_modifier
                and len(obj.eovc_data.edge_sets) > 0)

    def execute(self, ctx):
        obj = ctx.object
        active = _active_set(obj)
        if active is None:
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        sel_faces = [f for f in bm.faces if f.select]
        if len(sel_faces) != 1:
            self.report({'WARNING'}, f"Select exactly 1 face (got {len(sel_faces)})")
            return {'CANCELLED'}
        active.host_face_index = sel_faces[0].index
        self.report({'INFO'}, f"Host set: face#{sel_faces[0].index} for '{active.name}'")
        return {'FINISHED'}


class EOVC_OT_SetClearHost(bpy.types.Operator):
    """Clear explicit host on active set (revert to auto-pick)."""
    bl_idname = "eovc.set_clear_host"
    bl_label = "Clear Host"
    bl_description = "Revert active set to auto-pick host face."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH'
                and obj.eovc_data.has_modifier
                and len(obj.eovc_data.edge_sets) > 0)

    def execute(self, ctx):
        obj = ctx.object
        active = _active_set(obj)
        if active is None:
            return {'CANCELLED'}
        active.host_face_index = -1
        self.report({'INFO'}, f"'{active.name}' host cleared (auto-pick)")
        return {'FINISHED'}


class EOVC_OT_SetSelectEdges(bpy.types.Operator):
    """Select all edges of the active set in the viewport."""
    bl_idname = "eovc.set_select_edges"
    bl_label = "Select Set Edges"
    bl_description = "Select all edges of the active set in viewport"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        obj = ctx.object
        return (obj and obj.type == 'MESH' and ctx.mode == 'EDIT_MESH'
                and obj.eovc_data.has_modifier
                and len(obj.eovc_data.edge_sets) > 0)

    def execute(self, ctx):
        obj = ctx.object
        active = _active_set(obj)
        if active is None:
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        for v in bm.verts: v.select = False
        for e in bm.edges: e.select = False
        for f in bm.faces: f.select = False
        keys = _set_keys(active)
        for e in bm.edges:
            a, b = e.verts[0].index, e.verts[1].index
            if (min(a, b), max(a, b)) in keys:
                e.select_set(True)
        bm.select_flush(True)
        bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UIList for displaying edge sets
# ---------------------------------------------------------------------------
class EOVC_UL_EdgeSets(bpy.types.UIList):
    """Outliner-style list of EdgeSets with quick controls."""
    def draw_item(self, ctx, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "visible", text="",
                     icon='HIDE_OFF' if item.visible else 'HIDE_ON',
                     emboss=False)
            row.prop(item, "bake_enabled", text="",
                     icon='RESTRICT_RENDER_OFF' if item.bake_enabled else 'RESTRICT_RENDER_ON',
                     emboss=False)
            color_icon_map = {'RED': 'COLORSET_01_VEC',
                              'GREEN': 'COLORSET_03_VEC',
                              'BLUE': 'COLORSET_04_VEC'}
            row.label(text="", icon=color_icon_map.get(item.color, 'NONE'))
            row.prop(item, "name", text="", emboss=False)
            row.label(text=f"{len(item.edges)}")


class EOVC_OT_ClearStored(bpy.types.Operator):
    bl_idname = "eovc.clear_stored"
    bl_label = "Clear History"
    bl_description = (
        "Empty the source-edges history (used by Vertex Color step). Does "
        "NOT undo loop cuts already in the mesh — those are real geometry. "
        "Use Ctrl+Z or the Undo Last Cut button for that."
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        return ctx.object and ctx.object.type == 'MESH'

    def execute(self, ctx):
        obj = ctx.object
        n = len(obj.eovc_data.stored_edges)
        obj.eovc_data.stored_edges.clear()
        # Force a redraw so the panel updates immediately
        for area in ctx.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        self.report({'INFO'}, f"Cleared {n} entries from history")
        return {'FINISHED'}


class EOVC_OT_UndoLastCut(bpy.types.Operator):
    bl_idname = "eovc.undo_last_cut"
    bl_label = "Undo Last Cut"
    bl_description = "Undo the most recent loop cut (= Ctrl+Z, but as a button)"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, ctx):
        return True

    def execute(self, ctx):
        try:
            bpy.ops.ed.undo()
            self.report({'INFO'}, "Undone last operation")
        except Exception as exc:
            self.report({'WARNING'}, f"Undo failed: {exc}")
        return {'FINISHED'}


class EOVC_OT_SelectStored(bpy.types.Operator):
    bl_idname = "eovc.select_stored"
    bl_label = "Select Stored"
    bl_description = "Select all source edges previously cut (still in mesh)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        return ctx.object and ctx.object.type == 'MESH' and ctx.mode == 'EDIT_MESH'

    def execute(self, ctx):
        obj = ctx.object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False

        for e in _resolve_stored_edges(obj, bm):
            e.select_set(True)

        bm.select_flush(True)
        bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}


class EOVC_OT_ApplyVertexColor(bpy.types.Operator):
    bl_idname = "eovc.apply_vertex_color"
    bl_label = "Apply Vertex Color"
    bl_description = "Paint stored source edges' vertices with the chosen color"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        return ctx.object and ctx.object.type == 'MESH'

    def execute(self, ctx):
        obj = ctx.object
        settings = ctx.scene.eovc_settings
        rgba = COLOR_MAP[settings.color]

        was_edit = (ctx.mode == 'EDIT_MESH')
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')

        mesh = obj.data
        for s in obj.eovc_data.stored_edges:
            s.color = settings.color

        layer_name = settings.vc_layer_name
        attr = mesh.color_attributes.get(layer_name)
        if attr is None:
            attr = mesh.color_attributes.new(name=layer_name, type='BYTE_COLOR', domain='CORNER')
        mesh.color_attributes.active_color = attr

        target_verts = set()
        for s in obj.eovc_data.stored_edges:
            target_verts.add(s.v1)
            target_verts.add(s.v2)

        if not target_verts:
            if was_edit:
                bpy.ops.object.mode_set(mode='EDIT')
            self.report({'WARNING'}, "No stored edges to paint")
            return {'CANCELLED'}

        if attr.domain == 'CORNER':
            for poly in mesh.polygons:
                for li in poly.loop_indices:
                    vi = mesh.loops[li].vertex_index
                    if vi in target_verts:
                        attr.data[li].color = rgba
        else:
            for vi in target_verts:
                attr.data[vi].color = rgba

        mesh.update()
        if was_edit:
            bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"Painted {len(target_verts)} verts with {settings.color}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class EOVC_PT_Panel(bpy.types.Panel):
    bl_label = "Loop Edge & Vertex Color"
    bl_idname = "EOVC_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_order = 2
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, ctx):
        layout = self.layout
        s = ctx.scene.eovc_settings
        obj = ctx.object

        # Version header
        ver = ".".join(str(n) for n in bl_info["version"])
        layout.label(text=f"Addon v{ver}", icon='MESH_GRID')

        if not (obj and obj.type == 'MESH'):
            layout.label(text="Select a mesh object", icon='INFO')
            return

        ed = obj.eovc_data

        # ============ MODIFIER section (top-level, gates everything below) ============
        mod_box = layout.box()
        if not ed.has_modifier:
            mod_box.label(text="No Loop Edge Modifier on this mesh", icon='MODIFIER')
            mod_box.operator("eovc.add_modifier",
                             text="+ Add Loop Edge Modifier",
                             icon='ADD')
            mod_box.label(text="Required: adds modifier first, then cut", icon='INFO')
            return  # nothing else to show

        # === Modifier exists — show all controls ===
        state = "BAKED (cut visible)" if ed.is_baked else "SOURCE (raw, editable)"
        header_row = mod_box.row(align=True)
        header_row.label(text=f"Modifier: {state}", icon='MODIFIER')
        header_row.operator("eovc.remove_modifier", text="", icon='X')

        mod_box.prop(ed, "auto_bake", text="Auto Bake on Edit Mode toggle")
        row = mod_box.row(align=True)
        toggle_text = "Disable (→ Source)" if ed.is_baked else "Enable (→ Baked)"
        toggle_icon = 'HIDE_OFF' if ed.is_baked else 'HIDE_ON'
        row.operator("eovc.toggle_modifier", text=toggle_text, icon=toggle_icon)
        row.operator("eovc.rebake", text="Re-bake", icon='FILE_REFRESH')
        if ed.auto_bake:
            mod_box.label(text="Edit Mode → source. Object Mode → re-bake.", icon='INFO')
        else:
            mod_box.label(text="Auto Bake OFF — use buttons above", icon='INFO')

        # ============ EDGE SETS (Layers / Presets) ============
        sets_box = layout.box()
        sets_box.label(text=f"Edge Sets ({len(ed.edge_sets)})", icon='OUTLINER_OB_GROUP_INSTANCE')
        list_row = sets_box.row()
        list_row.template_list("EOVC_UL_EdgeSets", "", ed, "edge_sets",
                               ed, "active_set_index", rows=4)
        col = list_row.column(align=True)
        col.operator("eovc.set_add", text="", icon='ADD')
        col.operator("eovc.set_remove", text="", icon='REMOVE')
        col.separator()
        col.operator("eovc.set_move", text="", icon='TRIA_UP').direction = 'UP'
        col.operator("eovc.set_move", text="", icon='TRIA_DOWN').direction = 'DOWN'
        col.separator()
        col.operator("eovc.set_duplicate", text="", icon='DUPLICATE')

        active = _active_set(obj) if len(ed.edge_sets) > 0 else None
        if active is not None:
            params_box = layout.box()
            params_box.label(text=f"Active: {active.name}", icon='PROPERTIES')
            params_box.prop(active, "color", expand=True)
            params_box.prop(active, "intensity")
            params_box.prop(active, "distance")
            params_box.prop(active, "extra_lines")
            params_box.prop(active, "flip")
            # v20.7: algorithm toggle. Mitered = vertex-centric (bisector).
            params_box.prop(active, "algorithm", expand=True)
            row = params_box.row(align=True)
            row.prop(active, "visible", icon='HIDE_OFF' if active.visible else 'HIDE_ON', toggle=True)
            row.prop(active, "bake_enabled",
                     icon='RESTRICT_RENDER_OFF' if active.bake_enabled else 'RESTRICT_RENDER_ON',
                     toggle=True)

            # v20.5: Host face override
            host_row = params_box.row(align=True)
            if active.host_face_index >= 0:
                host_row.label(text=f"Host: face#{active.host_face_index}", icon='FACESEL')
                host_row.operator("eovc.set_clear_host", text="", icon='X')
            else:
                host_row.label(text="Host: Auto", icon='FACESEL')
                host_row.operator("eovc.set_pick_host", text="Pick", icon='RESTRICT_SELECT_OFF')

        # ============ EDGE MANAGEMENT ============
        # Always visible — Add will auto-create "Default" set on first use.
        # v20.2: stripped to just + Add / − Delete (Select via UIList click,
        # Replace via Delete-all-then-Add — cleaner workflow).
        edges_box = layout.box()
        if active is not None:
            edges_box.label(text=f"Edges in '{active.name}': {len(active.edges)}",
                            icon='EDGESEL')
        else:
            edges_box.label(text="No sets yet — '+ Add' creates 'Default'",
                            icon='EDGESEL')
        if ctx.mode == 'EDIT_MESH':
            row = edges_box.row(align=True)
            row.scale_y = 1.3
            row.operator("eovc.set_add_selected", text="+ Add", icon='ADD')
            sub = row.row(align=True)
            sub.scale_y = 1.3
            sub.enabled = active is not None
            sub.operator("eovc.set_delete_selected", text="− Delete", icon='REMOVE')
        else:
            edges_box.label(text="Enter Edit Mode to manage edges", icon='INFO')

        # ============ OTHER ============
        box = layout.box()
        box.label(text="Other", icon='QUESTION')
        box.operator("eovc.reset_source", text="Reset Source Snapshot",
                     icon='LOOP_BACK')
        box.prop(s, "vc_layer_name", text="VC Layer")
        # v20.11.1: per-vert BEVEL diagnostics dump (look in System
        # Console / Window > Toggle System Console on macOS-via-launcher)
        box.prop(ctx.window_manager, "eovc_bevel_verbose",
                 text="Verbose BEVEL Logs (console)",
                 icon='CONSOLE')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
classes = (
    EOVC_StoredEdge,
    EOVC_EdgeSet,
    EOVC_ObjectData,
    EOVC_Settings,
    EOVC_OT_AddLoopCutEdge,
    EOVC_OT_AddModifier,
    EOVC_OT_RemoveModifier,
    EOVC_OT_ToggleModifier,
    EOVC_OT_Rebake,
    EOVC_OT_ResetSource,
    EOVC_OT_SetAdd,
    EOVC_OT_SetRemove,
    EOVC_OT_SetMove,
    EOVC_OT_SetDuplicate,
    EOVC_OT_SetAddSelected,
    EOVC_OT_SetDeleteSelected,
    EOVC_OT_SetReplace,
    EOVC_OT_SetSelectEdges,
    EOVC_OT_SetPickHost,
    EOVC_OT_SetClearHost,
    EOVC_UL_EdgeSets,
    EOVC_OT_ClearStored,
    EOVC_OT_UndoLastCut,
    EOVC_OT_SelectStored,
    EOVC_OT_ApplyVertexColor,
    EOVC_PT_Panel,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Object.eovc_data = PointerProperty(type=EOVC_ObjectData)
    bpy.types.Scene.eovc_settings = PointerProperty(type=EOVC_Settings)
    # v20.11.1: WindowManager-scoped verbose flag for bevel diagnostics.
    bpy.types.WindowManager.eovc_bevel_verbose = BoolProperty(
        name="Verbose BEVEL Logs",
        description="Print per-vert shift/projection/dz diagnostics to console",
        default=False,
    )
    _register_msgbus()


def unregister():
    _unregister_msgbus()
    del bpy.types.Object.eovc_data
    del bpy.types.Scene.eovc_settings
    if hasattr(bpy.types.WindowManager, 'eovc_bevel_verbose'):
        del bpy.types.WindowManager.eovc_bevel_verbose
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
