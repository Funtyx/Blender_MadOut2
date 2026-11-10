bl_info = {
    "name": "Surface Decal Projector",
    "author": "Claude",
    "version": (1, 4, 1),
    "blender": (4, 0, 0),
    "location": "3D View > N > Decal  |  Add Modifier > Geometry Nodes > Surface Decal",
    "description": "Live-project a mesh onto another surface: it sticks and reshapes while you drag it",
    "category": "Object",
}

import math
import bpy

GROUP_NAME = "Surface Decal"
SDP_VERSION = 5
MODIFIER_NAME = "Surface Decal"

SOCKETS = (
    # (name, socket_type, default, extra)
    ("Target", 'NodeSocketObject', None, {}),
    ("Subdivisions X", 'NodeSocketInt', 2, {"min_value": 0, "max_value": 6}),
    ("Subdivisions Y", 'NodeSocketInt', 2, {"min_value": 0, "max_value": 6}),
    ("Offset", 'NodeSocketFloat', 0.01, {"subtype": 'DISTANCE', "min_value": -10.0, "max_value": 10.0}),
    ("Ray Length", 'NodeSocketFloat', 100.0, {"subtype": 'DISTANCE', "min_value": 0.0}),
    ("Ray Direction", 'NodeSocketVector', (0.0, 0.0, -1.0), {}),  # world space
)


# ---------------------------------------------------------------- helpers

def sock(collection, name, index):
    for s in collection:
        if s.name == name:
            return s
    return collection[index]


def socket_struct(mod, ident):
    """Blender 5 moved the modifier's input values out of ID properties and
    into mod.properties.inputs.<identifier>. Returns None on older builds."""
    try:
        return getattr(mod.properties.inputs, ident)
    except Exception:
        return None


def mod_get(mod, ident, default=None):
    st = socket_struct(mod, ident)
    if st is not None:
        try:
            return st.value
        except Exception:
            pass
    try:
        return mod[ident]
    except Exception:
        return default


def mod_set(mod, ident, value):
    st = socket_struct(mod, ident)
    if st is not None:
        try:
            st.value = value
            return True
        except Exception:
            pass
    try:
        mod[ident] = value
        return True
    except Exception:
        return False


def mod_prop(layout, mod, ident, text):
    st = socket_struct(mod, ident)
    if st is not None:
        layout.prop(st, "value", text=text)
        return True
    try:
        layout.prop(mod, '["%s"]' % ident, text=text)
        return True
    except Exception:
        return False


def group_inputs(ng):
    """{socket name: identifier} for the group's non-geometry inputs."""
    out = {}
    try:
        for item in ng.interface.items_tree:
            if getattr(item, "item_type", "") == 'SOCKET' and item.in_out == 'INPUT':
                if item.socket_type != 'NodeSocketGeometry':
                    out[item.name] = item.identifier
    except Exception:
        pass
    return out


def ensure_interface(ng):
    have = set()
    try:
        for item in ng.interface.items_tree:
            if getattr(item, "item_type", "") == 'SOCKET':
                have.add((item.name, item.in_out))
    except Exception:
        pass
    if ("Geometry", 'INPUT') not in have:
        ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    for name, stype, default, extra in SOCKETS:
        if (name, 'INPUT') in have:
            continue
        s = ng.interface.new_socket(name, in_out='INPUT', socket_type=stype)
        if default is not None:
            try:
                s.default_value = default
            except Exception:
                pass
        for k, v in extra.items():
            try:
                setattr(s, k, v)
            except Exception:
                pass
    if ("Geometry", 'OUTPUT') not in have:
        ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')


def reorder_interface(ng):
    """Keep the panel in the order SOCKETS declares, whatever was added when.
    items_tree numbers inputs and outputs in one run and the output sits first,
    so we sort the inputs into the slots the inputs already occupy - Geometry
    has to stay the first input or the modifier gets no geometry at all."""
    want = ["Geometry"] + [name for name, _t, _d, _e in SOCKETS]

    def inputs_now():
        return [i for i in ng.interface.items_tree
                if getattr(i, "item_type", "") == 'SOCKET' and i.in_out == 'INPUT']

    try:
        for pos, name in enumerate(want):
            current = inputs_now()
            if pos >= len(current):
                break
            item = next((i for i in current[pos:] if i.name == name), None)
            if item is None:
                continue
            slot = current[pos].index
            if item.index != slot:
                ng.interface.move(item, slot)
    except Exception:
        pass


def upgrade_subdiv_socket(ng):
    """v3 had a single 'Subdivisions'. Split it in two, keeping the old value
    on both sides so nothing visibly changes until the artist pulls them apart."""
    old = None
    try:
        for item in ng.interface.items_tree:
            if (getattr(item, "item_type", "") == 'SOCKET'
                    and item.in_out == 'INPUT' and item.name == "Subdivisions"):
                old = item
                break
    except Exception:
        return
    if old is None:
        return

    ident = old.identifier
    saved = []
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type == 'NODES' and mod.node_group is ng:
                value = mod_get(mod, ident)
                if value is not None:
                    try:
                        saved.append((mod, int(value)))
                    except Exception:
                        pass
    try:
        ng.interface.remove(old)
    except Exception:
        return

    ensure_interface(ng)
    ids = group_inputs(ng)
    for mod, value in saved:
        for name in ("Subdivisions X", "Subdivisions Y"):
            if name in ids:
                mod_set(mod, ids[name], value)


def build_nodes(ng):
    ng.nodes.clear()
    n = ng.nodes
    lk = ng.links.new

    gin = n.new('NodeGroupInput')
    gin.location = (-1200, 0)
    gout = n.new('NodeGroupOutput')
    gout.location = (700, 0)

    # --- add geometry so a flat plane can actually follow the surface.
    #     Subdivide Mesh cuts both directions at once, so for a different
    #     density per side we cut at the denser level and then pull the spare
    #     rows on the coarser side back onto the lines that side really owns -
    #     those come from subdividing the original mesh at its own level, so
    #     nothing is ever snapped onto a grid the mesh does not have.
    lvl_x = gin.outputs["Subdivisions X"]
    lvl_y = gin.outputs["Subdivisions Y"]

    lvl_max = n.new('ShaderNodeMath')
    lvl_max.location = (-1020, 420)
    lvl_max.operation = 'MAXIMUM'
    lk(lvl_x, lvl_max.inputs[0])
    lk(lvl_y, lvl_max.inputs[1])

    subdiv = n.new('GeometryNodeSubdivideMesh')
    subdiv.location = (-860, 240)
    lk(gin.outputs["Geometry"], subdiv.inputs["Mesh"])
    lk(lvl_max.outputs[0], subdiv.inputs["Level"])

    def flat_on_axis(key, x, y):
        """Position with everything but this one component zeroed, so that
        'nearest point' collapses into 'nearest line across this axis'."""
        p = n.new('GeometryNodeInputPosition')
        p.location = (x, y + 160)
        sep = n.new('ShaderNodeSeparateXYZ')
        sep.location = (x + 160, y + 160)
        lk(p.outputs["Position"], sep.inputs[0])
        flat = n.new('ShaderNodeCombineXYZ')
        flat.location = (x + 320, y + 160)
        lk(sep.outputs[key], flat.inputs[key])
        return sep, flat.outputs["Vector"]

    def axis_lines(key, lvl, y):
        """The rows this side actually has at its own level, as a point cloud."""
        ref = n.new('GeometryNodeSubdivideMesh')
        ref.location = (-1020, y)
        lk(gin.outputs["Geometry"], ref.inputs["Mesh"])
        lk(lvl, ref.inputs["Level"])

        _sep, flat = flat_on_axis(key, -1020, y + 200)

        pts = n.new('GeometryNodeMeshToPoints')
        pts.location = (-560, y)
        pts.mode = 'VERTICES'
        lk(ref.outputs["Mesh"], pts.inputs["Mesh"])
        lk(flat, pts.inputs["Position"])
        return pts.outputs["Points"]

    def snap_axis(geo, key, lvl, lines, y):
        """Move every vertex onto the nearest row this side owns - the rows the
        mesh already has stay exactly where they are, so nothing is distorted."""
        sep, flat = flat_on_axis(key, -400, y)

        prox = n.new('GeometryNodeProximity')
        prox.location = (80, y)
        prox.target_element = 'POINTS'
        lk(lines, sock(prox.inputs, "Geometry", 0))
        lk(flat, sock(prox.inputs, "Sample Position", 2))

        psep = n.new('ShaderNodeSeparateXYZ')
        psep.location = (260, y)
        lk(sock(prox.outputs, "Position", 0), psep.inputs[0])

        comb = n.new('ShaderNodeCombineXYZ')
        comb.location = (420, y)
        for k in "XYZ":
            lk((psep if k == key else sep).outputs[k], comb.inputs[k])

        setp = n.new('GeometryNodeSetPosition')
        setp.location = (580, y)
        lk(geo, setp.inputs["Geometry"])
        lk(comb.outputs["Vector"], setp.inputs["Position"])

        coarser = n.new('ShaderNodeMath')
        coarser.location = (580, y - 220)
        coarser.operation = 'LESS_THAN'
        lk(lvl, coarser.inputs[0])
        lk(lvl_max.outputs[0], coarser.inputs[1])

        sw = n.new('GeometryNodeSwitch')
        sw.location = (760, y)
        sw.input_type = 'GEOMETRY'
        lk(coarser.outputs[0], sock(sw.inputs, "Switch", 0))
        lk(geo, sock(sw.inputs, "False", 1))
        lk(setp.outputs["Geometry"], sock(sw.inputs, "True", 2))
        return sock(sw.outputs, "Output", 0)

    dense = snap_axis(subdiv.outputs["Mesh"], "X", lvl_x,
                      axis_lines("X", lvl_x, 2100), 1560)
    dense = snap_axis(dense, "Y", lvl_y,
                      axis_lines("Y", lvl_y, 1200), 700)

    # --- weld the rows that just landed on top of each other. They are exactly
    #     coincident, so the threshold only has to clear float noise - anything
    #     bigger would start eating the decal's own detail.
    bbox = n.new('GeometryNodeBoundBox')
    bbox.location = (940, 300)
    lk(gin.outputs["Geometry"], bbox.inputs["Geometry"])

    span = n.new('ShaderNodeVectorMath')
    span.location = (1100, 300)
    span.operation = 'SUBTRACT'
    lk(sock(bbox.outputs, "Max", 2), span.inputs[0])
    lk(sock(bbox.outputs, "Min", 1), span.inputs[1])

    diag = n.new('ShaderNodeVectorMath')
    diag.location = (1260, 300)
    diag.operation = 'LENGTH'
    lk(span.outputs["Vector"], diag.inputs[0])

    eps = n.new('ShaderNodeMath')
    eps.location = (1420, 300)
    eps.operation = 'MULTIPLY'
    eps.inputs[1].default_value = 1e-5
    lk(sock(diag.outputs, "Value", 1), eps.inputs[0])

    eps_safe = n.new('ShaderNodeMath')
    eps_safe.location = (1580, 300)
    eps_safe.operation = 'MAXIMUM'
    eps_safe.inputs[1].default_value = 1e-7
    lk(eps.outputs[0], eps_safe.inputs[0])

    merge = n.new('GeometryNodeMergeByDistance')
    merge.location = (1760, 700)
    # "Connected" only welds along existing edges - an "All" weld at any
    # threshold would happily fuse parts of the decal that never touched
    for where, value in ((merge, 'CONNECTED'),
                         (merge.inputs.get("Mode"), 'Connected'),
                         (merge.inputs.get("Mode"), 'CONNECTED')):
        if where is None:
            continue
        try:
            if where is merge:
                merge.mode = value
            else:
                where.default_value = value
            break
        except Exception:
            continue
    lk(dense, merge.inputs["Geometry"])
    lk(eps_safe.outputs[0], merge.inputs["Distance"])

    gap = n.new('ShaderNodeMath')
    gap.location = (1420, 60)
    gap.operation = 'SUBTRACT'
    lk(lvl_x, gap.inputs[0])
    lk(lvl_y, gap.inputs[1])

    gap_abs = n.new('ShaderNodeMath')
    gap_abs.location = (1580, 60)
    gap_abs.operation = 'ABSOLUTE'
    lk(gap.outputs[0], gap_abs.inputs[0])

    uneven = n.new('ShaderNodeMath')
    uneven.location = (1740, 60)
    uneven.operation = 'GREATER_THAN'
    uneven.inputs[1].default_value = 0.5
    lk(gap_abs.outputs[0], uneven.inputs[0])

    sw_merge = n.new('GeometryNodeSwitch')
    sw_merge.location = (1940, 700)
    sw_merge.input_type = 'GEOMETRY'
    lk(uneven.outputs[0], sock(sw_merge.inputs, "Switch", 0))
    lk(dense, sock(sw_merge.inputs, "False", 1))
    lk(merge.outputs["Geometry"], sock(sw_merge.inputs, "True", 2))
    dense = sock(sw_merge.outputs, "Output", 0)

    # --- the surface we project onto, brought into this object's local space
    objinfo = n.new('GeometryNodeObjectInfo')
    objinfo.location = (-960, -560)
    objinfo.transform_space = 'RELATIVE'
    lk(gin.outputs["Target"], objinfo.inputs["Object"])

    # --- our own transform, so the ray stays world-down whatever the object's
    #     rotation is (a decal rotated 90 deg on X used to shoot sideways)
    selfobj = n.new('GeometryNodeSelfObject')
    selfobj.location = (-1200, -820)
    selfinfo = n.new('GeometryNodeObjectInfo')
    selfinfo.location = (-1020, -820)
    selfinfo.transform_space = 'ORIGINAL'
    lk(selfobj.outputs[0], selfinfo.inputs["Object"])

    # world direction -> local direction
    localdir = n.new('ShaderNodeVectorRotate')
    localdir.location = (-800, -820)
    localdir.rotation_type = 'EULER_XYZ'
    localdir.invert = True
    lk(gin.outputs["Ray Direction"], localdir.inputs["Vector"])
    lk(selfinfo.outputs["Rotation"], sock(localdir.inputs, "Rotation", 4))

    dirnorm = n.new('ShaderNodeVectorMath')
    dirnorm.location = (-620, -820)
    dirnorm.operation = 'NORMALIZE'
    lk(localdir.outputs["Vector"], dirnorm.inputs[0])

    # world distance -> local distance (average scale)
    sxyz = n.new('ShaderNodeSeparateXYZ')
    sxyz.location = (-800, -1060)
    lk(selfinfo.outputs["Scale"], sxyz.inputs[0])

    add1 = n.new('ShaderNodeMath')
    add1.location = (-620, -1060)
    add1.operation = 'ADD'
    lk(sxyz.outputs["X"], add1.inputs[0])
    lk(sxyz.outputs["Y"], add1.inputs[1])

    add2 = n.new('ShaderNodeMath')
    add2.location = (-460, -1060)
    add2.operation = 'ADD'
    lk(add1.outputs[0], add2.inputs[0])
    lk(sxyz.outputs["Z"], add2.inputs[1])

    avg = n.new('ShaderNodeMath')
    avg.location = (-300, -1060)
    avg.operation = 'DIVIDE'
    avg.inputs[1].default_value = 3.0
    lk(add2.outputs[0], avg.inputs[0])

    safe = n.new('ShaderNodeMath')
    safe.location = (-150, -1060)
    safe.operation = 'MAXIMUM'
    safe.inputs[1].default_value = 0.0001
    lk(avg.outputs[0], safe.inputs[0])

    len_local = n.new('ShaderNodeMath')
    len_local.location = (0, -900)
    len_local.operation = 'DIVIDE'
    lk(gin.outputs["Ray Length"], len_local.inputs[0])
    lk(safe.outputs[0], len_local.inputs[1])

    off_local = n.new('ShaderNodeMath')
    off_local.location = (0, -1060)
    off_local.operation = 'DIVIDE'
    lk(gin.outputs["Offset"], off_local.inputs[0])
    lk(safe.outputs[0], off_local.inputs[1])

    # --- start the ray half a ray-length "above" the point, so surfaces both
    #     above and below the decal are found
    pos = n.new('GeometryNodeInputPosition')
    pos.location = (-960, 480)

    half = n.new('ShaderNodeMath')
    half.location = (180, -900)
    half.operation = 'MULTIPLY'
    half.inputs[1].default_value = 0.5
    lk(len_local.outputs[0], half.inputs[0])

    back = n.new('ShaderNodeVectorMath')
    back.location = (-440, -620)
    back.operation = 'SCALE'
    lk(dirnorm.outputs["Vector"], back.inputs[0])
    lk(half.outputs[0], sock(back.inputs, "Scale", 3))

    start = n.new('ShaderNodeVectorMath')
    start.location = (-260, 360)
    start.operation = 'SUBTRACT'
    lk(pos.outputs["Position"], start.inputs[0])
    lk(back.outputs["Vector"], start.inputs[1])

    ray = n.new('GeometryNodeRaycast')
    ray.location = (-40, -60)
    lk(objinfo.outputs["Geometry"], sock(ray.inputs, "Target Geometry", 0))
    lk(start.outputs["Vector"], sock(ray.inputs, "Source Position", 2))
    lk(dirnorm.outputs["Vector"], sock(ray.inputs, "Ray Direction", 3))
    lk(len_local.outputs[0], sock(ray.inputs, "Ray Length", 4))

    # --- lift off the surface a touch so it does not z-fight
    lift = n.new('ShaderNodeVectorMath')
    lift.location = (200, -300)
    lift.operation = 'SCALE'
    lk(sock(ray.outputs, "Hit Normal", 2), lift.inputs[0])
    lk(off_local.outputs[0], sock(lift.inputs, "Scale", 3))

    final = n.new('ShaderNodeVectorMath')
    final.location = (380, -160)
    final.operation = 'ADD'
    lk(sock(ray.outputs, "Hit Position", 1), final.inputs[0])
    lk(lift.outputs["Vector"], final.inputs[1])

    setpos = n.new('GeometryNodeSetPosition')
    setpos.location = (500, 200)
    lk(dense, setpos.inputs["Geometry"])
    lk(sock(ray.outputs, "Is Hit", 0), setpos.inputs["Selection"])
    lk(final.outputs["Vector"], setpos.inputs["Position"])

    lk(setpos.outputs["Geometry"], gout.inputs["Geometry"])


def ensure_node_group(force=False):
    """Create the group, or rebuild its innards when the addon was updated.
    The interface is left alone, so existing modifiers keep their values."""
    ng = bpy.data.node_groups.get(GROUP_NAME)
    if ng is None or ng.bl_idname != 'GeometryNodeTree':
        ng = bpy.data.node_groups.new(GROUP_NAME, 'GeometryNodeTree')
        try:
            ng.is_modifier = True
        except Exception:
            pass
        ensure_interface(ng)
        build_nodes(ng)
        ng["sdp_version"] = SDP_VERSION
        return ng

    if force or ng.get("sdp_version", 0) != SDP_VERSION:
        upgrade_subdiv_socket(ng)
        ensure_interface(ng)
        reorder_interface(ng)
        build_nodes(ng)
        ng["sdp_version"] = SDP_VERSION
    return ng


def decal_modifier(obj):
    if obj is None:
        return None
    for m in obj.modifiers:
        if m.type == 'NODES' and m.node_group and m.node_group.name.startswith(GROUP_NAME):
            return m
    return None


def decal_object(context):
    """Active object if it carries the modifier, else the first selected one that does."""
    obj = context.object
    if decal_modifier(obj) is not None:
        return obj
    for o in context.selected_objects:
        if decal_modifier(o) is not None:
            return o
    return None


def axis_edge_lengths(obj, sample=4000):
    """Average edge length along the object's own X and Y, in world units.
    Every edge is counted on whichever of the two it mostly runs along."""
    me = obj.data
    edges = me.edges
    if not edges:
        return 0.0, 0.0
    verts = me.vertices
    step = max(1, len(edges) // sample)
    totals = [0.0, 0.0]
    counts = [0, 0]
    for i in range(0, len(edges), step):
        a, b = edges[i].vertices
        d = verts[b].co - verts[a].co
        ax = 0 if abs(d.x) >= abs(d.y) else 1
        if abs(d[ax]) < 1e-9:
            continue
        totals[ax] += abs(d[ax])
        counts[ax] += 1
    sx = abs(obj.scale.x) or 1.0
    sy = abs(obj.scale.y) or 1.0
    out_x = (totals[0] / counts[0]) * sx if counts[0] else 0.0
    out_y = (totals[1] / counts[1]) * sy if counts[1] else 0.0
    return out_x, out_y


def average_edge_length(obj, sample=2000):
    me = obj.data
    edges = me.edges
    if not edges:
        return 0.0
    verts = me.vertices
    step = max(1, len(edges) // sample)
    total, count = 0.0, 0
    for i in range(0, len(edges), step):
        a, b = edges[i].vertices
        total += (verts[a].co - verts[b].co).length
        count += 1
    if count == 0:
        return 0.0
    scale = sum(abs(v) for v in obj.scale) / 3.0 or 1.0
    return (total / count) * scale


# ---------------------------------------------------------------- operators

class SDP_OT_add(bpy.types.Operator):
    bl_idname = "object.sdp_add"
    bl_label = "Add Surface Decal"
    bl_description = ("Add the projection modifier to the selected mesh(es). "
                      "Select the decal(s), then shift-select the surface last - "
                      "the active object becomes the target")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH'

    def execute(self, context):
        ng = ensure_node_group()
        ids = group_inputs(ng)
        active = context.object
        selected = [o for o in context.selected_objects if o.type == 'MESH']

        if len(selected) >= 2 and active in selected:
            target = active
            decals = [o for o in selected if o is not active]
        else:
            target = None
            decals = selected or ([active] if active else [])

        if not decals:
            self.report({'WARNING'}, "Nothing to add the modifier to")
            return {'CANCELLED'}

        n = 0
        for obj in decals:
            mod = decal_modifier(obj)
            if mod is None:
                mod = obj.modifiers.new(MODIFIER_NAME, 'NODES')
                mod.node_group = ng
            if target is not None and "Target" in ids:
                mod_set(mod, ids["Target"], target)
            obj.update_tag()
            n += 1

        if target is None:
            self.report({'WARNING'},
                        "Added to %d object(s) - now pick the surface in the modifier's Target field" % n)
        else:
            self.report({'INFO'}, "Added to %d object(s), target: %s" % (n, target.name))
        return {'FINISHED'}


class SDP_OT_set_target(bpy.types.Operator):
    bl_idname = "object.sdp_set_target"
    bl_label = "Use Active as Surface"
    bl_description = "Set the active object as the projection target for the other selected objects"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object is not None and len(context.selected_objects) >= 2

    def execute(self, context):
        active = context.object
        ids = {}
        n = 0
        for obj in context.selected_objects:
            if obj is active or obj.type != 'MESH':
                continue
            mod = decal_modifier(obj)
            if mod is None or mod.node_group is None:
                continue
            if mod.node_group.name not in ids:
                ids[mod.node_group.name] = group_inputs(mod.node_group)
            ident = ids[mod.node_group.name].get("Target")
            if ident and mod_set(mod, ident, active):
                obj.update_tag()
                n += 1
        self.report({'INFO'}, "Target set on %d object(s)" % n)
        return {'FINISHED'}


class SDP_OT_auto_detail(bpy.types.Operator):
    bl_idname = "object.sdp_auto_detail"
    bl_label = "Auto Detail"
    bl_description = "Pick a subdivision level so the decal is at least as dense as the surface under it"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return decal_object(context) is not None

    def execute(self, context):
        obj = decal_object(context)
        mod = decal_modifier(obj)
        ids = group_inputs(mod.node_group)
        target = None
        if "Target" in ids:
            target = mod_get(mod, ids["Target"])
        if target is None or target.type != 'MESH':
            self.report({'WARNING'}, "No target surface set")
            return {'CANCELLED'}

        decal_edge = average_edge_length(obj)
        target_edge = average_edge_length(target)
        if decal_edge <= 0 or target_edge <= 0:
            self.report({'WARNING'}, "Could not measure the meshes")
            return {'CANCELLED'}

        # each side gets its own level: a long thin decal needs plenty of cuts
        # down its length and hardly any across it
        edge_x, edge_y = axis_edge_lengths(obj)

        def level_for(edge):
            if edge <= 0:
                edge = decal_edge
            # the decal should end up at least as dense as the surface,
            # otherwise it cuts corners across the bumps
            return max(0, min(6, int(math.ceil(math.log(edge / target_edge, 2)))))

        lx, ly = level_for(edge_x), level_for(edge_y)
        for name, value in (("Subdivisions X", lx), ("Subdivisions Y", ly)):
            if name in ids:
                mod_set(mod, ids[name], value)
        obj.update_tag()
        self.report({'INFO'}, "Subdivisions X %d / Y %d (decal %.3f x %.3f / surface %.3f)"
                    % (lx, ly, edge_x, edge_y, target_edge))
        return {'FINISHED'}


class SDP_OT_apply(bpy.types.Operator):
    bl_idname = "object.sdp_apply"
    bl_label = "Apply"
    bl_description = "Freeze the projection into real geometry"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = decal_object(context)
        return obj is not None and obj.mode == 'OBJECT'

    def execute(self, context):
        obj = decal_object(context)
        mod = decal_modifier(obj)
        try:
            context.view_layer.objects.active = obj
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "Projection applied")
        return {'FINISHED'}


class SDP_OT_remove(bpy.types.Operator):
    bl_idname = "object.sdp_remove"
    bl_label = "Remove"
    bl_description = "Remove the projection modifier"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return decal_object(context) is not None

    def execute(self, context):
        obj = decal_object(context)
        mod = decal_modifier(obj)
        obj.modifiers.remove(mod)
        obj.update_tag()
        return {'FINISHED'}


# ---------------------------------------------------------------- UI

def draw_panel(layout, context):
    obj = context.object

    row = layout.row(align=True)
    row.scale_y = 1.4
    row.operator("object.sdp_add", icon='MOD_SHRINKWRAP')

    obj = decal_object(context)
    if obj is None:
        return
    mod = decal_modifier(obj)
    if mod is None:
        col = layout.column()
        col.label(text="Select the decal, then shift-select", icon='INFO')
        col.label(text="the surface last, and press the button.")
        return

    layout.separator()
    box = layout.box()
    box.label(text=obj.name, icon='OUTLINER_OB_MESH')

    ng = mod.node_group
    if ng is not None:
        try:
            for item in ng.interface.items_tree:
                if getattr(item, "item_type", "") != 'SOCKET':
                    continue
                if item.in_out != 'INPUT' or item.socket_type == 'NodeSocketGeometry':
                    continue
                mod_prop(box, mod, item.identifier, item.name)
        except Exception:
            box.label(text="Edit the values in the Modifier tab", icon='MODIFIER')

    row = box.row(align=True)
    row.operator("object.sdp_auto_detail", icon='MESH_GRID')
    row.operator("object.sdp_set_target", text="", icon='EYEDROPPER')

    row = layout.row(align=True)
    row.operator("object.sdp_apply", icon='CHECKMARK')
    row.operator("object.sdp_remove", text="", icon='X')
    layout.operator("object.sdp_rebuild", text="", icon='FILE_REFRESH')

    layout.separator()
    col = layout.column(align=True)
    col.label(text="Drag the decal - it follows the surface.", icon='INFO')
    col.label(text="Ray Direction is in world space.")


class SDP_OT_rebuild(bpy.types.Operator):
    bl_idname = "object.sdp_rebuild"
    bl_label = "Rebuild Node Group"
    bl_description = "Regenerate the Surface Decal node group after an add-on update"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ensure_node_group(force=True)
        for o in bpy.data.objects:
            if decal_modifier(o) is not None:
                o.update_tag()
        self.report({'INFO'}, "Node group rebuilt")
        return {'FINISHED'}


class SDP_PT_view3d(bpy.types.Panel):
    bl_label = "Surface Decal"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_order = 4
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        draw_panel(self.layout, context)


# ---------------------------------------------------------------- register

classes = (
    SDP_OT_add,
    SDP_OT_rebuild,
    SDP_OT_set_target,
    SDP_OT_auto_detail,
    SDP_OT_apply,
    SDP_OT_remove,
    SDP_PT_view3d,
)


def _upgrade_existing():
    """Bring a group saved by an older add-on version up to date."""
    try:
        if bpy.data.node_groups.get(GROUP_NAME) is not None:
            ensure_node_group()
            for o in bpy.data.objects:
                if decal_modifier(o) is not None:
                    o.update_tag()
    except Exception:
        pass
    return None


@bpy.app.handlers.persistent
def _on_load(_dummy):
    _upgrade_existing()


def register():
    for c in classes:
        bpy.utils.register_class(c)
    try:
        bpy.app.timers.register(_upgrade_existing, first_interval=0.5)
    except Exception:
        pass
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


def _purge_previous():
    names = {c.__name__ for c in classes}
    for base in (bpy.types.Panel, bpy.types.Operator):
        for c in list(base.__subclasses__()):
            if c.__name__ in names and getattr(c, "is_registered", False):
                try:
                    bpy.utils.unregister_class(c)
                except Exception:
                    pass


if __name__ == "__main__":
    _purge_previous()
    register()
    print("Surface Decal Projector 1.4.1 registered")
