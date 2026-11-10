bl_info = {
    "name": "Vertex Color Blend Converter",
    "author": "Claude",
    "version": (2, 0, 1),
    "blender": (4, 0, 0),
    "location": "3D View > N > VC Blend  |  Shader Editor > N > VC Blend  |  Properties > Material",
    "description": "4-slot vertex-colour blend: convert a material, then paint or fill the mask from one panel",
    "category": "Material",
}

import bpy

MARK = "vc_blend"
ORIG_NODE = "vc_blend_orig_node"
ORIG_SOCK = "vc_blend_orig_socket"
ORIG_COL = "vc_blend_orig_color"

BASE_DEF = ("BASE", "00_BASE_black", (0.020, 0.020, 0.020, 1.0))

LAYER_DEFS = (
    ("R", "01_RED",   (0.750, 0.070, 0.070, 1.0)),
    ("G", "02_GREEN", (0.080, 0.600, 0.120, 1.0)),
    ("B", "03_BLUE",  (0.070, 0.220, 0.850, 1.0)),
)

CHANNELS = ("BASE", "R", "G", "B")

CHANNEL_LABEL = {"BASE": "Base", "R": "Red", "G": "Green", "B": "Blue"}
CHANNEL_SLOT = {"BASE": "00_BASE", "R": "01_RED", "G": "02_GREEN", "B": "03_BLUE"}
CHANNEL_OUT = {"R": "Red", "G": "Green", "B": "Blue"}

CHANNEL_COLOR = {
    "BASE": (0.0, 0.0, 0.0),
    "R": (1.0, 0.0, 0.0),
    "G": (0.0, 1.0, 0.0),
    "B": (0.0, 0.0, 1.0),
}

DEFAULT_BRUSH_SIZE = 50


# ---------------------------------------------------------------- node helpers

def sock(collection, identifier, index):
    for s in collection:
        if s.identifier == identifier:
            return s
    return collection[index]


def placeholder_image(name, color):
    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.new(name, width=32, height=32, alpha=False)
    img.generated_type = 'BLANK'
    img.generated_color = color
    return img


def find_principled(nt):
    out = None
    for n in nt.nodes:
        if n.type == 'OUTPUT_MATERIAL' and n.is_active_output:
            out = n
            break
    if out is not None:
        link = next((l for l in nt.links if l.to_node == out and l.to_socket.name == "Surface"), None)
        if link and link.from_node.type == 'BSDF_PRINCIPLED':
            return link.from_node
    return next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)


def is_converted(mat):
    if not mat or not mat.use_nodes:
        return False
    return any(n.get(MARK) for n in mat.node_tree.nodes)


def clear_marked(nt):
    for n in [n for n in nt.nodes if n.get(MARK)]:
        nt.nodes.remove(n)


def base_node_of(mat):
    if not is_converted(mat):
        return None
    nt = mat.node_tree
    mix = nt.nodes.get("VC_MIX_R")
    if mix is None:
        return None
    a = sock(mix.inputs, "A_Color", 6)
    if a.links:
        return a.links[0].from_node
    return None


def slot_node(mat, channel):
    if not is_converted(mat):
        return None
    nt = mat.node_tree
    if channel == "BASE":
        return nt.nodes.get("VC_TEX_BASE") or base_node_of(mat)
    return nt.nodes.get("VC_TEX_" + channel)


def mask_attr_of(mat, fallback):
    if is_converted(mat):
        vc = mat.node_tree.nodes.get("VC_MASK")
        if vc and vc.layer_name:
            return vc.layer_name
    return fallback


# ---------------------------------------------------------------- core

def revert_material(mat):
    if not mat or not mat.use_nodes or not is_converted(mat):
        return "not converted"
    nt = mat.node_tree
    bsdf = find_principled(nt)
    orig_node = mat.get(ORIG_NODE, "")
    orig_sock = mat.get(ORIG_SOCK, "")
    orig_col = mat.get(ORIG_COL, None)
    clear_marked(nt)
    if bsdf is None:
        return None
    base_in = bsdf.inputs["Base Color"]
    if orig_node and orig_node in nt.nodes:
        n = nt.nodes[orig_node]
        s = n.outputs.get(orig_sock) or n.outputs[0]
        nt.links.new(s, base_in)
    elif orig_col is not None:
        base_in.default_value = tuple(orig_col)
    return None


def convert_material(mat, attr_name, base_mode='KEEP', keep_existing_uv=True):
    if mat is None:
        return "no material"

    if not mat.use_nodes:
        base_rgba = tuple(mat.diffuse_color)
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = find_principled(nt)
        if bsdf:
            bsdf.inputs["Base Color"].default_value = base_rgba

    nt = mat.node_tree
    bsdf = find_principled(nt)
    if bsdf is None:
        return "no Principled BSDF"

    if is_converted(mat):
        revert_material(mat)
        bsdf = find_principled(nt)

    base_in = bsdf.inputs["Base Color"]

    src_link = next((l for l in nt.links if l.to_socket == base_in), None)
    if src_link:
        mat[ORIG_NODE] = src_link.from_node.name
        mat[ORIG_SOCK] = src_link.from_socket.name
        anchor_x = src_link.from_node.location.x
        anchor_y = src_link.from_node.location.y
        existing_source = src_link.from_socket
    else:
        mat[ORIG_NODE] = ""
        mat[ORIG_SOCK] = ""
        mat[ORIG_COL] = list(base_in.default_value)
        anchor_x = bsdf.location.x - 900
        anchor_y = bsdf.location.y + 300
        existing_source = None

    uv = None
    if keep_existing_uv:
        uv = next((n for n in nt.nodes if n.type == 'UVMAP' and not n.get(MARK)), None)
    if uv is None:
        uv = nt.nodes.new("ShaderNodeUVMap")
        uv[MARK] = True
        uv.location = (anchor_x - 300, anchor_y - 900)

    frame = nt.nodes.new("NodeFrame")
    frame[MARK] = True
    frame.label = "Vertex Color Blend"

    if base_mode == 'NEW' or existing_source is None:
        ch, nm, col = BASE_DEF
        tex0 = nt.nodes.new("ShaderNodeTexImage")
        tex0[MARK] = True
        tex0.name = "VC_TEX_BASE"
        tex0.label = nm
        tex0.location = (anchor_x + 60, anchor_y + 60)
        tex0.image = placeholder_image("VCB_%s_%s" % (mat.name, nm), col)
        tex0.parent = frame
        nt.links.new(uv.outputs["UV"], tex0.inputs["Vector"])
        base_source = tex0.outputs["Color"]
    else:
        base_source = existing_source

    vc = nt.nodes.new("ShaderNodeVertexColor")
    vc[MARK] = True
    vc.name = "VC_MASK"
    vc.label = "Vertex mask"
    vc.layer_name = attr_name
    vc.location = (anchor_x + 60, anchor_y - 1150)
    vc.parent = frame

    sep = nt.nodes.new("ShaderNodeSeparateColor")
    sep[MARK] = True
    sep.mode = 'RGB'
    sep.location = (anchor_x + 280, anchor_y - 1150)
    sep.parent = frame
    nt.links.new(vc.outputs["Color"], sep.inputs["Color"])

    prev = base_source
    for i, (ch, nm, col) in enumerate(LAYER_DEFS):
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex[MARK] = True
        tex.name = "VC_TEX_" + ch
        tex.label = nm
        tex.location = (anchor_x + 60, anchor_y - 250 - i * 290)
        tex.image = placeholder_image("VCB_%s_%s" % (mat.name, nm), col)
        tex.parent = frame
        nt.links.new(uv.outputs["UV"], tex.inputs["Vector"])

        mix = nt.nodes.new("ShaderNodeMix")
        mix[MARK] = True
        mix.name = "VC_MIX_" + ch
        mix.label = "Mix " + nm
        mix.data_type = 'RGBA'
        mix.blend_type = 'MIX'
        mix.clamp_factor = True
        mix.location = (anchor_x + 420 + i * 210, anchor_y - 120 - i * 180)
        mix.parent = frame

        nt.links.new(sep.outputs[CHANNEL_OUT[ch]], sock(mix.inputs, "Factor_Float", 0))
        nt.links.new(prev, sock(mix.inputs, "A_Color", 6))
        nt.links.new(tex.outputs["Color"], sock(mix.inputs, "B_Color", 7))
        prev = sock(mix.outputs, "Result_Color", 2)

    nt.links.new(prev, base_in)
    return None


def ensure_attribute(obj, attr_name):
    me = obj.data
    ca = me.color_attributes.get(attr_name)
    created = False
    if ca is None:
        ca = me.color_attributes.new(name=attr_name, type='BYTE_COLOR', domain='CORNER')
        for d in ca.data:
            d.color = (0.0, 0.0, 0.0, 1.0)
        created = True
    try:
        me.color_attributes.active_color = ca
    except Exception:
        pass
    try:
        me.attributes.active_color_name = attr_name
        me.attributes.default_color_name = attr_name
    except Exception:
        pass
    return created


def set_material_preview(context):
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for sp in area.spaces:
                if sp.type == 'VIEW_3D':
                    if sp.shading.type not in {'MATERIAL', 'RENDERED'}:
                        sp.shading.type = 'MATERIAL'


def paint_owners(context):
    """Every object that may hold the brush colour / size in this Blender."""
    ts = context.tool_settings
    vp = getattr(ts, "vertex_paint", None)
    brush = getattr(vp, "brush", None) if vp else None
    owners = [
        getattr(ts, "unified_paint_settings", None),
        getattr(vp, "unified_paint_settings", None) if vp else None,
        brush,
    ]
    return brush, [o for o in owners if o is not None]


def selection_counts(obj):
    if obj is None or obj.type != 'MESH':
        return 0, 0
    me = obj.data
    return getattr(me, "total_vert_sel", 0), getattr(me, "total_face_sel", 0)


def masking_on(obj):
    if obj is None or obj.type != 'MESH':
        return False
    me = obj.data
    return bool(getattr(me, "use_paint_mask", False) or getattr(me, "use_paint_mask_vertex", False))


class object_mode:
    def __init__(self, context):
        self.context = context
        self.prev = None

    def __enter__(self):
        obj = self.context.object
        if obj and obj.mode != 'OBJECT':
            self.prev = obj.mode
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                self.prev = None
        return self

    def __exit__(self, *a):
        if self.prev:
            try:
                bpy.ops.object.mode_set(mode=self.prev)
            except Exception:
                pass
        return False


# ---------------------------------------------------------------- settings

def _swatch(channel):
    """Read-only colour indicator (the panel draws it disabled)."""
    return bpy.props.FloatVectorProperty(
        name=CHANNEL_LABEL[channel],
        subtype='COLOR', size=3, min=0.0, max=1.0,
        default=CHANNEL_COLOR[channel],
    )


class VCBSettings(bpy.types.PropertyGroup):
    attr_name: bpy.props.StringProperty(
        name="Attribute",
        description="Colour attribute the blend mask is read from",
        default="Blend4",
    )
    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[
            ('ACTIVE', "Active slot", "Only the material in the active slot"),
            ('OBJECT', "All slots of object", "Every material on the active object"),
            ('SELECTED', "All selected objects", "Every material on every selected object"),
        ],
        default='ACTIVE',
    )
    base_mode: bpy.props.EnumProperty(
        name="Base slot",
        items=[
            ('KEEP', "Keep current", "The material's existing Base Color becomes slot 0"),
            ('NEW', "New texture slot", "Slot 0 gets its own image texture node"),
        ],
        default='KEEP',
    )
    show_advanced: bpy.props.BoolProperty(
        name="Advanced",
        description="Material conversion and texture slots",
        default=False,
    )
    active_channel: bpy.props.StringProperty(default="")

    col_BASE: _swatch("BASE")
    col_R: _swatch("R")
    col_G: _swatch("G")
    col_B: _swatch("B")


# ---------------------------------------------------------------- operators

def gather(context):
    settings = context.scene.vcb
    obj = context.object
    mats = []
    if settings.scope == 'ACTIVE':
        if obj and obj.active_material:
            mats.append(obj.active_material)
    elif settings.scope == 'OBJECT':
        if obj:
            mats += [s.material for s in obj.material_slots if s.material]
    else:
        for o in context.selected_objects:
            mats += [s.material for s in o.material_slots if s.material]
    seen, out = set(), []
    for m in mats:
        if m.name not in seen:
            seen.add(m.name)
            out.append(m)
    return out


class VCB_OT_convert(bpy.types.Operator):
    bl_idname = "material.vcb_convert"
    bl_label = "Convert to Vertex Blend"
    bl_description = "Rewire the material in place: slot 0 shows on black vertices, the R/G/B channels blend three texture slots over it"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        s = context.scene.vcb
        mats = gather(context)
        if not mats:
            self.report({'WARNING'}, "No material found")
            return {'CANCELLED'}
        ok, fail = 0, []
        for m in mats:
            err = convert_material(m, s.attr_name, s.base_mode)
            if err:
                fail.append("%s (%s)" % (m.name, err))
            else:
                ok += 1
        made = 0
        with object_mode(context):
            targets = [o for o in context.selected_objects if o.type == 'MESH']
            if context.object and context.object.type == 'MESH' and context.object not in targets:
                targets.append(context.object)
            for o in targets:
                try:
                    if ensure_attribute(o, s.attr_name):
                        made += 1
                except Exception as exc:
                    fail.append("%s attr (%s)" % (o.name, exc))
        msg = "Converted %d material(s), %d new '%s' attribute(s)" % (ok, made, s.attr_name)
        if fail:
            self.report({'WARNING'}, msg + " - skipped: " + ", ".join(fail))
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


class VCB_OT_revert(bpy.types.Operator):
    bl_idname = "material.vcb_revert"
    bl_label = "Revert"
    bl_description = "Remove the vertex-blend nodes and restore the original Base Color"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        n = 0
        for m in gather(context):
            if revert_material(m) is None:
                n += 1
        self.report({'INFO'}, "Reverted %d material(s)" % n)
        return {'FINISHED'}


class VCB_OT_paint(bpy.types.Operator):
    bl_idname = "paint.vcb_paint"
    bl_label = "Paint"
    bl_description = "Enter Vertex Paint with the brush set to this channel"
    bl_options = {'REGISTER', 'UNDO'}

    channel: bpy.props.StringProperty(default="R")

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        s = context.scene.vcb
        obj = context.object
        attr = mask_attr_of(obj.active_material, s.attr_name)

        if obj.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass
        ensure_attribute(obj, attr)
        set_material_preview(context)

        try:
            bpy.ops.object.mode_set(mode='VERTEX_PAINT')
        except Exception as exc:
            self.report({'ERROR'}, "Cannot enter Vertex Paint: %s" % exc)
            return {'CANCELLED'}

        col = CHANNEL_COLOR.get(self.channel, (1.0, 0.0, 0.0))
        brush, owners = paint_owners(context)
        for owner in owners:
            try:
                owner.color = col
            except Exception:
                pass
        if brush is not None:
            for name, value in (("blend", 'MIX'), ("strength", 1.0)):
                try:
                    setattr(brush, name, value)
                except Exception:
                    pass

        s.active_channel = self.channel

        verts, faces = selection_counts(obj)
        if masking_on(obj) and verts == 0 and faces == 0:
            self.report({'WARNING'},
                        "Selection masking is on and nothing is selected - the brush will not paint")
        else:
            self.report({'INFO'}, "Painting %s into '%s'" % (CHANNEL_LABEL[self.channel], attr))
        return {'FINISHED'}


class VCB_OT_fill_selection(bpy.types.Operator):
    bl_idname = "paint.vcb_fill_selection"
    bl_label = "Fill selection"
    bl_description = "Write this channel's colour into the selected vertices only"
    bl_options = {'REGISTER', 'UNDO'}

    channel: bpy.props.StringProperty(default="R")

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        s = context.scene.vcb
        obj = context.object
        attr = mask_attr_of(obj.active_material, s.attr_name)
        col = tuple(CHANNEL_COLOR.get(self.channel, (1.0, 0.0, 0.0))) + (1.0,)

        with object_mode(context):
            me = obj.data
            ensure_attribute(obj, attr)
            ca = me.color_attributes.get(attr)
            if ca is None:
                self.report({'ERROR'}, "No '%s' attribute" % attr)
                return {'CANCELLED'}
            n = 0
            if ca.domain == 'CORNER':
                verts = me.vertices
                for loop in me.loops:
                    if verts[loop.vertex_index].select:
                        ca.data[loop.index].color = col
                        n += 1
            else:
                for i, v in enumerate(me.vertices):
                    if v.select:
                        ca.data[i].color = col
                        n += 1
            me.update()

        set_material_preview(context)
        if n == 0:
            self.report({'WARNING'}, "Nothing selected")
            return {'CANCELLED'}
        self.report({'INFO'}, "Filled %d element(s) with %s" % (n, CHANNEL_LABEL[self.channel]))
        return {'FINISHED'}


class VCB_OT_reset_tool(bpy.types.Operator):
    bl_idname = "paint.vcb_reset_tool"
    bl_label = "Reset tool"
    bl_description = ("Put the paint tool back to a sane state: masking off, "
                      "blend Mix, full strength, normal brush size, Material Preview")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.object
        s = context.scene.vcb

        for flag in ("use_paint_mask", "use_paint_mask_vertex"):
            try:
                setattr(obj.data, flag, False)
            except Exception:
                pass

        brush, owners = paint_owners(context)
        for owner in owners:
            try:
                owner.size = DEFAULT_BRUSH_SIZE
            except Exception:
                pass
            try:
                owner.strength = 1.0
            except Exception:
                pass
        if brush is not None:
            try:
                brush.blend = 'MIX'
            except Exception:
                pass

        attr = mask_attr_of(obj.active_material, s.attr_name)
        with object_mode(context):
            try:
                ensure_attribute(obj, attr)
            except Exception:
                pass
        set_material_preview(context)

        self.report({'INFO'}, "Tool reset: masking off, brush %d px, strength 1.0" % DEFAULT_BRUSH_SIZE)
        return {'FINISHED'}


class VCB_OT_fill_black(bpy.types.Operator):
    bl_idname = "paint.vcb_fill_black"
    bl_label = "Reset mask to Base"
    bl_description = "Fill the whole mask of selected meshes with black - slot 0 everywhere"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH'

    def execute(self, context):
        s = context.scene.vcb
        targets = [o for o in context.selected_objects if o.type == 'MESH']
        if context.object and context.object.type == 'MESH' and context.object not in targets:
            targets.append(context.object)
        n = 0
        with object_mode(context):
            for o in targets:
                attr = mask_attr_of(o.active_material, s.attr_name)
                ca = o.data.color_attributes.get(attr)
                if ca:
                    for d in ca.data:
                        d.color = (0.0, 0.0, 0.0, 1.0)
                    o.data.update()
                    n += 1
        self.report({'INFO'}, "Reset %d attribute(s) to base" % n)
        return {'FINISHED'}


class VCB_OT_end_paint(bpy.types.Operator):
    bl_idname = "paint.vcb_end"
    bl_label = "Done painting"
    bl_description = "Leave Vertex Paint and go back to Object Mode"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.mode == 'VERTEX_PAINT'

    def execute(self, context):
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass
        context.scene.vcb.active_channel = ""
        return {'FINISHED'}


# ---------------------------------------------------------------- UI

def draw_brush_settings(layout, context):
    ts = context.tool_settings
    vp = getattr(ts, "vertex_paint", None)
    brush = getattr(vp, "brush", None) if vp else None
    if brush is None:
        return
    col = layout.column(align=True)
    try:
        from bl_ui.properties_paint_common import UnifiedPaintPanel
        UnifiedPaintPanel.prop_unified(col, context, brush, "size",
                                       unified_name="use_unified_size",
                                       slider=True, text="Size")
        UnifiedPaintPanel.prop_unified(col, context, brush, "strength",
                                       unified_name="use_unified_strength",
                                       text="Strength")
    except Exception:
        try:
            col.prop(brush, "size", slider=True)
            col.prop(brush, "strength", slider=True)
        except Exception:
            pass


def draw_channel_row(layout, context, channel):
    s = context.scene.vcb
    obj = context.object
    active = bool(obj) and obj.mode == 'VERTEX_PAINT' and s.active_channel == channel

    row = layout.row(align=True)
    row.scale_y = 1.5
    sw = row.row(align=True)
    sw.scale_x = 0.35
    sw.enabled = False
    sw.prop(s, "col_" + channel, text="")
    op = row.operator("paint.vcb_paint", text=CHANNEL_LABEL[channel], depress=active)
    op.channel = channel


def draw_advanced(layout, context):
    s = context.scene.vcb
    obj = context.object
    mat = obj.active_material if obj else None

    col = layout.column(align=True)
    col.prop(s, "attr_name")
    col.prop(s, "scope")
    col.prop(s, "base_mode")

    row = layout.row(align=True)
    row.operator("material.vcb_convert", icon='NODE_MATERIAL')
    row.operator("material.vcb_revert", text="", icon='LOOP_BACK')

    if not (mat and is_converted(mat)):
        return

    layout.separator()
    layout.label(text="Textures", icon='TEXTURE')
    nt = mat.node_tree
    for ch in CHANNELS:
        box = layout.box()
        head = box.row(align=True)
        sw = head.row(align=True)
        sw.scale_x = 0.35
        sw.enabled = False
        sw.prop(s, "col_" + ch, text="")
        head.label(text=CHANNEL_SLOT[ch])
        node = slot_node(mat, ch)
        if node is None:
            box.label(text="-")
        elif node.type == 'TEX_IMAGE':
            box.template_ID(node, "image", open="image.open", new="image.new")
        elif node.type == 'RGB':
            box.prop(node.outputs[0], "default_value", text="")
        else:
            box.label(text=node.name)


def draw_panel(layout, context):
    s = context.scene.vcb
    obj = context.object
    mat = obj.active_material if obj else None

    if mat is None:
        layout.label(text="No active material", icon='INFO')
        return

    layout.label(text=mat.name, icon='MATERIAL')

    if not is_converted(mat):
        layout.label(text="Not a vertex-blend material", icon='DOT')
        layout.operator("material.vcb_convert", icon='NODE_MATERIAL')
    else:
        col = layout.column(align=True)
        for ch in CHANNELS:
            draw_channel_row(col, context, ch)

        verts, faces = selection_counts(obj)
        if verts or faces:
            box = layout.box()
            box.label(text="Selected: %d verts / %d faces" % (verts, faces), icon='RESTRICT_SELECT_OFF')
            box.label(text="Fill selection with:")
            grid = box.grid_flow(row_major=True, columns=2, align=True)
            for ch in CHANNELS:
                op = grid.operator("paint.vcb_fill_selection", text=CHANNEL_LABEL[ch])
                op.channel = ch

        if obj.mode == 'VERTEX_PAINT':
            box = layout.box()
            box.label(text="Brush", icon='BRUSH_DATA')
            draw_brush_settings(box, context)
            mrow = box.row(align=True)
            mrow.prop(obj.data, "use_paint_mask", text="Face mask", toggle=True)
            mrow.prop(obj.data, "use_paint_mask_vertex", text="Vertex mask", toggle=True)
            box.operator("paint.vcb_end", icon='CHECKMARK')

        row = layout.row(align=True)
        row.operator("paint.vcb_reset_tool", icon='FILE_REFRESH')
        row.operator("paint.vcb_fill_black", text="", icon='X')

    layout.separator()
    header = layout.row(align=True)
    header.prop(s, "show_advanced",
                icon='DISCLOSURE_TRI_DOWN' if s.show_advanced else 'DISCLOSURE_TRI_RIGHT',
                emboss=False)
    if s.show_advanced:
        draw_advanced(layout.box(), context)


class VCB_PT_view3d(bpy.types.Panel):
    bl_label = "Vertex Color Blend"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_order = 5
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        draw_panel(self.layout, context)


class VCB_PT_shader(bpy.types.Panel):
    bl_label = "Vertex Color Blend"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "MadOut2"

    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == 'ShaderNodeTree'

    def draw(self, context):
        draw_panel(self.layout, context)


class VCB_PT_material(bpy.types.Panel):
    bl_label = "Vertex Color Blend"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "material"

    def draw(self, context):
        draw_panel(self.layout, context)


# ---------------------------------------------------------------- register

classes = (
    VCBSettings,
    VCB_OT_convert,
    VCB_OT_revert,
    VCB_OT_paint,
    VCB_OT_fill_selection,
    VCB_OT_reset_tool,
    VCB_OT_fill_black,
    VCB_OT_end_paint,
    VCB_PT_view3d,
    VCB_PT_shader,
    VCB_PT_material,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.vcb = bpy.props.PointerProperty(type=VCBSettings)


def unregister():
    try:
        del bpy.types.Scene.vcb
    except Exception:
        pass
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


def _purge_previous():
    names = {c.__name__ for c in classes} | {"VCB_OT_prepare_paint"}
    for base in (bpy.types.Panel, bpy.types.Operator, bpy.types.PropertyGroup):
        for c in list(base.__subclasses__()):
            if c.__name__ in names and getattr(c, "is_registered", False):
                try:
                    bpy.utils.unregister_class(c)
                except Exception:
                    pass
    try:
        del bpy.types.Scene.vcb
    except Exception:
        pass


if __name__ == "__main__":
    _purge_previous()
    register()
    print("VC Blend Converter 2.0.1 registered")
