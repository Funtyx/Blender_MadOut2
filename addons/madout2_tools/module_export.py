# =====================================================
# Module Export - Экспорт модульных объектов в FBX (+ Unity Prefab Variant)
# Для Blender 4.0+ / 5.0
# =====================================================
#
# Назначение:
#   Аддон для пакетного экспорта Empty-модулей в FBX с автоматической
#   подготовкой геометрии под Unity. Опционально создаёт Unity Prefab Variant
#   с привязанным FBX-источником, переносит world transform Empty в Unity
#   coordinate space и подключает MadOut2 рантайм-скрипты (Distance,
#   AsyncMesh, AsyncCollider).
#
# === Pipeline экспорта (то же что показано в UI-панели) ===
#   1. Создаёт глубокую копию иерархии каждого выбранного Empty
#      (оригиналы в сцене не трогаются).
#   2. Применяет все модификаторы на копиях.
#   3. Конвертирует Instances/Linked-данные в реальную геометрию.
#   4. Приводит каждый меш в локальное пространство Empty
#      (Empty → 0,0,0; в mesh data остаются только LOCAL-трансформы детей).
#   5. Унифицирует UV-каналы: первый слой → "UVMap", остальные удаляются.
#   6. Чистит дубли материалов: 'Material.001/.002' → 'Material' (оригинал).
#   7. Объединяет все меши модуля в один (Join).
#   8. Исправляет нормали (flip) для мешей с отрицательным scale.
#   9. Сливает вершины по расстоянию 0.0001м (Merge by Distance,
#      use_unselected=False, sharp_edges=True).
#  10. Удаляет Loose geometry (висячие вершины и рёбра).
#  11. Экспортирует в FBX (axis -Z/+Y, FBX_SCALE_UNITS, embed textures).
#
# === Pivot Edit (отдельная фича панели) ===
#   - ВКЛ: дети Empty временно отвязываются (parent_clear KEEP_TRANSFORM)
#     — Empty можно двигать без визуального сдвига детей.
#   - ВЫКЛ: дети возвращаются к Empty (parent_set keep_transform=True)
#     — Empty становится новым pivot, дети остаются на месте.
#   - State хранится в кастом-проперти Empty: '_pivot_edit_active',
#     '_pivot_edit_children'. Empty в режиме Edit Pivot пропускается
#     при экспорте (без детей экспортировать нечего).
#
# === Unity Prefab Variant (опционально, чекбокс в UI) ===
#   - Вся иерархия строится внутри указанной папки:
#       <папка>/Meshes, <папка>/Prefabs, <папка>/Textures, <папка>/Materials
#     (если указана сама папка 'Meshes' — корнем считается её родитель).
#   - На FBX пишется .meta с фиксированными настройками ModelImporter
#     (addColliders: 1 → Unity сам добавит MeshCollider, и Variant
#     наследует его). При re-export GUID существующего .meta сохраняется,
#     чтобы не сломать ссылки в сценах.
#   - .prefab генерируется как Prefab Variant поверх FBX-модели:
#       * stripped GameObject + PrefabInstance с overrides на
#         m_LocalPosition/Rotation/Scale + m_Name
#       * добавлены MonoBehaviour: Distance, AsyncMesh, AsyncCollider
#         (GUID скриптов жёстко прошиты).
#   - World transform Empty конвертируется Blender → Unity:
#       position : (-bx,  bz, -by) * unit_scale
#       rotation : R_unity = C · R_blender · Cᵀ  (C = Y/Z swap + 180° Y)
#       scale    : (sx,  sz,  sy)
#     Учитывается scene.unit_settings.scale_length.
#   - Чекбокс «Перезаписать существующий» защищает от случайного
#     затирания .prefab с настроенными в сценах overrides.
#
# === Безопасность и идемпотентность ===
#   - Исходная сцена не модифицируется (всё делается на duplicate-копиях,
#     которые удаляются после экспорта вместе с orphan mesh data).
#   - Конфликты имён разруливаются: одноимённый Object/Mesh временно
#     переименовывается в '__bak_*', после экспорта возвращается обратно
#     — без этого Blender вешает '.001' суффикс на финальный объект.
#   - Empty не в активном View Layer пропускаются с warning'ом.
#
# === Что происходит в Unity при импорте экспортированных файлов ===
#   1. ModelImporter парсит .fbx по настройкам из .fbx.meta:
#        useFileUnits=1, useFileScale=1, globalScale=1, bakeAxisConversion=0
#        — итоговый размер 1:1 с Blender (метры).
#   2. Создаётся внутренний model-GameObject (fileID 100000) с
#        Transform (400000), MeshFilter, MeshRenderer (2300000).
#   3. addColliders:1 → Unity автоматически добавляет MeshCollider
#        на корень модели (он же будет унаследован Variant'ом).
#   4. Материалы экстрактятся как external (materialImportMode:2,
#        materialSearch:1, materialLocation:1) — Variant ссылается
#        на материалы проекта, не вшитые в FBX.
#   5. weldVertices:1, isReadable:0, generateSecondaryUV:0,
#        meshCompression:0 — рантайм-mesh оптимальный, без lightmap-UV.
#   6. На re-export GUID FBX'а сохраняется (читается из существующего
#        .meta) → ссылки в сценах и других префабах не ломаются;
#        addColliders патчится в 1 если был другой.
#   7. .prefab грузится как Prefab Variant: m_SourcePrefab указывает
#        на FBX-модель → Variant автоматически наследует Transform +
#        MeshFilter + MeshRenderer + MeshCollider от FBX.
#   8. PrefabInstance.m_Modifications переопределяют Transform корня
#        значениями world transform Blender-Empty (после Blender→Unity
#        конверсии), плюс m_Name = имени Empty.
#   9. m_AddedComponents подключают 3 MonoBehaviour к корню Variant'а:
#        - Distance       (LOD/дистанционный менеджер; lod_0 указывает
#                          на stripped MeshRenderer Variant'а; distances[6]
#                          и LocalBounds считаются в Blender тем же
#                          алгоритмом, что кнопка Calculate);
#        - AsyncMesh      (async-загрузка mesh; mf → stripped MeshFilter);
#        - AsyncCollider  (async-загрузка collider'а в рантайме).
#       mesh_name/guid/res_name оставлены пустыми — заполняются
#       пайплайном проекта.
#  10. .prefab.meta пишется с фиксированным GUID (или сохранённым,
#        если файл уже существует) — все scene-ссылки на префаб
#        остаются валидными при повторных экспортах.
#
# === Требования / зависимости ===
#   - Blender 4.0+ (тестировалось на 5.0).
#   - Целевой проект: MadOut2/Benza Unity (GUID скриптов в константах
#     SCRIPT_GUID_DISTANCE / _ASYNC_MESH / _ASYNC_COLLIDER).
#

bl_info = {
    "name": "Module Export",
    "author": "BWS",
    "version": (1, 5, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Module Export",
    "description": "Экспорт Empty-модулей в FBX + опциональный Unity Prefab Variant с переносом world transform и MadOut2-скриптами",
    "category": "Import-Export",
}

import bpy
import os
import re
import uuid
import random
from bpy.props import StringProperty, BoolProperty, EnumProperty
from bpy.types import Panel, Operator, AddonPreferences
from mathutils import Matrix, Quaternion


# Blender (Z-up, right-handed) → Unity (Y-up, left-handed) conversion.
# Derived empirically: Unity (ux, uy, uz) = (-bx, bz, -by).
# Equivalent to Y/Z axis swap + 180° around Y (standard FBX handedness fix).
_BL_TO_UNITY_3X3 = Matrix((
    (-1, 0, 0),
    (0, 0, 1),
    (0, -1, 0),
))


# === MadOut2 script GUIDs (Unity meta) ===
SCRIPT_GUID_DISTANCE = "0e5ba98a496e7f54c962eeb9bed5fdfc"
SCRIPT_GUID_ASYNC_MESH = "3e33f3a509c64c85bd1e2fe3df8451ce"
SCRIPT_GUID_ASYNC_COLLIDER = "3f3a67c22e6d439b8279b0c33cf8a3fa"


# ============================================================
# Unity prefab generation helpers
# ============================================================

def _gen_unity_guid():
    return uuid.uuid4().hex


def _gen_file_id_base():
    # 64-bit positive int, leave headroom for +N component offsets
    return random.randint(1 << 50, (1 << 62) - 16)


def _bl_pos_to_unity(loc, unit_scale=1.0):
    """Blender position -> Unity position.
    Unity = (-bx, bz, -by) * unit_scale.
    (Axis Y↔Z swap + 180° around Y for handedness, standard FBX convention.)"""
    return (-loc.x * unit_scale, loc.z * unit_scale, -loc.y * unit_scale)


def _bl_quat_to_unity(q):
    """Blender quaternion -> Unity quaternion (x, y, z, w).
    Converts via rotation matrix: R_unity = C @ R_blender @ C^T
    where C is the axis conversion matrix."""
    r_bl = q.to_matrix()  # 3x3 from Blender quat
    r_u = _BL_TO_UNITY_3X3 @ r_bl @ _BL_TO_UNITY_3X3.transposed()
    q_u = r_u.to_quaternion()
    return (q_u.x, q_u.y, q_u.z, q_u.w)


def _bl_scale_to_unity(s):
    """Blender scale (sx, sy, sz) → Unity scale (sx, sz, sy) (axis swap)."""
    return (s.x, s.z, s.y)


def _resolve_export_root(export_path):
    """Корень иерархии = указанная папка. Если указали саму '.../Meshes' —
    корень её родитель, чтобы не получилось Meshes/Meshes."""
    abs_path = os.path.normpath(bpy.path.abspath(export_path))
    if os.path.basename(abs_path) == "Meshes":
        return os.path.dirname(abs_path)
    return abs_path


def _resolve_sibling_dir(export_path, folder_name):
    """Подпапка иерархии внутри корня: Meshes / Prefabs / Textures / Materials."""
    return os.path.join(_resolve_export_root(export_path), folder_name)


def _resolve_prefab_dir(export_path):
    return _resolve_sibling_dir(export_path, "Prefabs")


def _find_existing_asset(fname, own_dir):
    """Готовый файл (с .meta) по имени: сначала своя папка (Materials/Textures),
    потом такая же папка соседних кварталов — куски, скопированные из другого
    дома, приносят его материалы и текстуры. Возвращает путь или None."""
    own = os.path.join(own_dir, fname)
    if os.path.exists(own):
        return own
    # У соседей ищем только «именные» M_/T_<Дом>_NN: общие имена вроде
    # Unknown-2.png в разных кварталах — это разные картинки
    if not fname.startswith(("M_", "T_")):
        return None
    folder = os.path.basename(os.path.normpath(own_dir))
    neighbours = os.path.dirname(os.path.dirname(os.path.normpath(own_dir)))
    try:
        siblings = sorted(os.listdir(neighbours))
    except OSError:
        return None
    for d in siblings:
        path = os.path.join(neighbours, d, folder, fname)
        if os.path.exists(path) and os.path.exists(path + ".meta"):
            return path
    return None


def _find_existing_mat(mat_name, mat_dir):
    return _find_existing_asset(_safe_filename(mat_name) + ".mat", mat_dir)


# ============================================================
# Режимы экспорта — готовые наборы галочек
# ============================================================

_EXPORT_MODES = (
    ('UPDATE', "Обновить модели",
     "FBX заново. Новые материалы и текстуры создаются, готовые не трогаются. "
     "Префаб — только для новых домов. Сцена не трогается. Дублей не будет", 'FILE_REFRESH', 0),
    ('GEOMETRY', "Только геометрия",
     "Только FBX. Материалы привязываются к готовым .mat (свои и соседних кварталов). "
     "Ничего нового не создаётся", 'MESH_DATA', 1),
    ('FULL', "Полный экспорт",
     "Всё с нуля: FBX, PNG, .mat, префабы и сцена перезаписываются. "
     "Правки этих файлов в Unity пропадут", 'PACKAGE', 2),
    ('CUSTOM', "Вручную", "Свои галочки", 'PREFERENCES', 3),
)

# (подпись, ключ «создавать», ключ «перезаписывать») → строки сводки в панели
_MODE_STEPS = (
    ("FBX", None, None),
    ("Материалы / текстуры", "assets", "overwrite_assets"),
    ("Префабы", "prefab", "overwrite_prefab"),
    ("Сцена Unity", "scene", "overwrite_scene"),
)


def _mode_settings(scene):
    """Что делает экспорт в выбранном режиме."""
    mode = scene.module_export_mode
    if mode == 'FULL':
        return dict(assets=True, overwrite_assets=True, prefab=True, overwrite_prefab=True,
                    scene=True, overwrite_scene=True)
    if mode == 'UPDATE':
        return dict(assets=True, overwrite_assets=False, prefab=True, overwrite_prefab=False,
                    scene=False, overwrite_scene=False)
    if mode == 'GEOMETRY':
        return dict(assets=False, overwrite_assets=False, prefab=False, overwrite_prefab=False,
                    scene=False, overwrite_scene=False)
    prefab = scene.module_export_create_prefab
    return dict(assets=scene.module_export_unpack_textures,
                overwrite_assets=scene.module_export_overwrite_prefab,
                prefab=prefab, overwrite_prefab=scene.module_export_overwrite_prefab,
                scene=prefab and scene.module_export_create_scene,
                overwrite_scene=scene.module_export_overwrite_scene)


def _safe_filename(name):
    """Имя файла без символов, на которых спотыкается файловая система/Unity."""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', name).strip().strip('.')
    return cleaned or "texture"


def _collect_images(obj):
    """Все картинки, на которые ссылаются материалы объекта, включая группы нод."""
    images = []
    seen = set()
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or not mat.use_nodes or mat.node_tree is None:
            continue
        stack = [mat.node_tree]
        visited = set()
        while stack:
            tree = stack.pop()
            if tree is None or id(tree) in visited:
                continue
            visited.add(id(tree))
            for node in tree.nodes:
                if node.bl_idname == 'ShaderNodeTexImage' and node.image is not None:
                    img = node.image
                    if id(img) not in seen:
                        seen.add(id(img))
                        images.append(img)
                elif node.bl_idname == 'ShaderNodeGroup':
                    stack.append(node.node_tree)
    return images


def _png_base_name(img):
    """Имя PNG без расширения, под которым картинка уходит в Unity."""
    if img.name.startswith("T_"):
        # имя уже приведено «Фиксом материалов» (T_<Дом>_NN) — оно и идёт в Unity
        return _safe_filename(_clean_name(img.name))
    base = bpy.path.basename(img.filepath_raw) if img.filepath_raw else ""
    return _safe_filename(os.path.splitext(base)[0] or os.path.splitext(img.name)[0] or img.name)


def _png_target_path(img, out_dir, used_paths=None, key=None):
    """Путь PNG в out_dir. used_paths {путь: ключ картинки} — разные картинки
    с одинаковым именем файла не затирают друг друга (суффикс _1, _2...)."""
    base = _png_base_name(img)
    out_path = os.path.join(out_dir, base + ".png")
    n = 1
    while used_paths and used_paths.get(out_path, key) != key:
        out_path = os.path.join(out_dir, f"{base}_{n}.png")
        n += 1
    return out_path


def _save_image_as_png(img, out_path):
    """Кладёт картинку в out_path ВСЕГДА в PNG — хоть она запакована в .blend,
    хоть лежит на диске в jpg/tga/psd. Возвращает путь или None."""
    if img.source == 'TILED':
        return None  # UDIM — это набор файлов, отдельная история

    old_format = img.file_format
    old_raw = img.filepath_raw
    try:
        if not img.has_data and img.source == 'FILE' and img.packed_file is None:
            try:
                img.reload()
            except Exception:
                pass
        img.file_format = 'PNG'
        img.save(filepath=out_path)
        return out_path
    except Exception:
        try:
            img.save_render(out_path)
            return out_path
        except Exception:
            return None
    finally:
        # datablock возвращаем как был — иначе сцена юзера уедет на PNG-пути
        try:
            img.file_format = old_format
            img.filepath_raw = old_raw
        except Exception:
            pass


# ============================================================
# Unity materials (Built-in Standard) + texture .meta
# ============================================================

def _lin_to_srgb(c):
    """Цвета в Blender линейные, а .mat Built-in хранит их в sRGB."""
    c = max(0.0, min(1.0, float(c)))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def _upstream_image(socket, depth=0, visited=None):
    """Ищет картинку выше по цепочке нод от сокета (через Mix/Hue/Gamma и т.п.).
    Возвращает (image, имя_выхода) или (None, None)."""
    if socket is None or not socket.is_linked or depth > 24:
        return None, None
    if visited is None:
        visited = set()
    for link in socket.links:
        node = link.from_node
        if node.bl_idname == 'ShaderNodeTexImage' and node.image is not None:
            return node.image, link.from_socket.name
        if id(node) in visited:
            continue
        visited.add(id(node))
        if node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None:
            for n in node.node_tree.nodes:
                if n.bl_idname == 'ShaderNodeTexImage' and n.image is not None:
                    return n.image, 'Color'
        for inp in node.inputs:
            img, out = _upstream_image(inp, depth + 1, visited)
            if img is not None:
                return img, out
    return None, None


def _material_info(mat):
    """Достаёт из нод то, что понимает Unity Standard: albedo/normal, цвет, metallic,
    smoothness, cutout. Ноды Principled ищутся от Material Output."""
    info = {
        "albedo": None, "normal": None, "color": (1.0, 1.0, 1.0, 1.0),
        "metallic": 0.0, "smoothness": 0.5, "cutout": False,
    }
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        if mat is not None:
            info["color"] = tuple(mat.diffuse_color)
        return info

    nodes = mat.node_tree.nodes
    bsdf = None
    out = next((n for n in nodes if n.bl_idname == 'ShaderNodeOutputMaterial' and n.is_active_output), None)
    if out is not None and out.inputs['Surface'].is_linked:
        cand = out.inputs['Surface'].links[0].from_node
        if cand.bl_idname == 'ShaderNodeBsdfPrincipled':
            bsdf = cand
    if bsdf is None:
        bsdf = next((n for n in nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)

    if bsdf is None:
        # Не Principled (Diffuse/Emission/кастом) — берём первую попавшуюся картинку
        if out is not None:
            info["albedo"], _ = _upstream_image(out.inputs['Surface'])
        if info["albedo"] is None:
            img = next((n.image for n in nodes
                        if n.bl_idname == 'ShaderNodeTexImage' and n.image is not None), None)
            info["albedo"] = img
        return info

    base = bsdf.inputs.get('Base Color')
    if base is not None:
        info["albedo"], _ = _upstream_image(base)
        if info["albedo"] is None:
            info["color"] = tuple(base.default_value)

    alpha = bsdf.inputs.get('Alpha')
    if alpha is not None:
        if alpha.is_linked:
            info["cutout"] = True
        elif alpha.default_value < 0.999:
            c = info["color"]
            info["color"] = (c[0], c[1], c[2], float(alpha.default_value))

    met = bsdf.inputs.get('Metallic')
    if met is not None and not met.is_linked:
        info["metallic"] = float(met.default_value)
    rough = bsdf.inputs.get('Roughness')
    if rough is not None and not rough.is_linked:
        info["smoothness"] = 1.0 - float(rough.default_value)

    nrm = bsdf.inputs.get('Normal')
    if nrm is not None and nrm.is_linked:
        nmap = nrm.links[0].from_node
        if nmap.bl_idname == 'ShaderNodeNormalMap':
            info["normal"], _ = _upstream_image(nmap.inputs.get('Color'))
    return info


def _write_texture_meta(png_path, is_normal):
    """.png.meta с GUID. Существующий не трогаем (там настройки юзера из Unity),
    только переключаем textureType на Normal map, если это нормалка."""
    meta_path = png_path + ".meta"
    existing = _read_existing_meta_guid(meta_path)
    if existing:
        if is_normal:
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                new_content = re.sub(r'(\n\s*textureType:\s*)\d+', r'\g<1>1', content, count=1)
                if new_content != content:
                    with open(meta_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
            except Exception:
                pass
        return existing

    guid = _gen_unity_guid()
    content = (
        "fileFormatVersion: 2\n"
        f"guid: {guid}\n"
        "TextureImporter:\n"
        "  internalIDToNameTable: []\n"
        "  externalObjects: {}\n"
        "  serializedVersion: 13\n"
        "  mipmaps:\n"
        "    mipMapMode: 0\n"
        "    enableMipMap: 1\n"
        f"    sRGBTexture: {0 if is_normal else 1}\n"
        "    linearTexture: 0\n"
        "  bumpmap:\n"
        "    convertToNormalMap: 0\n"
        "  isReadable: 0\n"
        "  textureFormat: 1\n"
        "  maxTextureSize: 2048\n"
        "  textureSettings:\n"
        "    serializedVersion: 2\n"
        "    filterMode: 1\n"
        "    aniso: 1\n"
        "    mipBias: 0\n"
        "    wrapU: 0\n"
        "    wrapV: 0\n"
        "    wrapW: 0\n"
        "  nPOTScale: 1\n"
        "  lightmap: 0\n"
        "  compressionQuality: 50\n"
        "  alphaUsage: 1\n"
        "  alphaIsTransparency: 0\n"
        f"  textureType: {1 if is_normal else 0}\n"
        "  textureShape: 1\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )
    with open(meta_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return guid


def _tex_env(name, guid):
    ref = f"{{fileID: 2800000, guid: {guid}, type: 3}}" if guid else "{fileID: 0}"
    return (
        f"    - {name}:\n"
        f"        m_Texture: {ref}\n"
        "        m_Scale: {x: 1, y: 1}\n"
        "        m_Offset: {x: 0, y: 0}\n"
    )


def _build_unity_material_yaml(name, info, albedo_guid, normal_guid):
    """Built-in Standard (fileID 46) — тот же шейдер, что у остальных материалов проекта."""
    r, g, b, a = info["color"]
    color = (_lin_to_srgb(r), _lin_to_srgb(g), _lin_to_srgb(b), a)
    cutout = info["cutout"]
    keywords = []
    if cutout:
        keywords.append("_ALPHATEST_ON")
    if normal_guid:
        keywords.append("_NORMALMAP")
    valid_kw = ("\n" + "".join(f"  - {k}\n" for k in keywords)).rstrip("\n") if keywords else " []"
    tags = "\n    RenderType: TransparentCutout" if cutout else " {}"
    tex = "".join([
        _tex_env("_BumpMap", normal_guid),
        _tex_env("_DetailAlbedoMap", None),
        _tex_env("_DetailMask", None),
        _tex_env("_DetailNormalMap", None),
        _tex_env("_EmissionMap", None),
        _tex_env("_MainTex", albedo_guid),
        _tex_env("_MetallicGlossMap", None),
        _tex_env("_OcclusionMap", None),
        _tex_env("_ParallaxMap", None),
    ])
    return (
        "%YAML 1.1\n"
        "%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!21 &2100000\n"
        "Material:\n"
        "  serializedVersion: 8\n"
        "  m_ObjectHideFlags: 0\n"
        "  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        f"  m_Name: {name}\n"
        "  m_Shader: {fileID: 46, guid: 0000000000000000f000000000000000, type: 0}\n"
        "  m_Parent: {fileID: 0}\n"
        "  m_ModifiedSerializedProperties: 0\n"
        f"  m_ValidKeywords:{valid_kw}\n"
        "  m_InvalidKeywords: []\n"
        "  m_LightmapFlags: 4\n"
        "  m_EnableInstancingVariants: 0\n"
        "  m_DoubleSidedGI: 0\n"
        f"  m_CustomRenderQueue: {2450 if cutout else -1}\n"
        f"  stringTagMap:{tags}\n"
        "  disabledShaderPasses: []\n"
        "  m_LockedProperties: \n"
        "  m_SavedProperties:\n"
        "    serializedVersion: 3\n"
        "    m_TexEnvs:\n"
        f"{tex}"
        "    m_Ints: []\n"
        "    m_Floats:\n"
        "    - _BumpScale: 1\n"
        "    - _Cutoff: 0.5\n"
        "    - _DetailNormalMapScale: 1\n"
        "    - _DstBlend: 0\n"
        "    - _GlossMapScale: 1\n"
        f"    - _Glossiness: {info['smoothness']:.4f}\n"
        "    - _GlossyReflections: 1\n"
        f"    - _Metallic: {info['metallic']:.4f}\n"
        f"    - _Mode: {1 if cutout else 0}\n"
        "    - _OcclusionStrength: 1\n"
        "    - _Parallax: 0.02\n"
        "    - _SmoothnessTextureChannel: 0\n"
        "    - _SpecularHighlights: 1\n"
        "    - _SrcBlend: 1\n"
        "    - _UVSec: 0\n"
        "    - _ZWrite: 1\n"
        "    m_Colors:\n"
        f"    - _Color: {{r: {color[0]:.6f}, g: {color[1]:.6f}, b: {color[2]:.6f}, a: {color[3]:.6f}}}\n"
        "    - _EmissionColor: {r: 0, g: 0, b: 0, a: 0}\n"
        "  m_BuildTextureStacks: []\n"
        "  m_AllowLocking: 1\n"
    )


def _write_material_meta(mat_path):
    meta_path = mat_path + ".meta"
    existing = _read_existing_meta_guid(meta_path)
    if existing:
        return existing
    guid = _gen_unity_guid()
    with open(meta_path, 'w', encoding='utf-8') as f:
        f.write(
            "fileFormatVersion: 2\n"
            f"guid: {guid}\n"
            "NativeFormatImporter:\n"
            "  externalObjects: {}\n"
            "  mainObjectFileID: 2100000\n"
            "  userData: \n"
            "  assetBundleName: \n"
            "  assetBundleVariant: \n"
        )
    return guid


def _yaml_scalar(s):
    """Имя материала как YAML-скаляр: в кавычки, если там спецсимволы."""
    if re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.\- ]*', s) and not s.endswith(' '):
        return s
    return "'" + s.replace("'", "''") + "'"


def _external_objects_entries(material_map):
    return "".join(
        "  - first:\n"
        "      type: UnityEngine:Material\n"
        "      assembly: UnityEngine.CoreModule\n"
        f"      name: {_yaml_scalar(name)}\n"
        f"    second: {{fileID: 2100000, guid: {guid}, type: 2}}\n"
        for name, guid in material_map.items()
    )


def _merge_external_objects(content, material_map):
    """Переписывает externalObjects в .fbx.meta: наши материалы → наши .mat,
    чужие записи (не материалы этого экспорта) сохраняются."""
    lines = content.split("\n")
    start = next((i for i, l in enumerate(lines) if l.startswith("  externalObjects:")), None)
    if start is None:
        return content
    end = start + 1
    while end < len(lines) and (lines[end].startswith("  - ") or lines[end].startswith("    ")):
        end += 1

    # Старые записи: блоки, начинающиеся с "  - "
    kept = []
    chunk = []
    for l in lines[start + 1:end] + ["  - "]:
        if l.startswith("  - ") and chunk:
            text = "\n".join(chunk)
            m = re.search(r'\n\s*name:\s*(.+)', text)
            name = m.group(1).strip() if m else None
            if name and len(name) >= 2 and name[0] == name[-1] == "'":
                name = name[1:-1].replace("''", "'")
            is_mat = "UnityEngine:Material" in text
            if not (is_mat and name in material_map):
                kept.append(text)
            chunk = []
        chunk.append(l)

    entries = "".join(k + "\n" for k in kept) + _external_objects_entries(material_map)
    block = ["  externalObjects:" + ("" if entries else " {}")]
    if entries:
        block.append(entries.rstrip("\n"))
    return "\n".join(lines[:start] + block + lines[end:])


def _deselect_all(context):
    """Снять выделение без оператора: bpy.ops.object.select_all падает с
    'context is incorrect', если Blender не в Object Mode."""
    for o in context.view_layer.objects:
        try:
            o.select_set(False)
        except RuntimeError:
            pass


def _ensure_object_mode(context):
    """Экспорт работает только в Object Mode — выходим из Edit/Paint/Sculpt.
    Возвращает False, если переключиться не удалось."""
    if context.mode == 'OBJECT':
        return True
    vl = context.view_layer
    if vl.objects.active is None:
        cand = next((o for o in context.selected_objects if o.name in vl.objects), None)
        if cand is not None:
            vl.objects.active = cand
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
    except RuntimeError:
        return False
    return context.mode == 'OBJECT'


def _plain_mesh_roots(context):
    """Выделенные меши, которые НЕ лежат внутри другого выделенного объекта —
    иначе один и тот же меш уехал бы и сам по себе, и в составе родителя."""
    selected = set(context.selected_objects)
    roots = []
    for obj in context.selected_objects:
        if obj.type != 'MESH':
            continue
        parent = obj.parent
        nested = False
        while parent is not None:
            if parent in selected:
                nested = True
                break
            parent = parent.parent
        if not nested:
            roots.append(obj)
    return roots


def _read_existing_meta_guid(meta_path):
    """If a .meta exists, return its existing guid (so we don't break Unity refs)."""
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith("guid:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def _patch_meta_for_variant(meta_path):
    """Patch existing .fbx.meta for Prefab Variant workflow:
    - addColliders: 1  (Unity auto-adds MeshCollider on import → variant inherits it)
    Returns True if file was modified."""
    if not os.path.exists(meta_path):
        return False
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            content = f.read()
        new_content = content
        # addColliders: <N> → addColliders: 1
        new_content, _ = re.subn(
            r'addColliders:\s*\d+',
            'addColliders: 1',
            new_content
        )
        if new_content != content:
            with open(meta_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            return True
    except Exception:
        pass
    return False


def _read_existing_prefab_mesh_fileid(prefab_path, expected_fbx_guid):
    """On re-export: read existing .prefab and extract MeshFilter's m_Mesh fileID
    if it points to our FBX (guid matches). Lets us preserve a previously-fixed
    mesh ref even if Unity ever switches back to hash mode."""
    if not os.path.exists(prefab_path):
        return None
    try:
        with open(prefab_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # Match: m_Mesh: {fileID: <id>, guid: <guid>, type: 3}
        pattern = r'm_Mesh:\s*\{fileID:\s*(-?\d+),\s*guid:\s*([a-f0-9]+),\s*type:\s*3\}'
        for m in re.finditer(pattern, content):
            file_id, guid = m.group(1), m.group(2)
            if guid == expected_fbx_guid and file_id != "0":
                return file_id
    except Exception:
        pass
    return None


def _write_fbx_meta(fbx_path, fallback_guid, material_map=None, patch_colliders=True):
    """Write Unity .fbx.meta. If exists: keep GUID + force addColliders: 1.
    material_map {имя материала в FBX: guid .mat} → externalObjects (remap),
    чтобы Unity сразу взял наши .mat вместо встроенных в FBX."""
    meta_path = fbx_path + ".meta"
    existing = _read_existing_meta_guid(meta_path)
    if existing:
        if patch_colliders:
            _patch_meta_for_variant(meta_path)
        if material_map:
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                new_content = _merge_external_objects(content, material_map)
                if new_content != content:
                    with open(meta_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
            except Exception:
                pass
        return existing

    if material_map:
        ext_objects = "  externalObjects:\n" + _external_objects_entries(material_map)
    else:
        ext_objects = "  externalObjects: {}\n"
    content = (
        "fileFormatVersion: 2\n"
        f"guid: {fallback_guid}\n"
        "ModelImporter:\n"
        "  serializedVersion: 22300\n"
        "  internalIDToNameTable: []\n"
        f"{ext_objects}"
        "  materials:\n"
        "    materialImportMode: 2\n"
        "    materialName: 0\n"
        "    materialSearch: 1\n"
        "    materialLocation: 1\n"
        "  animations:\n"
        "    legacyGenerateAnimations: 4\n"
        "    bakeSimulation: 0\n"
        "    resampleCurves: 1\n"
        "    optimizeGameObjects: 0\n"
        "    motionNodeName: \n"
        "    animationImportErrors: \n"
        "    animationImportWarnings: \n"
        "    animationRetargetingWarnings: \n"
        "    animationDoRetargetingWarnings: 0\n"
        "    importAnimatedCustomProperties: 0\n"
        "    importConstraints: 0\n"
        "    animationCompression: 1\n"
        "    animationRotationError: 0.5\n"
        "    animationPositionError: 0.5\n"
        "    animationScaleError: 0.5\n"
        "    animationWrapMode: 0\n"
        "    extraExposedTransformPaths: []\n"
        "    extraUserProperties: []\n"
        "    clipAnimations: []\n"
        "    isReadable: 0\n"
        "  meshes:\n"
        "    lODScreenPercentages: []\n"
        "    globalScale: 1\n"
        "    meshCompression: 0\n"
        "    addColliders: 1\n"
        "    useSRGBMaterialColor: 1\n"
        "    sortHierarchyByName: 1\n"
        "    importVisibility: 1\n"
        "    importBlendShapes: 1\n"
        "    importCameras: 1\n"
        "    importLights: 1\n"
        "    fileIdsGeneration: 0\n"
        "    swapUVChannels: 0\n"
        "    generateSecondaryUV: 0\n"
        "    useFileUnits: 1\n"
        "    keepQuads: 0\n"
        "    weldVertices: 1\n"
        "    bakeAxisConversion: 0\n"
        "    preserveHierarchy: 0\n"
        "    skinWeightsMode: 0\n"
        "    maxBonesPerVertex: 4\n"
        "    minBoneWeight: 0.001\n"
        "    optimizeBones: 1\n"
        "    meshOptimizationFlags: -1\n"
        "    indexFormat: 0\n"
        "    secondaryUVAngleDistortion: 8\n"
        "    secondaryUVAreaDistortion: 15.000001\n"
        "    secondaryUVHardAngle: 88\n"
        "    secondaryUVMarginMethod: 1\n"
        "    secondaryUVMinLightmapResolution: 40\n"
        "    secondaryUVMinObjectScale: 1\n"
        "    secondaryUVPackMargin: 4\n"
        "    useFileScale: 1\n"
        "  tangentSpace:\n"
        "    normalSmoothAngle: 60\n"
        "    normalImportMode: 0\n"
        "    tangentImportMode: 3\n"
        "    normalCalculationMode: 4\n"
        "    legacyComputeAllNormalsFromSmoothingGroupsWhenMeshHasBlendShapes: 0\n"
        "    blendShapeNormalImportMode: 1\n"
        "    normalSmoothingSource: 0\n"
        "  referencedClips: []\n"
        "  importAnimation: 1\n"
        "  humanDescription:\n"
        "    serializedVersion: 3\n"
        "    human: []\n"
        "    skeleton: []\n"
        "    armTwist: 0.5\n"
        "    foreArmTwist: 0.5\n"
        "    upperLegTwist: 0.5\n"
        "    legTwist: 0.5\n"
        "    armStretch: 0.05\n"
        "    legStretch: 0.05\n"
        "    feetSpacing: 0\n"
        "    globalScale: 1\n"
        "    rootMotionBoneName: \n"
        "    hasTranslationDoF: 0\n"
        "    hasExtraRoot: 0\n"
        "    skeletonHasParents: 1\n"
        "  lastHumanDescriptionAvatarSource: {instanceID: 0}\n"
        "  autoGenerateAvatarMappingIfUnspecified: 1\n"
        "  animationType: 2\n"
        "  humanoidOversampling: 1\n"
        "  avatarSetup: 0\n"
        "  addHumanoidExtraRootOnlyWhenUsingAvatar: 1\n"
        "  remapMaterialsIfMaterialImportModeIsNone: 0\n"
        "  additionalBone: 0\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )
    with open(meta_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return fallback_guid


def _write_prefab_meta(prefab_path, fallback_guid):
    """Write Unity .prefab.meta. If meta exists, keep its GUID."""
    meta_path = prefab_path + ".meta"
    existing = _read_existing_meta_guid(meta_path)
    if existing:
        return existing

    content = (
        "fileFormatVersion: 2\n"
        f"guid: {fallback_guid}\n"
        "PrefabImporter:\n"
        "  externalObjects: {}\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )
    with open(meta_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return fallback_guid


def _build_unity_prefab_yaml(name, world_matrix, fbx_guid, mesh_file_id=None, unit_scale=1.0,
                             distance_data=None, base_id=None):
    """Generate Unity Prefab Variant YAML based on FBX as source.
    The FBX itself acts as a model prefab — variant inherits Transform +
    MeshFilter + MeshRenderer + MeshCollider (requires addColliders: 1).
    We add Distance, AsyncMesh, AsyncCollider and override Transform
    position/rotation/scale with Empty's Blender world transform.
    `mesh_file_id` arg unused (kept for backward signature compat).
    `unit_scale` — Blender scene.unit_settings.scale_length."""
    loc, rot, scale = world_matrix.decompose()
    upos = _bl_pos_to_unity(loc, unit_scale)
    uquat = _bl_quat_to_unity(rot)
    uscale = _bl_scale_to_unity(scale)

    base = base_id if base_id is not None else _gen_file_id_base()
    distance_id = base + 2
    asyncmesh_id = base + 3
    asynccollider_id = base + 4
    prefab_inst_id = base + 5

    # Conventional Unity ModelImporter fileIDs for the FBX's main GameObject:
    FBX_GO = 100000
    FBX_TRANSFORM = 400000
    FBX_MESH_RENDERER = 2300000
    FBX_PREFAB_ANCHOR = 100100000
    FBX_MESH_FILTER = 3300000

    # Объекты FBX внутри Variant'а: id = (id_в_FBX XOR id_PrefabInstance) & mask —
    # на них ссылаются Distance.lod_0 и AsyncMesh.mf (как делает сам Unity)
    stripped_go_id = _variant_object_id(FBX_GO, prefab_inst_id)
    stripped_mr_id = _variant_object_id(FBX_MESH_RENDERER, prefab_inst_id)
    stripped_mf_id = _variant_object_id(FBX_MESH_FILTER, prefab_inst_id)

    if distance_data is None:
        distance_data = ([0.123] * 6, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    dists, b_center, b_extent = distance_data
    dist_yaml = "".join(f"  - {_fmt_float(d)}\n" for d in dists)

    return (
        "%YAML 1.1\n"
        "%TAG !u! tag:unity3d.com,2011:\n"
        f"--- !u!33 &{stripped_mf_id} stripped\n"
        "MeshFilter:\n"
        f"  m_CorrespondingSourceObject: {{fileID: {FBX_MESH_FILTER}, guid: {fbx_guid}, type: 3}}\n"
        f"  m_PrefabInstance: {{fileID: {prefab_inst_id}}}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        f"--- !u!23 &{stripped_mr_id} stripped\n"
        "MeshRenderer:\n"
        f"  m_CorrespondingSourceObject: {{fileID: {FBX_MESH_RENDERER}, guid: {fbx_guid}, type: 3}}\n"
        f"  m_PrefabInstance: {{fileID: {prefab_inst_id}}}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        f"--- !u!1 &{stripped_go_id} stripped\n"
        "GameObject:\n"
        f"  m_CorrespondingSourceObject: {{fileID: {FBX_GO}, guid: {fbx_guid}, type: 3}}\n"
        f"  m_PrefabInstance: {{fileID: {prefab_inst_id}}}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        f"--- !u!114 &{distance_id}\n"
        "MonoBehaviour:\n"
        "  m_ObjectHideFlags: 0\n"
        "  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        f"  m_GameObject: {{fileID: {stripped_go_id}}}\n"
        "  m_Enabled: 1\n"
        "  m_EditorHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {SCRIPT_GUID_DISTANCE}, type: 3}}\n"
        "  m_Name: \n"
        "  m_EditorClassIdentifier: \n"
        "  distances:\n"
        f"{dist_yaml}"
        "  lod_0:\n"
        f"  - {{fileID: {stripped_mr_id}}}\n"
        "  lod_1: []\n"
        "  LocalBounds:\n"
        f"    m_Center: {{x: {b_center[0]:.6f}, y: {b_center[1]:.6f}, z: {b_center[2]:.6f}}}\n"
        f"    m_Extent: {{x: {b_extent[0]:.6f}, y: {b_extent[1]:.6f}, z: {b_extent[2]:.6f}}}\n"
        "  imposterType: 0\n"
        "  DistanceTag: 0\n"
        "  SwitchTransitionDuration: 0\n"
        f"--- !u!114 &{asyncmesh_id}\n"
        "MonoBehaviour:\n"
        "  m_ObjectHideFlags: 0\n"
        "  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        f"  m_GameObject: {{fileID: {stripped_go_id}}}\n"
        "  m_Enabled: 1\n"
        "  m_EditorHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {SCRIPT_GUID_ASYNC_MESH}, type: 3}}\n"
        "  m_Name: \n"
        "  m_EditorClassIdentifier: \n"
        "  mesh_name: \n"
        "  guid: \n"
        "  res_name: \n"
        f"  mf: {{fileID: {stripped_mf_id}}}\n"
        f"--- !u!114 &{asynccollider_id}\n"
        "MonoBehaviour:\n"
        "  m_ObjectHideFlags: 0\n"
        "  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        f"  m_GameObject: {{fileID: {stripped_go_id}}}\n"
        "  m_Enabled: 1\n"
        "  m_EditorHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {SCRIPT_GUID_ASYNC_COLLIDER}, type: 3}}\n"
        "  m_Name: \n"
        "  m_EditorClassIdentifier: \n"
        "  mc: {fileID: 0}\n"
        "  mesh_name: \n"
        "  guid: \n"
        "  res_name: \n"
        "  meshRadius: 0\n"
        f"--- !u!1001 &{prefab_inst_id}\n"
        "PrefabInstance:\n"
        "  m_ObjectHideFlags: 0\n"
        "  serializedVersion: 2\n"
        "  m_Modification:\n"
        "    serializedVersion: 3\n"
        "    m_TransformParent: {fileID: 0}\n"
        "    m_Modifications:\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalPosition.x\n"
        f"      value: {upos[0]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalPosition.y\n"
        f"      value: {upos[1]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalPosition.z\n"
        f"      value: {upos[2]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalRotation.x\n"
        f"      value: {uquat[0]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalRotation.y\n"
        f"      value: {uquat[1]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalRotation.z\n"
        f"      value: {uquat[2]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalRotation.w\n"
        f"      value: {uquat[3]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalScale.x\n"
        f"      value: {uscale[0]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalScale.y\n"
        f"      value: {uscale[1]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_TRANSFORM}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_LocalScale.z\n"
        f"      value: {uscale[2]:.6f}\n"
        "      objectReference: {fileID: 0}\n"
        f"    - target: {{fileID: {FBX_GO}, guid: {fbx_guid}, type: 3}}\n"
        "      propertyPath: m_Name\n"
        f"      value: {name}\n"
        "      objectReference: {fileID: 0}\n"
        "    m_RemovedComponents: []\n"
        "    m_RemovedGameObjects: []\n"
        "    m_AddedGameObjects: []\n"
        "    m_AddedComponents:\n"
        f"    - targetCorrespondingSourceObject: {{fileID: {FBX_GO}, guid: {fbx_guid}, type: 3}}\n"
        "      insertIndex: -1\n"
        f"      addedObject: {{fileID: {distance_id}}}\n"
        f"    - targetCorrespondingSourceObject: {{fileID: {FBX_GO}, guid: {fbx_guid}, type: 3}}\n"
        "      insertIndex: -1\n"
        f"      addedObject: {{fileID: {asyncmesh_id}}}\n"
        f"    - targetCorrespondingSourceObject: {{fileID: {FBX_GO}, guid: {fbx_guid}, type: 3}}\n"
        "      insertIndex: -1\n"
        f"      addedObject: {{fileID: {asynccollider_id}}}\n"
        f"  m_SourcePrefab: {{fileID: {FBX_PREFAB_ANCHOR}, guid: {fbx_guid}, type: 3}}\n"
    )


# ============================================================
# Фикс материалов: дубли ищутся по СОДЕРЖИМОМУ (пиксели текстур + параметры),
# а не по имени — niz_kvartalD_3.079 и niz_kvartalD_3_1 могут быть одним и тем же
# материалом, а niz_kvartalD_3.082 — совсем другим.
# ============================================================

_FP_CACHE = {}


def _image_fingerprint(img):
    """Хеш пикселей картинки (в 8 бит) + размер. Одинаковые картинки с разными
    именами/форматами (jpg и png, запакованная и с диска) дают один хеш."""
    if img is None:
        return None
    key = img.as_pointer()
    if key in _FP_CACHE:
        return _FP_CACHE[key]
    import hashlib
    import numpy as np
    fp = None
    try:
        w, h = img.size
        if (w == 0 or h == 0) and img.source == 'FILE':
            try:
                img.reload()
                w, h = img.size
            except Exception:
                pass
        if w and h:
            n = w * h * img.channels
            arr = np.empty(n, dtype=np.float32)
            img.pixels.foreach_get(arr)
            q = np.clip(np.rint(arr * 255.0), 0, 255).astype(np.uint8)
            if img.channels == 4 and q[3::4].min() == 255:
                q = q.reshape(-1, 4)[:, :3].ravel()   # непрозрачный RGBA == RGB
            fp = f"{w}x{h}:" + hashlib.md5(q.tobytes()).hexdigest()
    except Exception:
        fp = None
    if fp is None:
        # нет пикселей (битый путь) — хоть по пути к файлу
        fp = "path:" + (bpy.path.abspath(img.filepath) if img.filepath else img.name_full)
    _FP_CACHE[key] = fp
    return fp


def _material_images(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return []
    out, stack, seen = [], [mat.node_tree], set()
    while stack:
        tree = stack.pop()
        if tree is None or tree.as_pointer() in seen:
            continue
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.bl_idname == 'ShaderNodeTexImage' and node.image is not None:
                out.append(node.image)
            elif node.bl_idname == 'ShaderNodeGroup':
                stack.append(node.node_tree)
    return out


_BASE_NODE_PROPS = None


def _round_value(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (bool, int, str)) or v is None:
        return v
    try:
        return tuple(_round_value(x) for x in v)
    except TypeError:
        return str(v)


def _tree_fingerprint(tree, depth=0):
    """Отпечаток дерева нод без учёта имён/позиций: типы нод, их настройки,
    несвязанные значения входов, картинки (по пикселям) и все связи.
    Так эмиссия, хром, неон и т.п. не склеятся только из-за отсутствия текстур."""
    global _BASE_NODE_PROPS
    if tree is None or depth > 8:
        return None
    if _BASE_NODE_PROPS is None:
        _BASE_NODE_PROPS = {p.identifier for p in bpy.types.ShaderNode.bl_rna.properties}

    def node_key(node):
        props = []
        for prop in node.bl_rna.properties:
            pid = prop.identifier
            if pid in _BASE_NODE_PROPS or prop.type not in {'ENUM', 'BOOLEAN', 'INT', 'FLOAT', 'STRING'}:
                continue
            try:
                props.append((pid, _round_value(getattr(node, pid))))
            except Exception:
                pass
        inputs = []
        for sock in node.inputs:
            if sock.is_linked or not hasattr(sock, "default_value"):
                inputs.append((sock.identifier, "L" if sock.is_linked else None))
            else:
                inputs.append((sock.identifier, _round_value(sock.default_value)))
        extra = None
        if node.bl_idname == 'ShaderNodeTexImage':
            extra = _image_fingerprint(node.image)
        elif node.bl_idname == 'ShaderNodeGroup':
            extra = _tree_fingerprint(node.node_tree, depth + 1)
        return (node.bl_idname, tuple(props), tuple(inputs), extra)

    keys = {n.as_pointer(): node_key(n) for n in tree.nodes if n.bl_idname not in {'NodeFrame', 'NodeReroute'}}
    nodes = tuple(sorted(map(repr, keys.values())))
    links = []
    for l in tree.links:
        if l.is_muted:
            continue
        a, b = keys.get(l.from_node.as_pointer()), keys.get(l.to_node.as_pointer())
        links.append(repr((a, l.from_socket.identifier, b, l.to_socket.identifier)))
    return (nodes, tuple(sorted(links)))


def _material_signature(mat):
    """Материалы равны, если совпадают картинки (по пикселям), всё дерево нод
    и настройки отображения. Имена значения не имеют."""
    if mat is None:
        return None
    tree = _tree_fingerprint(mat.node_tree) if mat.use_nodes else None
    settings = []
    for attr in ("blend_method", "surface_render_method", "use_backface_culling",
                 "alpha_threshold", "diffuse_color", "metallic", "roughness"):
        if hasattr(mat, attr):
            settings.append((attr, _round_value(getattr(mat, attr))))
    return (bool(mat.use_nodes), tree, tuple(settings))


_SUFFIX_RE = re.compile(r'(\.\d{3})+$')


def _clean_name(name):
    """'niz_kvartalD_3.079' → 'niz_kvartalD_3', 'tex.png' → 'tex'."""
    name = _SUFFIX_RE.sub('', name)
    root, ext = os.path.splitext(name)
    if ext.lower() in {'.png', '.jpg', '.jpeg', '.tga', '.dds', '.bmp', '.tif', '.tiff', '.psd', '.exr'}:
        name = root
    return name


def _name_rank(name):
    """Чем меньше — тем лучше имя для «главного» экземпляра: без .NNN, короче."""
    return (bool(_SUFFIX_RE.search(name)), len(name), name)


def _image_display_name(img):
    if img.filepath:
        base = bpy.path.basename(img.filepath)
        if base:
            return _clean_name(base)
    return _clean_name(img.name)


def _house_base(name):
    """'SM_Building_RS_History_01' → 'Building_RS_History_01'."""
    base = _SUFFIX_RE.sub('', name)
    if base.startswith("SM_"):
        base = base[3:]
    return _safe_filename(base).replace(" ", "_")


def _scope_houses(context):
    """[(дом, [меши дома])]. Дом = выделенный Empty (как при экспорте) или
    выделенный меш без выделенного родителя. Ничего не выделено — дом это
    ближайший предок-Empty с префиксом SM_, иначе самый верхний Empty."""
    selected = list(context.selected_objects)
    houses = {}

    def meshes_under(root):
        out, stack = [], [root]
        while stack:
            o = stack.pop()
            if o.type == 'MESH':
                out.append(o)
            stack.extend(o.children)
        return out

    if selected:
        sel = set(selected)
        for r in selected:
            p = r.parent
            nested = False
            while p is not None:
                if p in sel:
                    nested = True
                    break
                p = p.parent
            if not nested:
                houses[r] = meshes_under(r)
    else:
        for o in context.scene.objects:
            if o.type != 'MESH':
                continue
            house, top, p = None, None, o.parent
            while p is not None:
                if p.type == 'EMPTY':
                    top = p
                    if house is None and p.name.startswith("SM_"):
                        house = p
                p = p.parent
            houses.setdefault(house or top or o, []).append(o)
    return sorted(houses.items(), key=lambda kv: (_house_base(kv[0].name), kv[0].name))


def _house_warnings(houses):
    """Проверка имён домов: префикс SM_, хвосты .001, одинаковые имена после очистки."""
    warns, seen = [], {}
    for h, _ in houses:
        if not h.name.startswith("SM_"):
            warns.append(f"Нет префикса SM_: {h.name}")
        if _SUFFIX_RE.search(h.name):
            warns.append(f"Дубль имени (.NNN): {h.name}")
        if re.search(r'[^A-Za-z0-9_\-]', _SUFFIX_RE.sub('', h.name)):
            warns.append(f"Пробелы/спецсимволы: {h.name}")
        base = _house_base(h.name)
        if base in seen:
            warns.append(f"Одинаковое имя дома: {seen[base]} и {h.name}")
        seen.setdefault(base, h.name)
    return warns


def _scope_materials(context):
    """Материалы выделенных объектов (вместе с детьми). Ничего не выделено —
    все меши сцены."""
    mats = []
    for _, meshes in _scope_houses(context):
        for o in meshes:
            for slot in o.material_slots:
                if slot.material is not None and slot.material not in mats:
                    mats.append(slot.material)
    return mats


def _build_plan(context, merge_untextured=False):
    """План фикса: группы дублей + какие дома используют какие материалы
    (в порядке домов по алфавиту и слотов) + проверка имён домов."""
    houses = _scope_houses(context)
    order = []          # [(mat, house_base)] в порядке появления
    for h, meshes in houses:
        hb = _house_base(h.name)
        for o in sorted(meshes, key=lambda x: x.name):
            for slot in o.material_slots:
                if slot.material is not None:
                    order.append((slot.material, hb))
    mats = []
    for m, _ in order:
        if m not in mats:
            mats.append(m)
    plan = _analyze_materials(mats, merge_untextured)
    plan["order"] = order
    plan["house_warnings"] = _house_warnings(houses)
    plan["house_count"] = len(houses)
    return plan


def _analyze_materials(mats, merge_untextured=False):
    """Группы одинаковых материалов и картинок. Возвращает dict с планом.
    Материалы без текстур по умолчанию не сливаются: одинаковые «белые
    заглушки» (M_DoorAtlas_01, Car_5_Chrome…) различаются смыслом имени."""
    groups = {}
    for m in mats:
        sig = _material_signature(m)
        if not merge_untextured and not _material_images(m):
            sig = ("untextured", m.as_pointer())
        groups.setdefault(sig, []).append(m)
    mat_groups = [sorted(g, key=lambda m: _name_rank(m.name)) for g in groups.values()]

    imgs = []
    for m in mats:
        for i in _material_images(m):
            if i not in imgs:
                imgs.append(i)
    by_fp = {}
    for i in imgs:
        by_fp.setdefault(_image_fingerprint(i), []).append(i)
    # Главная картинка: без .NNN, самая используемая — а не самая короткая по имени
    img_groups = [sorted(g, key=lambda i: (bool(_SUFFIX_RE.search(i.name)), -i.users, i.name))
                  for g in by_fp.values()]
    return {"mat_groups": mat_groups, "img_groups": img_groups,
            "mat_total": len(mats), "img_total": len(imgs)}


def _draw_house_warnings(layout, plan, limit=6):
    warns = plan.get("house_warnings") or []
    if not warns:
        return
    box = layout.box()
    box.label(text=f"Проверка имён домов: замечаний {len(warns)}", icon='ERROR')
    sub = box.column(align=True)
    sub.scale_y = 0.8
    for w in warns[:limit]:
        sub.label(text=w)
    if len(warns) > limit:
        sub.label(text=f"… и ещё {len(warns) - limit}")


def _dup_counts(plan):
    """(лишних материалов, лишних картинок) — сколько уйдёт при фиксе."""
    return (sum(len(g) - 1 for g in plan["mat_groups"]),
            sum(len(g) - 1 for g in plan["img_groups"]))


# План, посчитанный в invoke (окно предпросмотра) — переиспользуем в execute,
# чтобы не хешировать текстуры дважды
_PENDING_PLAN = {}


def _rename_unique(ids, items, wanted, collection):
    """Переименовывает items по wanted без конфликтов (через временные имена)."""
    for it in items:
        it.name = "__fix_" + uuid.uuid4().hex[:10]
    used = set()
    for it in sorted(items, key=lambda x: wanted[x]):
        base = wanted[it]
        name, n = base, 2
        while name in used or (collection.get(name) not in (None, it)):
            name = f"{base}_{n}"
            n += 1
        it.name = name
        used.add(name)


def _house_numbering(prefix, items_in_order, current_name):
    """Имена вида <prefix>_<Дом>_<NN>. Порядок items_in_order: [(item, дом)] —
    первый дом получает элемент («кто первый получил имя, у того и будет»).
    Уже правильно названные элементы сохраняют свой номер (стабильные имена
    → стабильные .mat/.png и GUID в Unity)."""
    first_house = {}
    for it, hb in items_in_order:
        first_house.setdefault(it, hb)
    taken = {}      # дом → занятые номера
    wanted = {}
    pending = []
    for it, hb in first_house.items():
        m = re.fullmatch(re.escape(f"{prefix}_{hb}_") + r'(\d{2,})', current_name(it))
        if m and int(m.group(1)) not in taken.setdefault(hb, set()):
            taken[hb].add(int(m.group(1)))
            wanted[it] = current_name(it)
            continue
        # Уже назван по ДРУГОМУ дому (кусок скопирован из соседнего дома) —
        # имя не трогаем: иначе в Unity появится копия его .mat/.png
        other = re.fullmatch(re.escape(f"{prefix}_") + r'(.+)_(\d{2,})', current_name(it))
        if other and other.group(1) != hb:
            taken.setdefault(other.group(1), set()).add(int(other.group(2)))
            wanted[it] = current_name(it)
        else:
            pending.append((it, hb))
    for it, hb in pending:
        nums = taken.setdefault(hb, set())
        n = 1
        while n in nums:
            n += 1
        nums.add(n)
        wanted[it] = f"{prefix}_{hb}_{n:02d}"
    return wanted


def _apply_material_fix(plan, merge_images=True, rename='HOUSE'):
    """Сливает дубли по готовому плану _build_plan и переименовывает.
    rename: 'HOUSE' — M_<Дом>_NN / T_<Дом>_NN, 'TEXTURE' — по имени текстуры,
    'NONE' — без переименования. Возвращает отчёт."""
    if rename is True:
        rename = 'TEXTURE'
    elif rename is False:
        rename = 'NONE'

    # 1. Одинаковые картинки → одна
    img_merged = 0
    img_canon = {}
    for g in plan["img_groups"]:
        for i in g:
            img_canon[i] = g[0]
    if merge_images:
        for g in plan["img_groups"]:
            for dup in g[1:]:
                dup.user_remap(g[0])
                img_merged += 1

    # 2. Одинаковые материалы → один (главный — с самым чистым именем)
    mat_merged = 0
    keep = []
    mat_canon = {}
    for g in plan["mat_groups"]:
        keep.append(g[0])
        for m in g:
            mat_canon[m] = g[0]
        for dup in g[1:]:
            dup.user_remap(g[0])
            mat_merged += 1

    renamed_m = renamed_t = 0
    if rename == 'HOUSE' and plan.get("order"):
        # материалы: в порядке домов, дубли уже указывают на главный
        mat_order = [(mat_canon.get(m, m), hb) for m, hb in plan["order"]]
        wanted = _house_numbering("M", mat_order, lambda m: m.name)
        _rename_unique(None, list(wanted), wanted, bpy.data.materials)
        renamed_m = len(wanted)

        # текстуры: дом материала, в котором картинка встретилась первой
        img_order = []
        for m, hb in mat_order:
            info = _material_info(m)
            seq = [info["albedo"], info["normal"]] + _material_images(m)
            for i in seq:
                if i is not None:
                    img_order.append((img_canon.get(i, i) if merge_images else i, hb))
        wanted_i = _house_numbering("T", img_order, lambda i: i.name)
        _rename_unique(None, list(wanted_i), wanted_i, bpy.data.images)
        renamed_t = len(wanted_i)

    elif rename == 'TEXTURE':
        wanted = {}
        for m in keep:
            info = _material_info(m)
            img = info["albedo"] or next(iter(_material_images(m)), None)
            # без текстур — оставляем своё имя, только без .NNN
            wanted[m] = _safe_filename(_image_display_name(img) if img else _clean_name(m.name))
        _rename_unique(None, keep, wanted, bpy.data.materials)
        renamed_m = len(keep)

    msg = (f"Материалов: {plan['mat_total']} → {len(keep)} (слито {mat_merged})"
           f" | Текстур слито: {img_merged}")
    if rename != 'NONE':
        msg += f" | Переименовано: M_ {renamed_m}" + (f", T_ {renamed_t}" if renamed_t else "")
    warns = plan.get("house_warnings") or []
    if warns:
        msg += f" | Замечаний по именам домов: {len(warns)}"
    return msg


# ============================================================
# Distance.distances — тот же расчёт, что кнопка Calculate в Unity
# (DistanceCalculate.cs): ортокамера 256x256 с 6 сторон, доля закрашенных
# пикселей * orthographicSize * 10. Здесь силуэт растеризуется в numpy,
# с отсечением задних граней как у Standard (Cull Back).
# ============================================================

_DIST_TEX = 256

# Side: (оси плоскости проекции, ось взгляда, знак нормали видимых граней)
# Порядок как в Distance.Side: Up, Down, Left, Right, Forward, Back
_DIST_SIDES = (
    ((0, 2), 1, +1),   # Up      — камера смотрит вниз, видит верх (n.y > 0)
    ((0, 2), 1, -1),   # Down
    ((1, 2), 0, -1),   # Left    — смотрит вдоль +X, видит грани n.x < 0
    ((1, 2), 0, +1),   # Right
    ((0, 1), 2, +1),   # Forward — смотрит вдоль -Z, видит n.z > 0
    ((0, 1), 2, -1),   # Back
)


def _raster_coverage(tri, n=_DIST_TEX):
    """tri: (T,3,2) в пикселях. Доля пикселей, чей центр внутри хоть одного
    треугольника (правило растеризации GPU)."""
    import numpy as np
    covered = np.zeros((n, n), dtype=bool)
    chunk = 400000
    for s0 in range(0, len(tri), chunk):
        t = tri[s0:s0 + chunk]
        ys = t[:, :, 1]
        r0 = np.clip(np.ceil(ys.min(1) - 0.5), 0, n).astype(np.int64)
        r1 = np.clip(np.floor(ys.max(1) - 0.5), -1, n - 1).astype(np.int64)
        cnt = r1 - r0 + 1
        keep = cnt > 0
        if not keep.any():
            continue
        t, r0, cnt = t[keep], r0[keep], cnt[keep]
        idx = np.repeat(np.arange(len(t)), cnt)
        start = np.repeat(np.cumsum(cnt) - cnt, cnt)
        rows = r0[idx] + (np.arange(len(idx)) - start)
        yc = rows + 0.5
        xl = np.full(len(idx), np.inf)
        xr = np.full(len(idx), -np.inf)
        for a, b in ((0, 1), (1, 2), (2, 0)):
            xa, ya = t[idx, a, 0], t[idx, a, 1]
            xb, yb = t[idx, b, 0], t[idx, b, 1]
            hit = ((ya <= yc) & (yc < yb)) | ((yb <= yc) & (yc < ya))
            dy = np.where(hit, yb - ya, 1.0)
            x = xa + (yc - ya) * (xb - xa) / dy
            xl = np.where(hit, np.minimum(xl, x), xl)
            xr = np.where(hit, np.maximum(xr, x), xr)
        c0 = np.clip(np.ceil(xl - 0.5), 0, n).astype(np.int64, copy=False) if len(xl) else xl
        c1 = np.clip(np.floor(xr - 0.5), -1, n - 1).astype(np.int64, copy=False) if len(xr) else xr
        ok = np.isfinite(xl) & np.isfinite(xr)
        c0 = np.where(ok, c0, 1)
        c1 = np.where(ok, c1, 0)
        ok &= c1 >= c0
        if not ok.any():
            continue
        diff = np.zeros((n, n + 1), dtype=np.int32)
        np.add.at(diff, (rows[ok], c0[ok]), 1)
        np.add.at(diff, (rows[ok], c1[ok] + 1), -1)
        covered |= np.cumsum(diff, axis=1)[:, :n] > 0
    return float(covered.mean())


def _filter_distance(raw):
    """DistanceCalculate.FilterResult."""
    if raw > 10:
        return float(round(raw))  # Mathf.Round — тоже банковское округление
    result = int(raw * 10.0) / 10.0
    if result < 0.1 and raw > 0.00001:
        result = 0.1
    return result


def _calc_distance_data(mesh, unit_scale=1.0):
    """По mesh (в локальном пространстве модуля) считает Distance.distances
    и LocalBounds в координатах Unity. Возвращает (distances[6], center, extent)."""
    import numpy as np
    mesh.calc_loop_triangles()
    nt = len(mesh.loop_triangles)
    nv = len(mesh.vertices)
    if nt == 0 or nv == 0:
        return [0.123] * 6, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)

    co = np.empty(nv * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) * unit_scale
    co = np.stack((-co[:, 0], co[:, 2], -co[:, 1]), axis=1)   # Blender → Unity

    vi = np.empty(nt * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", vi)
    vi = vi.reshape(-1, 3)
    nrm = np.empty(nt * 3, dtype=np.float64)
    mesh.loop_triangles.foreach_get("normal", nrm)
    nrm = nrm.reshape(-1, 3)
    nrm = np.stack((-nrm[:, 0], nrm[:, 2], -nrm[:, 1]), axis=1)

    used = co[np.unique(vi)]
    bmin, bmax = used.min(0), used.max(0)
    center = (bmin + bmax) * 0.5
    size = bmax - bmin
    tris = co[vi]  # (T,3,3)

    distances = []
    for (a, b), view_axis, sign in _DIST_SIDES:
        ortho = max(size[a], size[b]) / 2.0
        if ortho <= 1e-9:
            distances.append(0.0)
            continue
        vis = nrm[:, view_axis] * sign > 1e-12
        t2 = tris[vis][:, :, (a, b)]
        lo = np.array((center[a] - ortho, center[b] - ortho))
        px = (t2 - lo) / (2.0 * ortho) * _DIST_TEX
        alpha = _raster_coverage(px)
        if alpha < 0.05:
            alpha *= min(4.0, 1.0 + ortho * 0.5)
        distances.append(_filter_distance(alpha * ortho * 10.0))

    if sum(distances) < 0.01:  # CheckDistanceToAllZero
        distances = [0.123] * 6
    return distances, tuple(center), tuple(size * 0.5)


def _fmt_float(v):
    return f"{v:.6f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def _read_prefab_base_id(prefab_path):
    """Если префаб уже есть — берём его базовый fileID, чтобы при перезаписи
    ID объектов не менялись (иначе слетят ссылки/оверрайды в сценах)."""
    inst = _read_prefab_instance_id(prefab_path) if os.path.exists(prefab_path) else None
    return inst - 5 if inst is not None and inst > 5 else None


def _prefab_has_broken_lod_ref(prefab_path):
    """Префаб старой версии аддона — такой безопасно пересобрать:
    (а) lod_0 указывает на рендерер внутри ассета FBX, distances пустой;
    (б) то же после пересохранения в Unity: lod_0: [] и distances из нулей."""
    try:
        with open(prefab_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return False
    if SCRIPT_GUID_DISTANCE not in content:
        return False
    if re.search(r'lod_0:\n  - \{fileID: 2300000, guid: [0-9a-f]+,\s+type: 3\}', content):
        return True
    m = re.search(r'distances:(?: \[\]|((?:\n  - [-0-9.e]+)+))\n  lod_0: \[\]', content)
    if m:
        vals = re.findall(r'-?[0-9.e]+', m.group(1) or "")
        return all(float(v) == 0.0 for v in vals)
    return False


# ============================================================
# Unity scene (.unity) — инстансы префабов на мировых позициях из Blender
# ============================================================

# Conventional ModelImporter fileIDs (legacy fileIdsGeneration: 0), см. префаб
_FBX_GO_ID = 100000
_FBX_TRANSFORM_ID = 400000
_FILEID_MASK = 0x7FFFFFFFFFFFFFFF


def _read_prefab_instance_id(prefab_path):
    """fileID PrefabInstance внутри нашего Variant'а (--- !u!1001 &<id>)."""
    try:
        with open(prefab_path, 'r', encoding='utf-8') as f:
            m = re.search(r'^--- !u!1001 &(\d+)', f.read(), re.M)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _variant_object_id(source_file_id, prefab_instance_id):
    """fileID объекта FBX в том виде, как он лежит внутри Prefab Variant.
    Unity: (id_в_источнике XOR id_PrefabInstance) & 0x7FFFFFFFFFFFFFFF."""
    return (source_file_id ^ prefab_instance_id) & _FILEID_MASK


def _scene_mod(target_id, guid, prop, value):
    return (
        f"    - target: {{fileID: {target_id}, guid: {guid}, type: 3}}\n"
        f"      propertyPath: {prop}\n"
        f"      value: {value}\n"
        "      objectReference: {fileID: 0}\n"
    )


_TRANSFORM_PROPS = (
    "m_LocalPosition.x", "m_LocalPosition.y", "m_LocalPosition.z",
    "m_LocalRotation.x", "m_LocalRotation.y", "m_LocalRotation.z", "m_LocalRotation.w",
    "m_LocalScale.x", "m_LocalScale.y", "m_LocalScale.z",
)


def _scene_transform_values(world_matrix, unit_scale):
    """{propertyPath: значение} — world transform из Blender в осях Unity."""
    loc, rot, scale = world_matrix.decompose()
    vals = (_bl_pos_to_unity(loc, unit_scale) + _bl_quat_to_unity(rot)
            + _bl_scale_to_unity(scale))
    return {p: f"{v:.6f}" for p, v in zip(_TRANSFORM_PROPS, vals)}


def _scene_instance_block(scene_inst_id, name, prefab_guid, inst_in_prefab, world_matrix,
                          unit_scale):
    tr = _variant_object_id(_FBX_TRANSFORM_ID, inst_in_prefab)
    go = _variant_object_id(_FBX_GO_ID, inst_in_prefab)
    vals = _scene_transform_values(world_matrix, unit_scale)
    mods = "".join(_scene_mod(tr, prefab_guid, p, vals[p]) for p in _TRANSFORM_PROPS)
    mods += _scene_mod(go, prefab_guid, "m_Name", _yaml_scalar(name))
    return (
        f"--- !u!1001 &{scene_inst_id}\n"
        "PrefabInstance:\n"
        "  m_ObjectHideFlags: 0\n"
        "  serializedVersion: 2\n"
        "  m_Modification:\n"
        "    serializedVersion: 3\n"
        "    m_TransformParent: {fileID: 0}\n"
        "    m_Modifications:\n"
        f"{mods}"
        "    m_RemovedComponents: []\n"
        "    m_RemovedGameObjects: []\n"
        "    m_AddedGameObjects: []\n"
        "    m_AddedComponents: []\n"
        f"  m_SourcePrefab: {{fileID: 100100000, guid: {prefab_guid}, type: 3}}\n"
    )


def _build_unity_scene_yaml(entries, unit_scale=1.0):
    """entries: [(name, prefab_guid, prefab_instance_id, world_matrix)].
    Каждый модуль — инстанс своего .prefab, Transform = world transform из Blender
    (та же конверсия осей, что и в префабе → в сцене стоит один в один)."""
    out = [
        "%YAML 1.1\n",
        "%TAG !u! tag:unity3d.com,2011:\n",
        "--- !u!29 &1\n"
        "OcclusionCullingSettings:\n"
        "  m_ObjectHideFlags: 0\n"
        "  serializedVersion: 2\n"
        "  m_OcclusionBakeSettings:\n"
        "    smallestOccluder: 5\n"
        "    smallestHole: 0.25\n"
        "    backfaceThreshold: 100\n"
        "  m_SceneGUID: 00000000000000000000000000000000\n"
        "  m_OcclusionCullingData: {fileID: 0}\n",
        "--- !u!104 &2\n"
        "RenderSettings:\n"
        "  m_ObjectHideFlags: 0\n"
        "  serializedVersion: 10\n"
        "  m_Fog: 0\n"
        "  m_AmbientMode: 0\n"
        "  m_AmbientIntensity: 1\n"
        "  m_SkyboxMaterial: {fileID: 10304, guid: 0000000000000000f000000000000000, type: 0}\n"
        "  m_Sun: {fileID: 0}\n",
        "--- !u!157 &3\n"
        "LightmapSettings:\n"
        "  m_ObjectHideFlags: 0\n"
        "  serializedVersion: 13\n"
        "  m_LightingDataAsset: {fileID: 0}\n"
        "  m_LightingSettings: {fileID: 0}\n",
        "--- !u!196 &4\n"
        "NavMeshSettings:\n"
        "  serializedVersion: 2\n"
        "  m_ObjectHideFlags: 0\n"
        "  m_NavMeshData: {fileID: 0}\n",
    ]
    roots = []
    used_ids = set()
    for name, prefab_guid, inst_in_prefab, world_matrix in entries:
        scene_inst_id = _gen_file_id_base()
        while scene_inst_id in used_ids:
            scene_inst_id = _gen_file_id_base()
        used_ids.add(scene_inst_id)
        roots.append(scene_inst_id)
        out.append(_scene_instance_block(scene_inst_id, name, prefab_guid, inst_in_prefab,
                                         world_matrix, unit_scale))
    out.append(
        "--- !u!1660057539 &9223372036854775807\n"
        "SceneRoots:\n"
        "  m_ObjectHideFlags: 0\n"
        "  m_Roots:" + ("".join(f"\n  - {{fileID: {r}}}" for r in roots) if roots else " []") + "\n"
    )
    return "".join(out)


def _set_instance_transform(block, tr_id, prefab_guid, vals):
    """Подменяет в блоке PrefabInstance значения transform-оверрайдов;
    недостающие оверрайды дописывает в начало m_Modifications."""
    missing = []
    for prop, value in vals.items():
        pattern = (r'(- target: \{fileID: ' + str(tr_id) + r', guid: ' + prefab_guid
                   + r', type: 3\}\n\s+propertyPath: ' + re.escape(prop) + r'\n\s+value: )[^\n]*')
        block, n = re.subn(pattern, lambda m: m.group(1) + value, block)
        if n == 0:
            missing.append(prop)
    if missing:
        block = block.replace("    m_Modifications: []\n", "    m_Modifications:\n", 1)
        added = "".join(_scene_mod(tr_id, prefab_guid, p, vals[p]) for p in missing)
        block = block.replace("    m_Modifications:\n", "    m_Modifications:\n" + added, 1)
    return block


def _update_scene_yaml(text, entries, unit_scale=1.0):
    """Точечное обновление существующей .unity: у инстансов выделенных домов —
    новый transform, отсутствующие дома дописываются. Всё остальное в сцене
    (другие объекты, правки из Unity) остаётся как было.
    Возвращает (текст, обновлено, добавлено, пропущено[имена])."""
    parts = re.split(r'(?=^--- !u!)', text, flags=re.M)
    used_ids = {int(x) for x in re.findall(r'^--- !u!\d+ &(\d+)', text, re.M)}
    updated, skipped, added_ids = 0, [], []
    for name, prefab_guid, inst_in_prefab, world_matrix in entries:
        tr = _variant_object_id(_FBX_TRANSFORM_ID, inst_in_prefab)
        vals = _scene_transform_values(world_matrix, unit_scale)
        source = f"m_SourcePrefab: {{fileID: 100100000, guid: {prefab_guid}, type: 3}}"
        found = False
        for i, part in enumerate(parts):
            if not part.startswith("--- !u!1001 ") or source not in part:
                continue
            found = True
            # Дом вложен в другой объект в Unity — его local ≠ world из Blender
            if "m_TransformParent: {fileID: 0}" not in part:
                skipped.append(name)
                continue
            parts[i] = _set_instance_transform(part, tr, prefab_guid, vals)
            updated += 1
        if not found:
            scene_inst_id = _gen_file_id_base()
            while scene_inst_id in used_ids:
                scene_inst_id = _gen_file_id_base()
            used_ids.add(scene_inst_id)
            added_ids.append(scene_inst_id)
            block = _scene_instance_block(scene_inst_id, name, prefab_guid, inst_in_prefab,
                                          world_matrix, unit_scale)
            roots_at = next((i for i, p in enumerate(parts) if "\nSceneRoots:\n" in p), len(parts))
            parts.insert(roots_at, block)

    text = "".join(parts)
    if added_ids:
        new_roots = "".join(f"\n  - {{fileID: {r}}}" for r in added_ids)
        if "  m_Roots: []" in text:
            text = text.replace("  m_Roots: []", "  m_Roots:" + new_roots, 1)
        else:
            text = re.sub(r'(  m_Roots:(?:\n  - \{fileID: -?\d+\})*)',
                          lambda m: m.group(1) + new_roots, text, count=1)
    return text, updated, len(added_ids), skipped


def _write_scene_meta(scene_path):
    meta_path = scene_path + ".meta"
    existing = _read_existing_meta_guid(meta_path)
    if existing:
        return existing
    guid = _gen_unity_guid()
    with open(meta_path, 'w', encoding='utf-8') as f:
        f.write(
            "fileFormatVersion: 2\n"
            f"guid: {guid}\n"
            "DefaultImporter:\n"
            "  externalObjects: {}\n"
            "  userData: \n"
            "  assetBundleName: \n"
            "  assetBundleVariant: \n"
        )
    return guid


def _resolve_scene_path(export_path):
    root = _resolve_export_root(export_path)
    name = _safe_filename(os.path.basename(root) or "Scene")
    return os.path.join(root, "Scenes", name + ".unity")


class MODULEEXPORT_PT_main_panel(Panel):
    """Панель экспорта модулей"""
    bl_label = "Module Export"
    bl_idname = "MODULEEXPORT_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_order = 7
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        cfg = _mode_settings(scene)
        mode = scene.module_export_mode

        layout.prop(scene, "module_export_path", text="")

        # Режим — крупные кнопки
        col = layout.column(align=True)
        col.scale_y = 1.3
        col.prop(scene, "module_export_mode", expand=True)

        # Сводка: что произойдёт с каждым типом файлов
        box = layout.box()
        desc = next(m[2] for m in _EXPORT_MODES if m[0] == mode)
        if mode != 'CUSTOM':
            sub = box.column(align=True)
            sub.scale_y = 0.75
            for line in _wrap(desc, 42):
                sub.label(text=line)
            box.separator(factor=0.3)

        if mode == 'CUSTOM':
            col = box.column(align=True)
            col.prop(scene, "module_export_unpack_textures", text="Текстуры PNG + материалы Unity")
            col.prop(scene, "module_export_create_prefab", text="Unity префаб (.prefab)")
            row = col.row()
            row.enabled = scene.module_export_create_prefab
            row.prop(scene, "module_export_create_scene", text="Unity сцена (.unity)")
            col.separator()
            row = col.row()
            row.enabled = scene.module_export_create_prefab or scene.module_export_unpack_textures
            row.prop(scene, "module_export_overwrite_prefab", text="Перезаписать .mat / PNG / префабы")
            row = col.row()
            row.enabled = cfg["scene"]
            row.prop(scene, "module_export_overwrite_scene", text="Пересоздать сцену")
            box.separator(factor=0.3)

        col = box.column(align=True)
        for label, make_key, over_key in _MODE_STEPS:
            if make_key is None:
                state, icon = "обновить", 'CHECKMARK'
            elif not cfg[make_key]:
                if make_key == "assets" and (cfg["prefab"] or mode == 'GEOMETRY'):
                    state, icon = "привязать к готовым", 'LINKED'
                else:
                    state, icon = "не трогать", 'REMOVE'
            elif cfg[over_key]:
                state, icon = "перезаписать", 'ERROR'
            else:
                state, icon = "только новые", 'ADD'
            split = col.split(factor=0.5)
            split.active = icon != 'REMOVE'
            split.label(text=label)
            r = split.row()
            r.alert = icon == 'ERROR'
            r.label(text=state, icon=icon)

        # Куда реально лягут файлы
        if scene.module_export_path:
            dirs = [("Meshes", _resolve_sibling_dir(scene.module_export_path, "Meshes"))]
            if cfg["assets"]:
                dirs.append(("Materials", _resolve_sibling_dir(scene.module_export_path, "Materials")))
                dirs.append(("Textures", _resolve_sibling_dir(scene.module_export_path, "Textures")))
            if cfg["prefab"]:
                dirs.append(("Prefabs", _resolve_prefab_dir(scene.module_export_path)))
            if cfg["scene"]:
                dirs.append(("Scene", _resolve_scene_path(scene.module_export_path)))
            root = _resolve_export_root(scene.module_export_path)
            col = box.column(align=True)
            col.scale_y = 0.7
            col.active = False
            for _, d in dirs:
                col.label(text=os.path.relpath(d, os.path.dirname(root)), icon='FILE_FOLDER')

        # Опции
        col = layout.column(align=True)
        col.prop(scene, "module_export_plain_objects", text="Экспортировать выделенные меши")
        col.prop(scene, "module_export_check_duplicates", text="Проверять дубли перед экспортом")

        # Выделение + кнопка
        empties = [o for o in context.selected_objects if o.type == 'EMPTY']
        plain = _plain_mesh_roots(context) if scene.module_export_plain_objects else []
        count = len(empties) + len(plain)
        row = layout.row()
        row.alignment = 'CENTER'
        if count:
            text = f"Домов к экспорту: {count}"
            if plain:
                text += f"  (Empty {len(empties)}, мешей {len(plain)})"
            row.label(text=text, icon='EMPTY_AXIS')
        else:
            row.alert = True
            row.label(text="Выделите Empty дома", icon='INFO')

        row = layout.row()
        row.scale_y = 2.0
        row.enabled = count > 0
        mode_label, mode_icon = next((m[1], m[3]) for m in _EXPORT_MODES if m[0] == mode)
        row.operator("moduleexport.export", text=f"ЭКСПОРТ · {mode_label}", icon=mode_icon)

        row = layout.row()
        row.scale_y = 1.4
        row.enabled = count > 0
        row.operator("moduleexport.update_scene", text="Обновить сцену · выделенные",
                     icon='SCENE_DATA')


class MODULEEXPORT_PT_tools(Panel):
    bl_label = "Инструменты"
    bl_idname = "MODULEEXPORT_PT_tools"
    bl_parent_id = "MODULEEXPORT_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"

    def draw(self, context):
        layout = self.layout
        selected_empties = [obj for obj in context.selected_objects if obj.type == 'EMPTY']
        in_edit = [e for e in selected_empties if e.get("_pivot_edit_active", False)]
        all_in_edit = len(selected_empties) > 0 and len(in_edit) == len(selected_empties)

        col = layout.column(align=True)
        col.scale_y = 1.3
        row = col.row(align=True)
        row.enabled = len(selected_empties) > 0
        row.alert = all_in_edit
        if all_in_edit:
            row.operator("moduleexport.toggle_pivot_edit",
                         text="Edit Pivot: ВЫКЛ (вернуть детей)", icon='LOCKED')
        else:
            row.operator("moduleexport.toggle_pivot_edit",
                         text="Edit Pivot: двигать Empty без детей", icon='UNLOCKED')
        col.operator("moduleexport.fix_materials", text="Фикс материалов (дубли)", icon='MATERIAL')


class MODULEEXPORT_PT_help(Panel):
    bl_label = "Справка"
    bl_idname = "MODULEEXPORT_PT_help"
    bl_parent_id = "MODULEEXPORT_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        sections = (
            ("Режимы", 'QUESTION', [
                "Полный — первый экспорт квартала",
                "Обновить — поменял дома, добавил материалы",
                "Геометрия — правка только меша",
                "Готовые .mat/PNG ищутся и в соседних",
                "кварталах: копий не создаётся",
            ]),
            ("Что делает в Blender", 'BLENDER', [
                "1. Копия иерархии, модификаторы, Instances",
                "2. В центр (0,0,0), UV → 'UVMap'",
                "3. Дубли материалов, Join, нормали",
                "4. Merge 0.0001м, Loose geometry",
                "5. FBX (+ PNG и .mat по режиму)",
            ]),
            ("Что делает в Unity", 'EXPORT', [
                "1. FBX 1:1 (метры) + MeshCollider",
                "2. Материалы → наши .mat (remap)",
                "3. Re-export: GUID сохраняется",
                "4. .prefab = Variant над FBX + Distance/",
                "   AsyncMesh/AsyncCollider",
                "5. .unity: префабы на местах из Blender",
            ]),
        )
        for title, icon, lines in sections:
            box = layout.box()
            box.label(text=title, icon=icon)
            col = box.column(align=True)
            col.scale_y = 0.75
            for line in lines:
                col.label(text=line)


def _wrap(text, width):
    """Перенос строки по словам для label'ов панели."""
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines


class MODULEEXPORT_OT_fix_materials(Operator):
    """Ищет дубли материалов и текстур по содержимому (пиксели + параметры),
    сливает их в один и даёт материалам понятные имена по текстуре.
    Работает по выделенным объектам с детьми (ничего не выделено — вся сцена)"""
    bl_idname = "moduleexport.fix_materials"
    bl_label = "Фикс материалов"
    bl_options = {'REGISTER', 'UNDO'}

    rename: bpy.props.EnumProperty(
        name="Имена",
        items=(
            ('HOUSE', "По дому: M_/T_",
             "SM_Building_01 → материалы M_Building_01_01.., текстуры T_Building_01_01... "
             "Общий для нескольких домов — по первому дому (по алфавиту)"),
            ('TEXTURE', "По имени текстуры", "niz_kvartalD_3.079 → niz_kvartal2.txd_nizhek_224_0"),
            ('NONE', "Не переименовывать", "Только слить дубли"),
        ),
        default='HOUSE',
    )
    merge_untextured: BoolProperty(
        name="Сливать и материалы без текстур",
        description="Одинаковые материалы без текстур (заглушки с цветом) тоже "
                    "объединять. По умолчанию выкл — их имена обычно что-то значат",
        default=False,
    )
    merge_images: BoolProperty(
        name="Слить одинаковые текстуры",
        description="Картинки с одинаковыми пикселями заменяются одной "
                    "(меньше дублей PNG в Unity)",
        default=True,
    )

    def invoke(self, context, event):
        _FP_CACHE.clear()
        plan = _build_plan(context, self.merge_untextured)
        _PENDING_PLAN["fix"] = plan
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        plan = _PENDING_PLAN.get("fix")
        if plan:
            dup_m = [g for g in plan["mat_groups"] if len(g) > 1]
            dup_i = [g for g in plan["img_groups"] if len(g) > 1]
            col = layout.column(align=True)
            col.label(text=f"Материалов: {plan['mat_total']} → уникальных: {len(plan['mat_groups'])}",
                      icon='MATERIAL')
            col.label(text=f"Текстур: {plan['img_total']} → уникальных: {len(plan['img_groups'])}",
                      icon='IMAGE_DATA')
            if dup_m:
                box = layout.box()
                box.label(text="Будут объединены:")
                sub = box.column(align=True)
                sub.scale_y = 0.8
                for g in dup_m[:12]:
                    others = ", ".join(m.name for m in g[1:4]) + (" …" if len(g) > 4 else "")
                    sub.label(text=f"{g[0].name}  ←  {others}")
                if len(dup_m) > 12:
                    sub.label(text=f"… и ещё {len(dup_m) - 12} групп")
            else:
                layout.label(text="Дублей материалов не найдено", icon='CHECKMARK')
            _draw_house_warnings(layout, plan)
        layout.prop(self, "merge_images")
        layout.prop(self, "merge_untextured")
        layout.prop(self, "rename", expand=True)

    def execute(self, context):
        if not _ensure_object_mode(context):
            self.report({'ERROR'}, "Перейдите в Object Mode")
            return {'CANCELLED'}
        _FP_CACHE.clear()
        plan = _build_plan(context, self.merge_untextured)
        if not plan["mat_total"]:
            self.report({'WARNING'}, "Нет материалов у выбранных объектов")
            return {'CANCELLED'}
        for w in plan["house_warnings"]:
            self.report({'WARNING'}, w)
        msg = _apply_material_fix(plan, self.merge_images, self.rename)
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class MODULEEXPORT_OT_toggle_pivot_edit(Operator):
    """Переключает режим редактирования позиции Empty (pivot).
    ВКЛ: дети временно отвязываются с сохранением мировых координат
         — Empty можно свободно двигать без перемещения детей.
    ВЫКЛ: дети возвращаются к Empty с keep_transform — визуально
          остаются на месте, Empty становится новым pivot."""
    bl_idname = "moduleexport.toggle_pivot_edit"
    bl_label = "Toggle Edit Pivot"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_empties = [obj for obj in context.selected_objects if obj.type == 'EMPTY']
        if not selected_empties:
            self.report({'ERROR'}, "Выберите Empty для редактирования Pivot!")
            return {'CANCELLED'}
        if not _ensure_object_mode(context):
            self.report({'ERROR'}, "Перейдите в Object Mode")
            return {'CANCELLED'}

        # Сохраняем активный объект и selection
        prev_active = context.view_layer.objects.active
        prev_selected = list(context.selected_objects)

        enabled_count = 0
        disabled_count = 0
        skipped_no_children = 0

        for empty in selected_empties:
            in_edit = bool(empty.get("_pivot_edit_active", False))

            if not in_edit:
                # === ВКЛЮЧИТЬ Edit Pivot: отвязать детей ===
                # Фильтруем детей: только те что в текущем View Layer (иначе select_set упадёт)
                children = [c for c in empty.children if c.name in context.view_layer.objects]
                if not children:
                    skipped_no_children += 1
                    continue

                empty["_pivot_edit_children"] = [c.name for c in children]
                empty["_pivot_edit_active"] = True

                _deselect_all(context)
                for c in children:
                    try:
                        c.select_set(True)
                    except RuntimeError:
                        continue
                context.view_layer.objects.active = children[0]
                bpy.ops.object.parent_clear(type='CLEAR_KEEP_TRANSFORM')

                enabled_count += 1
            else:
                # === ВЫКЛЮЧИТЬ Edit Pivot: вернуть parent с keep_transform ===
                child_names = list(empty.get("_pivot_edit_children", []))
                children = [bpy.data.objects.get(n) for n in child_names]
                children = [c for c in children if c is not None and c.name in context.view_layer.objects]

                if children:
                    _deselect_all(context)
                    for c in children:
                        c.select_set(True)
                    empty.select_set(True)
                    context.view_layer.objects.active = empty
                    bpy.ops.object.parent_set(type='OBJECT', keep_transform=True)

                if "_pivot_edit_children" in empty:
                    del empty["_pivot_edit_children"]
                empty["_pivot_edit_active"] = False

                disabled_count += 1

        # Восстанавливаем selection и active
        _deselect_all(context)
        for o in prev_selected:
            try:
                o.select_set(True)
            except Exception:
                pass
        if prev_active is not None and prev_active.name in bpy.data.objects:
            context.view_layer.objects.active = prev_active

        msg_parts = []
        if enabled_count:
            msg_parts.append(f"Edit Pivot ВКЛ: {enabled_count}")
        if disabled_count:
            msg_parts.append(f"Edit Pivot ВЫКЛ: {disabled_count}")
        if skipped_no_children:
            msg_parts.append(f"Пропущено (нет детей): {skipped_no_children}")
        self.report({'INFO'}, " | ".join(msg_parts) if msg_parts else "Нет изменений")

        return {'FINISHED'}


class MODULEEXPORT_OT_export(Operator):
    """Экспортировать выбранные объекты/коллекции в FBX"""
    bl_idname = "moduleexport.export"
    bl_label = "Экспорт модулей"
    bl_options = {'REGISTER', 'UNDO'}

    dup_action: bpy.props.EnumProperty(
        name="Дубли",
        items=(
            ('FIX', "Сначала исправить, потом экспорт",
             "Слить дубли и назвать M_<Дом>_NN / T_<Дом>_NN (как кнопка «Фикс материалов»), затем экспорт"),
            ('IGNORE', "Экспортировать как есть", "Дубли уедут в Unity отдельными материалами"),
        ),
        default='FIX',
        options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        """Перед экспортом — проверка дублей материалов в выделенном."""
        _PENDING_PLAN.pop("export", None)
        if not context.scene.module_export_check_duplicates or not _ensure_object_mode(context):
            return self.execute(context)
        _FP_CACHE.clear()
        plan = _build_plan(context)
        if _dup_counts(plan) == (0, 0) and not plan["house_warnings"]:
            return self.execute(context)
        _PENDING_PLAN["export"] = plan
        return context.window_manager.invoke_props_dialog(
            self, width=460, title="Проверка перед экспортом", confirm_text="Продолжить")

    def draw(self, context):
        plan = _PENDING_PLAN.get("export")
        layout = self.layout
        if not plan:
            return
        dm, di = _dup_counts(plan)
        col = layout.column(align=True)
        col.label(text=f"Материалов: {plan['mat_total']} → уникальных: {len(plan['mat_groups'])}"
                       f"  (дублей {dm})", icon='MATERIAL')
        col.label(text=f"Текстур: {plan['img_total']} → уникальных: {len(plan['img_groups'])}"
                       f"  (дублей {di})", icon='IMAGE_DATA')
        groups = [g for g in plan["mat_groups"] if len(g) > 1]
        if groups:
            box = layout.box()
            sub = box.column(align=True)
            sub.scale_y = 0.8
            for g in groups[:6]:
                others = ", ".join(m.name for m in g[1:3]) + (" …" if len(g) > 3 else "")
                sub.label(text=f"{g[0].name}  ←  {others}")
            if len(groups) > 6:
                sub.label(text=f"… и ещё {len(groups) - 6} групп")
        _draw_house_warnings(layout, plan)
        layout.prop(self, "dup_action", expand=True)

    def get_all_mesh_children(self, obj, meshes=None):
        """Рекурсивно собирает все меши из иерархии объекта"""
        if meshes is None:
            meshes = []

        if obj.type == 'MESH':
            meshes.append(obj)

        for child in obj.children:
            self.get_all_mesh_children(child, meshes)

        return meshes

    def rename_uv_layers(self, mesh_data):
        """Переименовывает все UV слои в 'UVMap'"""
        if mesh_data.uv_layers:
            # Переименовываем первый UV слой в UVMap
            if len(mesh_data.uv_layers) > 0:
                mesh_data.uv_layers[0].name = "UVMap"
            # Удаляем остальные UV слои если они есть (оставляем только первый)
            while len(mesh_data.uv_layers) > 1:
                mesh_data.uv_layers.remove(mesh_data.uv_layers[1])

    def get_base_material_name(self, mat_name):
        """
        Извлекает базовое имя материала без суффиксов .001, .002 и т.д.
        Например: 'Material.001' -> 'Material'
        """
        # Паттерн для поиска суффиксов типа .001, .002 и т.д.
        pattern = r'^(.+)\.\d{3}$'
        match = re.match(pattern, mat_name)
        if match:
            return match.group(1)
        return mat_name

    def cleanup_duplicate_materials(self, obj):
        """
        Заменяет дубликаты материалов (.001, .002) на оригинальные.
        """
        if not obj.data.materials:
            return

        for i, mat_slot in enumerate(obj.material_slots):
            if mat_slot.material is None:
                continue

            mat = mat_slot.material
            base_name = self.get_base_material_name(mat.name)

            # Если имя отличается от базового - возможно дубликат. Подменяем
            # ТОЛЬКО если содержимое совпадает: 'X.079' с другой текстурой —
            # это другой материал, а не дубль 'X'
            if base_name != mat.name:
                original_mat = bpy.data.materials.get(base_name)
                if (original_mat is not None
                        and _material_signature(original_mat) == _material_signature(mat)):
                    obj.data.materials[i] = original_mat

    def create_deep_copy(self, context, root_obj):
        """
        Создаёт полную копию иерархии объектов с учётом world transform.
        Применяет модификаторы через дублирование и применение.
        Возвращает список копий мешей.
        """
        # Собираем все оригинальные меши
        original_meshes = self.get_all_mesh_children(root_obj)

        # Фильтруем: только меши в активном View Layer (иначе select_set упадёт)
        vl_objects = context.view_layer.objects
        filtered = []
        for m in original_meshes:
            if m.name in vl_objects:
                filtered.append(m)
            else:
                self.report({'WARNING'}, f"Пропущен (не в View Layer): {m.name}")
        original_meshes = filtered

        if not original_meshes:
            return []

        # Позиция корневого объекта
        root_world_matrix = root_obj.matrix_world.copy()
        # ПОЛНЫЙ inverse: «приводим Empty в ноль» — вся позиция/rotation/scale Empty
        # вычитается, в mesh data остаются только LOCAL трансформы детей относительно Empty.
        # В Unity пишем position + rotation + scale в Transform override префаба.
        root_world_inv = root_world_matrix.inverted()

        duplicates = []

        for orig_obj in original_meshes:
            # Получаем world matrix оригинала
            orig_world_matrix = orig_obj.matrix_world.copy()

            # Relative = child in Empty's local space.
            # Ничего от Empty не запекается — только child local transforms.
            relative_matrix = root_world_inv @ orig_world_matrix

            # Проверяем на отрицательный scale
            has_negative_scale = relative_matrix.determinant() < 0

            # Создаём дубликат объекта через оператор (сохраняет модификаторы)
            _deselect_all(context)
            orig_obj.select_set(True)
            context.view_layer.objects.active = orig_obj

            # Надёжно находим новую копию: запоминаем объекты до дублирования,
            # затем берём разницу. НЕ доверяем context.active_object — после
            # оператора он может быть None (отменённый дубликат, проблемы контекста).
            before = set(context.view_layer.objects)
            bpy.ops.object.duplicate(linked=False)
            new_objs = [o for o in context.view_layer.objects if o not in before]

            # Fallback на выделение/активный объект, если diff пуст
            if not new_objs:
                new_objs = list(context.selected_objects)
            new_obj = new_objs[0] if new_objs else context.active_object

            if new_obj is None:
                self.report({'WARNING'}, f"Не удалось дублировать '{orig_obj.name}', пропускаю")
                continue

            # Делаем копию активной и единственной выделенной для дальнейших операторов
            _deselect_all(context)
            new_obj.select_set(True)
            context.view_layer.objects.active = new_obj

            new_obj.name = orig_obj.name + "_export_temp"

            # Убираем parent чтобы не было проблем с трансформациями
            new_obj.parent = None
            new_obj.matrix_world = Matrix.Identity(4)

            # Применяем модификаторы
            if new_obj.modifiers:
                _deselect_all(context)
                new_obj.select_set(True)
                context.view_layer.objects.active = new_obj

                # Применяем каждый модификатор
                for mod in new_obj.modifiers[:]:
                    try:
                        bpy.ops.object.modifier_apply(modifier=mod.name)
                    except:
                        # Если не удалось применить - удаляем
                        new_obj.modifiers.remove(mod)

            # Теперь применяем относительную трансформацию к мешу
            new_obj.data.transform(relative_matrix)

            # Исправляем нормали если был отрицательный scale
            if has_negative_scale:
                new_obj.data.flip_normals()

            # Объект остаётся в нулевых координатах
            new_obj.location = (0, 0, 0)
            new_obj.rotation_euler = (0, 0, 0)
            new_obj.scale = (1, 1, 1)

            # Унифицируем UV
            self.rename_uv_layers(new_obj.data)

            # Очищаем дубликаты материалов
            self.cleanup_duplicate_materials(new_obj)

            duplicates.append(new_obj)

        return duplicates

    def remove_loose_geometry(self, context):
        """Удаляет неиспользуемую геометрию (loose verts, edges)"""
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')

        # Удаляем loose вершины (не соединённые с гранями)
        bpy.ops.mesh.delete_loose(
            use_verts=True,
            use_edges=True,
            use_faces=False
        )

        bpy.ops.object.mode_set(mode='OBJECT')

    def export_texture(self, img, assets, is_normal=False):
        """PNG + .png.meta (один раз на картинку за экспорт). Возвращает guid или None."""
        key = img.name_full
        if key in assets["tex_done"]:
            path, guid = assets["tex_done"][key]
            if path and is_normal and key not in assets["tex_normal"]:
                assets["tex_normal"].add(key)
                guid = _write_texture_meta(path, True)
            return guid

        target = _png_target_path(img, assets["tex_dir"], assets["tex_paths"], key)
        found = _find_existing_asset(os.path.basename(target), assets["tex_dir"])
        own = found is not None and os.path.dirname(found) == os.path.normpath(assets["tex_dir"])
        if found and (not own or not assets["overwrite"]):
            # PNG уже есть (у себя или у соседей) — переиспользуем, не пишем заново
            path = found
            assets["reused_textures"] += 1
        else:
            path = _save_image_as_png(img, target)
            if path:
                assets["written_textures"].append(path)
        guid = None
        if path:
            assets["tex_paths"][path] = key
            guid = _write_texture_meta(path, is_normal)
            if is_normal:
                assets["tex_normal"].add(key)
        else:
            self.report({'WARNING'}, f"Текстура не сохранилась: {img.name}")
        assets["tex_done"][key] = (path, guid)
        return guid

    def export_materials(self, obj, assets):
        """Unity .mat (Standard) на каждый материал объекта.
        Возвращает {имя материала в FBX: guid .mat}."""
        material_map = {}
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in material_map:
                continue
            if mat.name in assets["mat_done"]:
                material_map[mat.name] = assets["mat_done"][mat.name]
                continue

            # Материал соседнего квартала — берём его .mat, а не плодим копию
            found = _find_existing_mat(mat.name, assets["mat_dir"])
            if found and os.path.dirname(found) != os.path.normpath(assets["mat_dir"]):
                guid = _read_existing_meta_guid(found + ".meta")
                if guid:
                    assets["mat_done"][mat.name] = guid
                    assets["reused_materials"] += 1
                    material_map[mat.name] = guid
                    continue

            assets["local_mats"].append(mat)
            info = _material_info(mat)
            albedo_guid = self.export_texture(info["albedo"], assets) if info["albedo"] else None
            normal_guid = self.export_texture(info["normal"], assets, True) if info["normal"] else None

            mat_name = _safe_filename(mat.name)
            mat_path = os.path.join(assets["mat_dir"], mat_name + ".mat")
            if not os.path.exists(mat_path) or assets["overwrite"]:
                with open(mat_path, 'w', encoding='utf-8') as f:
                    f.write(_build_unity_material_yaml(mat_name, info, albedo_guid, normal_guid))
                assets["written_materials"] += 1
            else:
                assets["reused_materials"] += 1
            guid = _write_material_meta(mat_path)
            assets["mat_done"][mat.name] = guid
            material_map[mat.name] = guid
        # Unity при импорте FBX заменяет спецсимволы в именах (Glass:1 → Glass_1) —
        # remap пишем на оба варианта имени
        for name, guid in list(material_map.items()):
            material_map.setdefault(_safe_filename(name), guid)
        return material_map

    def existing_material_map(self, obj, mat_dir, export_name):
        """Без распаковки текстур: привязать материалы FBX к уже готовым .mat
        (своим или соседних кварталов), чтобы re-export не давал белых."""
        material_map = {}
        missing = []
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in material_map:
                continue
            found = _find_existing_mat(mat.name, mat_dir)
            guid = _read_existing_meta_guid(found + ".meta") if found else None
            if guid:
                material_map[mat.name] = guid
            else:
                missing.append(mat.name)
        if missing:
            self.report({'WARNING'}, f"'{export_name}': нет .mat для {', '.join(missing)} — "
                                     f"в Unity будут белыми. Экспортируйте режимом «Обновить модели»")
        for name, guid in list(material_map.items()):
            material_map.setdefault(_safe_filename(name), guid)
        return material_map

    def process_and_export(self, context, root_obj, export_name, export_path, assets=None,
                           mat_dir=None):
        """Обрабатывает и экспортирует один модуль.
        Возвращает (ok, путь_или_ошибка, {материал: guid .mat})."""

        # Проверяем тип объекта
        if root_obj.type == 'EMPTY':
            pass  # OK
        elif root_obj.type == 'MESH':
            pass  # OK
        else:
            return False, "Неподдерживаемый тип объекта", {}

        # Создаём глубокие копии с применёнными трансформациями и модификаторами
        duplicates = self.create_deep_copy(context, root_obj)

        if not duplicates:
            return False, f"Нет мешей в '{export_name}'", {}

        # Выделяем все дубликаты для Join
        _deselect_all(context)
        for dup in duplicates:
            dup.select_set(True)
        context.view_layer.objects.active = duplicates[0]

        # Объединяем если больше одного меша
        if len(duplicates) > 1:
            bpy.ops.object.join()

        # Теперь у нас один объект
        final_obj = context.active_object

        # Ещё раз убеждаемся что UV унифицирован после Join
        self.rename_uv_layers(final_obj.data)

        # Ещё раз очищаем дубликаты материалов после Join
        self.cleanup_duplicate_materials(final_obj)

        # Слияние вершин по расстоянию
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')

        # Merge by Distance с настройками как на скриншоте
        # Centroid Merge включен, Unselected выключен, Sharp Edges включен
        bpy.ops.mesh.remove_doubles(
            threshold=0.0001,  # Merge Distance (0.1мм — соответствует UI)
            use_unselected=False,  # Unselected выключен
            use_sharp_edge_from_normals=True  # Sharp Edges включен
        )

        bpy.ops.object.mode_set(mode='OBJECT')

        # Удаляем loose geometry (висячие вершины и рёбра)
        self.remove_loose_geometry(context)

        # Объект уже в нулевых координатах
        final_obj.location = (0, 0, 0)
        final_obj.rotation_euler = (0, 0, 0)
        final_obj.scale = (1, 1, 1)

        # Убираем все конфликты по имени — иначе Blender вешает .NNN суффикс:
        #   (a) Объект с таким же именем — обычно это сам Empty!
        #   (b) Mesh data с таким же именем — оригинал юзера или остатки от прошлых exports

        # (a) Отодвигаем объект-конфликт (Empty или другой)
        conflicting_obj = bpy.data.objects.get(export_name)
        conflicting_obj_old_name = None
        if conflicting_obj is not None and conflicting_obj != final_obj:
            conflicting_obj_old_name = conflicting_obj.name
            conflicting_obj.name = f"__bak_obj_{export_name}"

        # (b) Подчищаем orphan mesh data (users == 0) с похожими именами
        for m in list(bpy.data.meshes):
            if m == final_obj.data:
                continue
            if m.users == 0 and (m.name == export_name or m.name.startswith(export_name + ".")):
                bpy.data.meshes.remove(m)

        # (c) Отодвигаем mesh data с таким же именем (если есть с users)
        existing_mesh = bpy.data.meshes.get(export_name)
        existing_mesh_old_name = None
        if existing_mesh and existing_mesh != final_obj.data:
            existing_mesh_old_name = existing_mesh.name
            existing_mesh.name = f"__bak_mesh_{export_name}"

        # Теперь путь свободен — именуем объект и mesh data чисто
        final_obj.name = export_name
        final_obj.data.name = export_name

        # Текстуры + материалы Unity — до экспорта, пока временный объект ещё жив
        material_map = {}
        if assets is not None:
            try:
                assets["local_mats"] = []
                material_map = self.export_materials(final_obj, assets)
            except Exception as e:
                self.report({'WARNING'}, f"Материалы '{export_name}': {e}")
            # Остальные картинки (не albedo/normal) — тоже в PNG, но только
            # своих материалов: у чужих (соседний квартал) всё уже лежит у соседей
            for mat in assets["local_mats"]:
                for img in _material_images(mat):
                    self.export_texture(img, assets)
        elif mat_dir:
            material_map = self.existing_material_map(final_obj, mat_dir, export_name)

        # Distance.distances + LocalBounds — по финальному мешу (как Calculate в Unity)
        self.last_distance_data = None
        try:
            unit = float(context.scene.unit_settings.scale_length or 1.0)
            self.last_distance_data = _calc_distance_data(final_obj.data, unit)
        except Exception as e:
            self.report({'WARNING'}, f"Distance '{export_name}' не посчитан: {e}")

        # Формируем путь экспорта
        abs_export_path = bpy.path.abspath(export_path)
        if not os.path.exists(abs_export_path):
            os.makedirs(abs_export_path)

        filepath = os.path.join(abs_export_path, export_name + ".fbx")

        # Экспорт в FBX с настройками как на скриншоте
        bpy.ops.export_scene.fbx(
            filepath=filepath,
            use_selection=True,
            # Include
            use_visible=False,
            use_active_collection=False,
            object_types={'MESH'},
            use_custom_props=False,
            # Transform
            global_scale=1.0,
            apply_scale_options='FBX_SCALE_UNITS',
            axis_forward='-Z',
            axis_up='Y',
            apply_unit_scale=True,
            use_space_transform=True,
            bake_space_transform=True,
            # Geometry
            mesh_smooth_type='FACE',  # Normals Only
            use_subsurf=False,
            use_mesh_modifiers=False,  # Модификаторы уже применены
            use_mesh_edges=False,
            use_triangles=False,
            use_tspace=False,
            colors_type='SRGB',
            prioritize_active_color=False,
            # Armature
            primary_bone_axis='Y',
            secondary_bone_axis='X',
            armature_nodetype='NULL',
            use_armature_deform_only=False,
            add_leaf_bones=True,
            # Animation
            bake_anim=False,
            # Textures: если они уже вынесены в PNG + .mat — в FBX не вшиваем
            # (иначе FBX раздувается на сотни МБ, а Unity их всё равно не использует)
            path_mode='STRIP' if assets is not None or material_map else 'COPY',
            embed_textures=assets is None and not material_map,
            batch_mode='OFF',
        )

        # Удаляем временный объект И его mesh data (чтобы не оставалось orphan)
        mesh_data_name = final_obj.data.name
        bpy.ops.object.delete()
        orphan = bpy.data.meshes.get(mesh_data_name)
        if orphan is not None and orphan.users == 0:
            bpy.data.meshes.remove(orphan)

        # Восстанавливаем имена отодвинутых объекта и mesh data юзера
        if conflicting_obj is not None and conflicting_obj_old_name:
            try:
                conflicting_obj.name = conflicting_obj_old_name
            except Exception:
                pass
        if existing_mesh is not None and existing_mesh_old_name:
            try:
                existing_mesh.name = existing_mesh_old_name
            except Exception:
                pass

        return True, filepath, material_map

    def add_scene_entry(self, entries, create_scene, name, prefab_path, prefab_guid, world_matrix):
        if not create_scene:
            return
        inst_id = _read_prefab_instance_id(prefab_path)
        if not prefab_guid or inst_id is None:
            self.report({'WARNING'}, f"'{name}' не попадёт в сцену: префаб не в формате "
                                     f"Variant (пересоберите режимом «Полный экспорт»)")
            return
        entries.append((name, prefab_guid, inst_id, world_matrix))

    def write_scene(self, export_path, entries, unit_scale, overwrite):
        scene_path = _resolve_scene_path(export_path)
        if os.path.exists(scene_path) and not overwrite:
            self.report({'INFO'}, f"Сцена существует, пропуск: {scene_path}")
            return "Сцена: пропущена (уже есть)"
        try:
            os.makedirs(os.path.dirname(scene_path), exist_ok=True)
            with open(scene_path, 'w', encoding='utf-8') as f:
                f.write(_build_unity_scene_yaml(entries, unit_scale))
            _write_scene_meta(scene_path)
        except Exception as e:
            self.report({'WARNING'}, f"Ошибка сцены: {e}")
            return "Сцена: ошибка"
        self.report({'INFO'}, f"Сцена ({len(entries)} объектов): {scene_path}")
        return f"Сцена: {len(entries)} объектов"

    def execute(self, context):
        scene = context.scene
        export_path = scene.module_export_path
        cfg = _mode_settings(scene)
        create_prefab = cfg["prefab"]
        overwrite_prefab = cfg["overwrite_prefab"]

        if not export_path:
            self.report({'ERROR'}, "Укажите путь экспорта!")
            return {'CANCELLED'}

        # Дубли, найденные в invoke: исправляем до экспорта, если так выбрали
        plan = _PENDING_PLAN.pop("export", None)
        if plan is not None and self.dup_action == 'FIX':
            self.report({'INFO'}, "Фикс материалов: " + _apply_material_fix(plan, rename='HOUSE'))
        if plan is not None:
            for w in plan.get("house_warnings", []):
                self.report({'WARNING'}, w)
        _FP_CACHE.clear()

        if not _ensure_object_mode(context):
            self.report({'ERROR'}, "Не удалось перейти в Object Mode — выйдите из "
                                   "Edit/Paint режима и повторите экспорт")
            return {'CANCELLED'}

        plain_objects_on = scene.module_export_plain_objects
        unpack_textures = cfg["assets"]

        # Собираем выбранные Empty объекты
        selected_empties = [obj for obj in context.selected_objects if obj.type == 'EMPTY']
        # ...и, если включена галочка, обычные объекты — каждый уходит своим FBX
        plain_objects = _plain_mesh_roots(context) if plain_objects_on else []

        if not selected_empties and not plain_objects:
            if plain_objects_on:
                self.report({'ERROR'}, "Выберите Empty или меш-объекты для экспорта!")
            else:
                self.report({'ERROR'}, "Выберите Empty! Для обычных объектов включите "
                                       "галочку «Экспортировать выделенные объекты»")
            return {'CANCELLED'}

        # Empty в режиме Edit Pivot — нет детей, экспорт сломается. Пропускаем.
        in_edit = [e for e in selected_empties if e.get("_pivot_edit_active", False)]
        if in_edit:
            names = ", ".join(e.name for e in in_edit)
            self.report({'WARNING'}, f"Пропущены (Edit Pivot ВКЛ): {names}")
            selected_empties = [e for e in selected_empties if not e.get("_pivot_edit_active", False)]
            if not selected_empties and not plain_objects:
                self.report({'ERROR'}, "Все Empty в режиме Edit Pivot — выключи перед экспортом!")
                return {'CANCELLED'}

        export_roots = list(selected_empties) + list(plain_objects)

        # Снимаем мировые матрицы ДО любых модификаций (чтобы записать позицию в префаб)
        empty_world_matrices = {o.name: o.matrix_world.copy() for o in export_roots}

        # Вся иерархия внутри указанной папки: Meshes / Prefabs / Textures / Materials
        meshes_dir = _resolve_sibling_dir(export_path, "Meshes")
        assets = None
        if unpack_textures:
            assets = {
                "tex_dir": _resolve_sibling_dir(export_path, "Textures"),
                "mat_dir": _resolve_sibling_dir(export_path, "Materials"),
                "overwrite": cfg["overwrite_assets"],
                "tex_done": {}, "tex_paths": {}, "tex_normal": set(),
                "mat_done": {}, "written_textures": [], "written_materials": 0,
                "reused_textures": 0, "reused_materials": 0, "local_mats": [],
            }
            try:
                os.makedirs(assets["tex_dir"], exist_ok=True)
                os.makedirs(assets["mat_dir"], exist_ok=True)
            except Exception as e:
                self.report({'ERROR'}, f"Не могу создать папку текстур/материалов: {e}")
                return {'CANCELLED'}

        # Папка префабов
        prefab_dir = None
        if create_prefab:
            prefab_dir = _resolve_prefab_dir(export_path)
            try:
                os.makedirs(prefab_dir, exist_ok=True)
            except Exception as e:
                self.report({'ERROR'}, f"Не могу создать папку префабов: {e}")
                return {'CANCELLED'}

        exported_count = 0
        prefab_count = 0
        errors = []
        create_scene = create_prefab and cfg["scene"]
        scene_entries = []  # (name, prefab_guid, prefab_instance_id, world_matrix)
        unit_scale = float(scene.unit_settings.scale_length or 1.0)

        # Экспортируем каждый корень: Empty-модули и обычные объекты одинаково
        for empty_obj in export_roots:
            export_name = empty_obj.name
            success, result, material_map = self.process_and_export(
                context, empty_obj, export_name, meshes_dir, assets=assets,
                mat_dir=_resolve_sibling_dir(export_path, "Materials"),
            )
            if not success:
                errors.append(result)
                continue

            exported_count += 1
            fbx_path = result
            self.report({'INFO'}, f"FBX: {fbx_path}")

            # .fbx.meta пишем всегда, когда есть что связать: материалы и/или префаб
            fbx_guid = None
            if create_prefab or material_map:
                try:
                    fbx_guid = _write_fbx_meta(fbx_path, _gen_unity_guid(), material_map,
                                               patch_colliders=create_prefab)
                except Exception as e:
                    self.report({'WARNING'}, f"Ошибка .fbx.meta '{export_name}': {e}")

            if not create_prefab or fbx_guid is None:
                continue

            # Генерация .prefab + .meta
            prefab_path = os.path.join(prefab_dir, export_name + ".prefab")
            world_matrix = empty_world_matrices[export_name]
            repair = (os.path.exists(prefab_path) and not overwrite_prefab
                      and _prefab_has_broken_lod_ref(prefab_path))
            if repair:
                self.report({'INFO'}, f"Префаб старого формата (пустой Distance) — пересобираю: {prefab_path}")
            if os.path.exists(prefab_path) and not overwrite_prefab and not repair:
                self.report({'INFO'}, f"Префаб существует, пропуск: {prefab_path}")
                self.add_scene_entry(scene_entries, create_scene, export_name, prefab_path,
                                     _read_existing_meta_guid(prefab_path + ".meta"), world_matrix)
                continue

            try:
                yaml_text = _build_unity_prefab_yaml(
                    export_name, world_matrix, fbx_guid, unit_scale=unit_scale,
                    distance_data=self.last_distance_data,
                    base_id=_read_prefab_base_id(prefab_path),
                )
                with open(prefab_path, 'w', encoding='utf-8') as f:
                    f.write(yaml_text)
                prefab_guid = _write_prefab_meta(prefab_path, _gen_unity_guid())
                prefab_count += 1
                self.add_scene_entry(scene_entries, create_scene, export_name, prefab_path,
                                     prefab_guid, world_matrix)
                self.report({'INFO'}, f"Префаб (Variant, unit={unit_scale}): {prefab_path}")
            except Exception as e:
                self.report({'WARNING'}, f"Ошибка префаба '{export_name}': {e}")

        scene_msg = ""
        if create_scene and scene_entries:
            scene_msg = self.write_scene(export_path, scene_entries, unit_scale,
                                         cfg["overwrite_scene"])

        if errors:
            for err in errors:
                self.report({'WARNING'}, err)

        if exported_count > 0:
            msg = f"FBX: {exported_count}"
            if create_prefab:
                msg += f" | Префабов: {prefab_count}"
            if assets is not None:
                msg += (f" | Материалов: новых {assets['written_materials']},"
                        f" готовых {assets['reused_materials']}"
                        f" | Текстур: новых {len(assets['written_textures'])},"
                        f" готовых {assets['reused_textures']}")
            if create_prefab:
                msg += f" → {prefab_dir}"
            if scene_msg:
                msg += f" | {scene_msg}"
            self.report({'INFO'}, msg)
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Ничего не экспортировано")
            return {'CANCELLED'}


# Регистрация свойств сцены
class MODULEEXPORT_OT_update_scene(Operator):
    """Обновить в сцене Unity позиции выделенных домов и добавить недостающие.
    FBX, префабы и материалы не трогаются, остальные объекты сцены — тоже"""
    bl_idname = "moduleexport.update_scene"
    bl_label = "Обновить сцену"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        export_path = scene.module_export_path
        if not export_path:
            self.report({'ERROR'}, "Укажите путь экспорта!")
            return {'CANCELLED'}
        roots = [o for o in context.selected_objects if o.type == 'EMPTY']
        if scene.module_export_plain_objects:
            roots += _plain_mesh_roots(context)
        if not roots:
            self.report({'ERROR'}, "Выделите Empty дома")
            return {'CANCELLED'}

        prefab_dir = _resolve_prefab_dir(export_path)
        unit_scale = float(scene.unit_settings.scale_length or 1.0)
        entries, no_prefab = [], []
        for obj in roots:
            prefab_path = os.path.join(prefab_dir, obj.name + ".prefab")
            guid = _read_existing_meta_guid(prefab_path + ".meta")
            inst_id = _read_prefab_instance_id(prefab_path) if guid else None
            if inst_id is None:
                no_prefab.append(obj.name)
                continue
            entries.append((obj.name, guid, inst_id, obj.matrix_world.copy()))
        if no_prefab:
            self.report({'WARNING'}, "Нет префаба (сначала экспорт): " + ", ".join(no_prefab))
        if not entries:
            self.report({'ERROR'}, "Ни у одного выделенного дома нет префаба в " + prefab_dir)
            return {'CANCELLED'}

        scene_path = _resolve_scene_path(export_path)
        try:
            if os.path.exists(scene_path):
                with open(scene_path, 'r', encoding='utf-8') as f:
                    text = f.read()
                text, updated, added, skipped = _update_scene_yaml(text, entries, unit_scale)
            else:
                text = _build_unity_scene_yaml(entries, unit_scale)
                updated, added, skipped = 0, len(entries), []
            os.makedirs(os.path.dirname(scene_path), exist_ok=True)
            with open(scene_path, 'w', encoding='utf-8') as f:
                f.write(text)
            _write_scene_meta(scene_path)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка сцены: {e}")
            return {'CANCELLED'}

        if skipped:
            self.report({'WARNING'}, "Не тронуты (в Unity вложены в другой объект): "
                        + ", ".join(sorted(set(skipped))))
        self.report({'INFO'}, f"Сцена {os.path.basename(scene_path)}: обновлено {updated},"
                              f" добавлено {added}")
        return {'FINISHED'}


def register_properties():
    bpy.types.Scene.module_export_path = StringProperty(
        name="Export Path",
        description="Корневая папка: внутри создаются Meshes, Prefabs, Textures, Materials",
        default="//",
        subtype='DIR_PATH'
    )
    bpy.types.Scene.module_export_create_prefab = BoolProperty(
        name="Создать Unity префаб",
        description="После FBX генерирует .prefab + .meta в подпапке Prefabs",
        default=True
    )
    bpy.types.Scene.module_export_overwrite_prefab = BoolProperty(
        name="Перезаписать префаб",
        description="Перезаписывает существующие .prefab и .mat. ВНИМАНИЕ: у инстансов "
                    "префаба в сценах могут слететь оверрайды, правки материалов в Unity пропадут",
        default=False
    )
    bpy.types.Scene.module_export_create_scene = BoolProperty(
        name="Создать Unity сцену",
        description="Собирает <папка>/Scenes/<имя папки>.unity: все экспортированные "
                    "префабы стоят на тех же местах, что и в Blender",
        default=False
    )
    bpy.types.Scene.module_export_overwrite_scene = BoolProperty(
        name="Пересоздать сцену",
        description="ВКЛ — сцена собирается заново из текущего экспорта (правки в Unity "
                    "пропадут). ВЫКЛ — существующая сцена не трогается",
        default=False
    )
    bpy.types.Scene.module_export_check_duplicates = BoolProperty(
        name="Проверять дубли перед экспортом",
        description="При нажатии «Экспорт» сначала ищет дубли материалов/текстур "
                    "в выделенном и предлагает исправить их до экспорта",
        default=True
    )
    bpy.types.Scene.module_export_plain_objects = BoolProperty(
        name="Экспортировать выделенные объекты",
        description="Кроме Empty-модулей экспортировать и обычные выделенные меши — "
                    "каждый своим FBX, по тому же пайплайну. Меш внутри выделенного "
                    "родителя не дублируется: он уедет в составе родителя",
        default=False
    )
    bpy.types.Scene.module_export_unpack_textures = BoolProperty(
        name="Распаковать текстуры в PNG",
        description="Сохраняет все текстуры материалов в подпапку Textures "
                    "всегда в PNG (включая запакованные в .blend и jpg/tga/psd), "
                    "создаёт Unity-материалы .mat в папке Materials и привязывает их к FBX",
        default=False
    )
    bpy.types.Scene.module_export_mode = EnumProperty(
        name="Режим экспорта",
        items=[(k, n, d, i, v) for k, n, d, i, v in _EXPORT_MODES],
        default='UPDATE',
    )

def unregister_properties():
    del bpy.types.Scene.module_export_path
    del bpy.types.Scene.module_export_create_prefab
    del bpy.types.Scene.module_export_overwrite_prefab
    del bpy.types.Scene.module_export_create_scene
    del bpy.types.Scene.module_export_overwrite_scene
    del bpy.types.Scene.module_export_check_duplicates
    del bpy.types.Scene.module_export_plain_objects
    del bpy.types.Scene.module_export_unpack_textures
    del bpy.types.Scene.module_export_mode


classes = (
    MODULEEXPORT_PT_main_panel,
    MODULEEXPORT_PT_tools,
    MODULEEXPORT_PT_help,
    MODULEEXPORT_OT_toggle_pivot_edit,
    MODULEEXPORT_OT_fix_materials,
    MODULEEXPORT_OT_export,
    MODULEEXPORT_OT_update_scene,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    register_properties()


def unregister():
    unregister_properties()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
