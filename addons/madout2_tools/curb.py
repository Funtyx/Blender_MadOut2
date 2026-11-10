bl_info = {
    "name": "Clean G-Curb From Selected Edges (Live Preview)",
    "author": "Dmitriy",
    "version": (5, 26, 0),
    "blender": (5, 0, 0),
    "location": "3D Viewport > N-panel > Curb",
    "description": (
        "Бордюр буквой Г по выделенным рёбрам (8 профилей, выбор частей) "
        "плюс режим Bridge Fill — заливка между двумя цепочками рёбер с "
        "тайлящимся UV. Живое превью, полный UV-контроль и постоянные "
        "пресеты на все параметры с векторным каталогом."
    ),
    "category": "Object",
}

import bpy
import bpy.utils.previews
import bmesh
import math
import json
import os
from mathutils import Matrix, Vector

try:
    import numpy as np
except ImportError:  # numpy идёт со всеми официальными сборками Blender
    np = None
from bpy.props import (
    FloatProperty,
    StringProperty,
    PointerProperty,
    EnumProperty,
    IntProperty,
    BoolProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, Menu
from bpy.app.handlers import persistent


# ------------------------------------------------------------------------
# ПРЕСЕТЫ — хранилище на диске
# ------------------------------------------------------------------------

PRESET_DIRNAME = "clean_g_curb"
PRESET_FILENAME = "curb_presets.json"
PRESET_FILE_VERSION = 1
NO_PRESET = "__NONE__"

# Все параметры, которые попадают в пресет (геометрия + UV + материал + bake)
PRESET_KEYS = (
    # режим
    "build_mode",
    "bridge_pairing",
    "uv_method",
    "uv_align",
    "bridge_cuts",
    "bridge_stitch",
    "close_free_side",
    "stitch_depth",
    "stitch_snap",
    # профиль
    "curb_width",
    "curb_height",
    "profile_mode",
    "parts_mode",
    "flip_direction",
    "flip_normals",
    "up_source",
    "smooth_radius",
    # UV
    "uv_scale_u",
    "uv_y_size",
    "uv_y_offset",
    "uv_orientation",
    "uv_flip_u",
    "uv_flip_v",
    "uv_offset_u",
    "uv_offset_v",
    "uv_fit_part",
    # прочее
    "material_name",
    "merge_distance",
)

_BASE_PRESET = {
    "build_mode": 'CURB',
    "bridge_pairing": 'PROJECT',
    "uv_method": 'SIDES',
    "uv_align": 'SIDE_A',
    "bridge_cuts": 0,
    "bridge_stitch": True,
    "close_free_side": False,
    "stitch_depth": 4,
    "stitch_snap": 0.01,
    "curb_width": 0.2,
    "curb_height": 0.2,
    "profile_mode": 'BOTTOM_INNER',
    "parts_mode": 'BOTH',
    "flip_direction": False,
    "flip_normals": True,
    "up_source": 'WORLD_UP',
    "smooth_radius": 2,
    "uv_scale_u": 1.0,
    "uv_y_size": 0.34,
    "uv_y_offset": 0.0,
    "uv_orientation": 'VERTICAL',
    "uv_flip_u": False,
    "uv_flip_v": False,
    "uv_offset_u": 0.0,
    "uv_offset_v": 0.0,
    "uv_fit_part": False,
    "material_name": "Curb_Mat",
    "merge_distance": 0.001,
}


def _make_preset(**overrides):
    data = dict(_BASE_PRESET)
    data.update(overrides)
    return data


# Заводские пресеты — создаются при первом запуске (и по кнопке Restore Factory)
FACTORY_PRESETS = {
    "Border 1": _make_preset(uv_y_size=0.34, uv_y_offset=0.0),
    "Border 2": _make_preset(uv_y_size=0.34, uv_y_offset=-0.67),
    "Border 3": _make_preset(uv_y_size=0.34, uv_y_offset=-0.34),
    "Border 1 (UV Horizontal)": _make_preset(uv_orientation='HORIZONTAL'),
    "Wall Only": _make_preset(parts_mode='WALL', curb_width=0.0, uv_fit_part=True),
    "Shelf Only": _make_preset(parts_mode='SHELF', curb_height=0.0, uv_fit_part=True),
    "Road Fill (Bridge)": _make_preset(build_mode='BRIDGE', uv_scale_u=1.0,
                                       uv_y_offset=0.0, uv_align='SIDE_A'),
}

# Кэш пресетов в памяти (None = ещё не читали с диска)
_PRESETS = None

# Кэш items для динамического EnumProperty.
# ВАЖНО: Blender требует, чтобы Python держал ссылку на строки items,
# иначе возможны крэши/мусор в UI.
_PRESET_ENUM_CACHE = []

# Счётчик подавления живого превью (чтобы применение пресета не пересобирало
# меш на каждом из ~19 присваиваний)
_SUSPEND = 0

# Защита от рекурсии в depsgraph-хендлере
_IN_HANDLER = False


def preset_dir():
    """Папка конфигов пользователя (переживает перезапуск Blender)"""
    try:
        return bpy.utils.user_resource('CONFIG', path=PRESET_DIRNAME, create=True)
    except TypeError:
        # старые сборки: позиционный аргумент
        return bpy.utils.user_resource('CONFIG', PRESET_DIRNAME, True)


def preset_path():
    return os.path.join(preset_dir(), PRESET_FILENAME)


def _sanitize_presets(raw):
    """Оставляем только словари с известными ключами"""
    clean = {}
    if not isinstance(raw, dict):
        return clean
    for name, values in raw.items():
        if not isinstance(name, str) or not isinstance(values, dict):
            continue
        clean[name] = {k: v for k, v in values.items() if k in PRESET_KEYS}
    return clean


def load_presets(force=False):
    """Читает пресеты с диска (один раз, дальше из кэша)"""
    global _PRESETS
    if _PRESETS is not None and not force:
        return _PRESETS

    data = {}
    path = preset_path()
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = _sanitize_presets(raw.get("presets", raw))
        except Exception as e:
            print(f"[Clean G-Curb] Не удалось прочитать пресеты ({path}): {e}")

    if not data and not os.path.isfile(path):
        # первый запуск — раскладываем заводские
        data = {name: dict(values) for name, values in FACTORY_PRESETS.items()}
        _PRESETS = data
        save_presets()
        return _PRESETS

    _PRESETS = data
    return _PRESETS


def save_presets():
    """Пишет пресеты на диск"""
    path = preset_path()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(
                {"version": PRESET_FILE_VERSION, "presets": _PRESETS or {}},
                f,
                indent=2,
                ensure_ascii=False,
            )
        return True
    except Exception as e:
        print(f"[Clean G-Curb] Не удалось сохранить пресеты ({path}): {e}")
        return False


def capture_preset_values(settings):
    """Снимок всех параметров панели"""
    values = {}
    for key in PRESET_KEYS:
        try:
            values[key] = getattr(settings, key)
        except AttributeError:
            continue
    return values


def apply_preset_values(settings, values):
    """
    Применяет пресет ко всем параметрам сразу.
    Живое превью на время применения подавляется — пересобираем меш один раз.
    """
    global _SUSPEND
    _SUSPEND += 1
    try:
        for key in PRESET_KEYS:
            if key not in values:
                continue
            try:
                setattr(settings, key, values[key])
            except Exception as e:
                # например, устаревший идентификатор enum в старом пресете
                print(f"[Clean G-Curb] Пропущен параметр '{key}': {e}")
    finally:
        _SUSPEND -= 1


def set_active_preset_silent(settings, name):
    """Меняет выбранный пресет в дропдауне, не запуская повторное применение"""
    global _SUSPEND
    _SUSPEND += 1
    try:
        try:
            settings.active_preset = name
        except TypeError:
            settings.active_preset = NO_PRESET
        settings.active_preset_name = "" if name == NO_PRESET else name
    finally:
        _SUSPEND -= 1


# ------------------------------------------------------------------------
# ВИЗУАЛЬНЫЙ КАТАЛОГ — превью формы профиля и текстуры на ней
# ------------------------------------------------------------------------

ICON_SIZE = 128        # размер превью в галерее
ICON_SMALL = 32        # размер иконки в дропдауне
CURRENT_ICON_NAME = "__current__"

# Векторный стиль: плоская заливка + тёмная окантовка, фон прозрачный.
# Текстуру на иконку НЕ натягиваем — только форма профиля.
FILL_SHELF = (0.87, 0.87, 0.89)     # горизонтальная полка
FILL_WALL = (0.60, 0.60, 0.65)      # вертикальная стенка — тоном темнее
OUTLINE_COLOR = (0.09, 0.09, 0.10)
OUTLINE_PX = 2.0                    # толщина окантовки в пикселях
STROKE_FRAC = 0.085                 # полутолщина профиля как доля от иконки

# Иконка зависит ТОЛЬКО от формы — UV, материал и bake на неё не влияют
SHAPE_KEYS = ("curb_width", "curb_height", "profile_mode", "parts_mode", "flip_direction")

# След бордюра сверху: полигоны, под которыми исходную геометрию надо удалить,
# иначе бордюр ложится на существующие грани вторым слоем.
_footprint = []

# Куда заливка воткнула новые вершины на исходных рёбрах:
# список (индекс вершины A, индекс вершины B, доля вдоль ребра).
# Заполняется при построении, используется при Bake.
_stitch_points = []

# След бордюра сверху: список полигонов (список точек), под которыми
# исходную геометрию надо удалить — иначе бордюр ложится на грани вторым слоем.

_pcoll = None          # bpy.utils.previews коллекция
_icon_cache = {}       # имя пресета -> icon_id
_state = {"current_key": None}


def previews_available():
    return np is not None and _pcoll is not None


def _merged(values):
    """Пресет + дефолты (старые пресеты могут не знать новых ключей)"""
    data = dict(_BASE_PRESET)
    data.update(values or {})
    return data


def _profile_points_2d(values):
    """Сечение профиля в 2D: ((shelf_a, shelf_b), (wall_a, wall_b))"""
    w = float(values["curb_width"])
    h = float(values["curb_height"])
    mode = values["profile_mode"]

    side = -1.0 if 'INNER' in mode else 1.0
    if values["flip_direction"]:
        side = -side

    p0 = (0.0, 0.0)
    if mode.startswith('BOTTOM'):
        p1 = (side * w, 0.0)
        p2 = (side * w, -h)
    elif mode.startswith('TOP'):
        p1 = (0.0, h)
        p2 = (side * w, h)
    elif mode.startswith('DOWN'):
        p1 = (0.0, -h)
        p2 = (side * w, -h)
    else:  # UP_*
        p1 = (side * w, 0.0)
        p2 = (side * w, h)

    # p1 — общий угол профиля Г
    if mode.startswith('BOTTOM') or mode.startswith('UP'):
        return (p0, p1), (p1, p2), p1   # сперва полка, потом стенка
    return (p1, p2), (p0, p1), p1       # сперва стенка, потом полка


def _render_icon_pixels(values, size=ICON_SIZE):
    """
    Векторный силуэт сечения бордюра: плоская заливка + окантовка, фон прозрачный.
    Возвращает (size, size, 4) float RGBA.
    """
    values = _merged(values)

    # фон прозрачный, но RGB = цвет окантовки, чтобы сглаженный край
    # не давал чёрной каймы на светлой теме
    canvas = np.empty((size, size, 4), dtype=np.float32)
    canvas[:, :, 0] = OUTLINE_COLOR[0]
    canvas[:, :, 1] = OUTLINE_COLOR[1]
    canvas[:, :, 2] = OUTLINE_COLOR[2]
    canvas[:, :, 3] = 0.0

    rects = (_bridge_icon_rects(size) if values["build_mode"] == 'BRIDGE'
             else _curb_icon_rects(values, size))
    if not rects:
        return canvas

    # ряд 0 = низ (как в буфере превью Blender'а), +Y профиля смотрит вверх
    yy, xx = np.mgrid[0:size, 0:size]
    px = xx.astype(np.float32) + 0.5
    py = yy.astype(np.float32) + 0.5

    def rect_cover(x0, x1, y0, y1, grow=0.0):
        """Покрытие осевого прямоугольника с мягким краем в 1 пиксель"""
        cx_ = np.clip(np.minimum(px - (x0 - grow), (x1 + grow) - px) + 0.5, 0.0, 1.0)
        cy_ = np.clip(np.minimum(py - (y0 - grow), (y1 + grow) - py) + 0.5, 0.0, 1.0)
        return cx_ * cy_

    # 1) окантовка — объединение раздутых прямоугольников
    outline = np.zeros((size, size), dtype=np.float32)
    for (x0, x1, y0, y1, _) in rects:
        np.maximum(outline, rect_cover(x0, x1, y0, y1, grow=OUTLINE_PX), out=outline)
    canvas[..., 3] = outline

    # 2) заливка поверх
    for (x0, x1, y0, y1, color) in rects:
        cover = rect_cover(x0, x1, y0, y1)
        if not cover.any():
            continue
        c = cover[..., None]
        canvas[..., :3] = canvas[..., :3] * (1.0 - c) + np.array(color, np.float32) * c
        np.maximum(canvas[..., 3], cover, out=canvas[..., 3])

    return canvas


def _bridge_icon_rects(size):
    """Иконка Bridge Fill: горизонтальная полоса заливки между двумя рёбрами"""
    mx = size * 0.10          # по бокам поля поменьше — читается как дорога
    y0 = size * 0.30
    y1 = size * 0.70
    bar = max(2.0, size * 0.055)
    return [
        (mx, size - mx, y0 + bar, y1 - bar, FILL_SHELF),   # сама заливка
        (mx, size - mx, y0, y0 + bar, FILL_WALL),          # выделенное ребро снизу
        (mx, size - mx, y1 - bar, y1, FILL_WALL),          # и сверху
    ]


def _curb_icon_rects(values, size):
    """Прямоугольники силуэта Г-профиля в пикселях иконки"""
    shelf, wall, joint = _profile_points_2d(values)

    parts = values["parts_mode"]
    segs = []
    if parts in ('BOTH', 'SHELF'):
        segs.append((shelf, FILL_SHELF))
    if parts in ('BOTH', 'WALL'):
        segs.append((wall, FILL_WALL))

    # выкидываем вырожденные сегменты (width=0 или height=0)
    segs = [s for s in segs
            if abs(s[0][1][0] - s[0][0][0]) > 1e-7 or abs(s[0][1][1] - s[0][0][1]) > 1e-7]
    if not segs:
        return []

    pts = [p for (seg, _) in segs for p in seg]
    min_x = min(p[0] for p in pts)
    max_x = max(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    max_y = max(p[1] for p in pts)
    extent = max(max_x - min_x, max_y - min_y, 1e-6)

    half = size * STROKE_FRAC
    usable = size - 2.0 * (half + OUTLINE_PX + size * 0.04)
    scale = usable / extent
    cx = (min_x + max_x) * 0.5
    cy = (min_y + max_y) * 0.5

    def to_px(p):
        return (size * 0.5 + (p[0] - cx) * scale,
                size * 0.5 + (p[1] - cy) * scale)

    # Профиль всегда осевой: полка горизонтальна, стенка вертикальна.
    # Строим прямоугольники — получаем чёткие прямые срезы вместо круглых торцов.
    rects = []
    for seg, color in segs:
        (ax, ay) = to_px(seg[0])
        (bx, by) = to_px(seg[1])
        x0, x1 = min(ax, bx), max(ax, bx)
        y0, y1 = min(ay, by), max(ay, by)
        if (x1 - x0) >= (y1 - y0):      # горизонтальная полка
            y0, y1 = (ay + by) * 0.5 - half, (ay + by) * 0.5 + half
        else:                            # вертикальная стенка
            x0, x1 = (ax + bx) * 0.5 - half, (ax + bx) * 0.5 + half
        rects.append((x0, x1, y0, y1, color))

    # угол Г: квадрат в общей точке профиля, иначе на стыке остаётся вырез
    if len(rects) == 2:
        jx, jy = to_px(joint)
        rects.insert(1, (jx - half, jx + half, jy - half, jy + half, FILL_SHELF))

    return rects


def _ensure_pcoll():
    global _pcoll
    if _pcoll is None and np is not None:
        try:
            _pcoll = bpy.utils.previews.new()
        except Exception as e:
            print(f"[Clean G-Curb] Превью недоступны: {e}")
    return _pcoll


def _make_preview(key, values):
    """Генерирует превью и возвращает icon_id (0 = без иконки)"""
    pcoll = _ensure_pcoll()
    if pcoll is None or np is None:
        return 0
    try:
        pv = pcoll.get(key)
        if pv is None:
            pv = pcoll.new(key)

        canvas = _render_icon_pixels(values)
        flat = canvas.reshape(-1)
        pv.image_size = (ICON_SIZE, ICON_SIZE)
        try:
            pv.image_pixels_float.foreach_set(flat)
        except TypeError:
            pv.image_pixels_float = flat.tolist()

        step = ICON_SIZE // ICON_SMALL
        small = canvas.reshape(ICON_SMALL, step, ICON_SMALL, step, 4).mean(axis=(1, 3))
        pv.icon_size = (ICON_SMALL, ICON_SMALL)
        try:
            pv.icon_pixels_float.foreach_set(small.reshape(-1))
        except TypeError:
            pv.icon_pixels_float = small.reshape(-1).tolist()

        return pv.icon_id
    except Exception as e:
        print(f"[Clean G-Curb] Не удалось нарисовать превью «{key}»: {e}")
        return 0


def preset_icon_id(name, values):
    """icon_id пресета, с ленивой генерацией и кэшем"""
    if not previews_available() and _ensure_pcoll() is None:
        return 0
    icon = _icon_cache.get(name)
    if icon is None:
        icon = _make_preview(name, values)
        _icon_cache[name] = icon
    return icon


def current_settings_icon_id(settings):
    """Живое превью текущих настроек (перерисовывается только при изменении формы)"""
    if _ensure_pcoll() is None:
        return 0
    key = tuple(str(getattr(settings, k, "")) for k in SHAPE_KEYS)
    if _state["current_key"] != key or CURRENT_ICON_NAME not in _icon_cache:
        _icon_cache[CURRENT_ICON_NAME] = _make_preview(
            CURRENT_ICON_NAME, capture_preset_values(settings)
        )
        _state["current_key"] = key
    return _icon_cache[CURRENT_ICON_NAME]


def invalidate_previews():
    """Сбрасывает кэш превью — после любой правки списка пресетов"""
    global _PRESET_ENUM_CACHE
    if _pcoll is not None:
        _pcoll.clear()
    _icon_cache.clear()
    _state["current_key"] = None
    _PRESET_ENUM_CACHE = []


def preset_enum_items(self, context):
    """Динамический список пресетов для дропдауна и галереи"""
    global _PRESET_ENUM_CACHE
    presets = load_presets()
    items = [(NO_PRESET, "— Не выбран —", "Пресет не применён", 0, 0)]
    for i, name in enumerate(sorted(presets.keys(), key=lambda s: s.lower())):
        icon = preset_icon_id(name, presets[name])
        items.append((name, name, f"Применить пресет «{name}»", icon, i + 1))
    _PRESET_ENUM_CACHE = items
    return _PRESET_ENUM_CACHE


# ------------------------------------------------------------------------
# УТИЛИТЫ
# ------------------------------------------------------------------------

def build_paths_from_edges(edges):
    """
    Разбивает набор рёбер на последовательности вершин (пути).
    Возвращает список (indices, is_closed).
    """
    vert_edges = {}
    edge_lookup = set()
    for e in edges:
        v0, v1 = e.verts
        edge_lookup.add(frozenset((v0.index, v1.index)))
        for v in e.verts:
            vert_edges.setdefault(v.index, []).append(e)

    unvisited = set(edges)
    paths = []

    while unvisited:
        e_start = unvisited.pop()
        v1, v2 = e_start.verts
        path_verts = [v1, v2]
        is_closed = False

        # вперёд от v2
        v_curr = v2
        e_prev = e_start
        while True:
            candidates = [
                e for e in vert_edges[v_curr.index]
                if e in unvisited and e is not e_prev
            ]
            if not candidates:
                break
            e_next = candidates[0]
            unvisited.remove(e_next)
            v_next = e_next.other_vert(v_curr)
            # Замкнулся на стартовую вершину — не дублируем её
            if v_next is path_verts[0]:
                is_closed = True
                break
            path_verts.append(v_next)
            e_prev = e_next
            v_curr = v_next

        # назад от v1 (только если путь ещё не замкнулся)
        prefix = []
        if not is_closed:
            v_curr = v1
            e_prev = e_start
            while True:
                candidates = [
                    e for e in vert_edges[v_curr.index]
                    if e in unvisited and e is not e_prev
                ]
                if not candidates:
                    break
                e_next = candidates[0]
                unvisited.remove(e_next)
                v_next = e_next.other_vert(v_curr)
                if v_next is path_verts[-1]:
                    is_closed = True
                    break
                prefix.append(v_next)
                e_prev = e_next
                v_curr = v_next

        prefix.reverse()
        full_path = prefix + path_verts

        idxs = [v.index for v in full_path]

        paths.append((idxs, is_closed))

    return paths


def assign_material(obj, mat_name):
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(mat_name)
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def cleanup_preview_object():
    """Удаляет preview объект и его mesh"""
    old = bpy.data.objects.get("Curb_Preview")
    if old:
        bpy.data.objects.remove(old, do_unlink=True)

    mesh = bpy.data.meshes.get("Curb_PreviewMesh")
    if mesh and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def get_selected_edges_hash(context):
    """Возвращает хеш выделенных рёбер для отслеживания изменений"""
    src_obj = context.active_object
    if src_obj is None or src_obj.type != 'MESH' or src_obj.mode != 'EDIT':
        return None

    bm_src = bmesh.from_edit_mesh(src_obj.data)
    sel_edges = tuple(sorted((e.verts[0].index, e.verts[1].index) for e in bm_src.edges if e.select))
    return hash(sel_edges) if sel_edges else None


# ------------------------------------------------------------------------
# НАСТРОЙКИ
# ------------------------------------------------------------------------

class CurbSettings(PropertyGroup):

    def _update_preview(self, context):
        if context is None or _SUSPEND:
            return
        if not self.live_preview:
            return
        try:
            # Обновляем превью только если мы в режиме Edit и есть выделенные рёбра
            if context.active_object and context.active_object.mode == 'EDIT':
                build_curb_preview(context)
            else:
                cleanup_preview_object()
        except Exception as e:
            print(f"Preview update error: {e}")

    def _update_active_preset(self, context):
        if _SUSPEND:
            return
        name = self.active_preset
        if name == NO_PRESET:
            self.active_preset_name = ""
            return

        values = load_presets().get(name)
        if values is None:
            # пресет удалили из-под нас
            set_active_preset_silent(self, NO_PRESET)
            return

        apply_preset_values(self, values)
        self.active_preset_name = name

        # один пересбор превью на весь пресет
        if context and self.live_preview:
            try:
                if context.active_object and context.active_object.mode == 'EDIT':
                    build_curb_preview(context)
            except Exception as e:
                print(f"Preview update error: {e}")

    # ---- пресеты ----
    active_preset: EnumProperty(
        name="Preset",
        description="Пресет со всеми параметрами: профиль, части, UV, материал",
        items=preset_enum_items,
        update=_update_active_preset,
    )

    active_preset_name: StringProperty(
        name="Active Preset Name",
        description="Имя выбранного пресета (для восстановления после перезагрузки файла)",
        default="",
        options={'HIDDEN'},
    )

    live_preview: BoolProperty(
        name="Live Preview",
        description="Пересобирать превью автоматически при изменении параметров и выделения",
        default=True,
    )

    catalog_scale: FloatProperty(
        name="Icon Size",
        description="Размер карточек в визуальном каталоге пресетов",
        default=7.0,
        min=2.0,
        max=16.0,
    )

    # ---- режим построения ----
    build_mode: EnumProperty(
        name="Mode",
        description="Что строим по выделенным рёбрам",
        items=[
            ('CURB', "Г-Curb", "Бордюр буквой Г вдоль одной цепочки рёбер"),
            ('BRIDGE', "Bridge Fill",
             "Заливка между ДВУМЯ цепочками рёбер. Ширина берётся из самой "
             "геометрии, UV тайлится по реальным размерам, а не растягивается"),
        ],
        default='CURB',
        update=_update_preview,
    )

    bridge_pairing: EnumProperty(
        name="Ribs",
        description="Как соединять две стороны при заливке",
        items=[
            ('PROJECT', "Nearest",
             "Перемычки по кратчайшему расстоянию: на изгибе рёбра идут поперёк "
             "дороги, вершины обеих сторон сохраняются, недостающие добавляются"),
            ('LENGTH', "Proportional",
             "По доле длины: 1:1 для сторон с одинаковой разбивкой, "
             "но на изгибе перемычки уходят вкось"),
        ],
        default='PROJECT',
        update=_update_preview,
    )

    uv_method: EnumProperty(
        name="UV",
        description="Как разложить заливку",
        items=[
            ('SIDES', "Sides (Tiles)",
             "Каждая сторона раскатывается в свою настоящую длину, перемычки "
             "наклоняются. Плитка ложится ровно вдоль обоих бордюров — "
             "методика для тротуарной плитки"),
            ('PLANAR', "Planar",
             "Проекция на плоскость: форма как сверху, искажений нет, но "
             "плитка не поворачивается вслед за дорогой"),
        ],
        default='SIDES',
        update=_update_preview,
    )

    uv_align: EnumProperty(
        name="Align",
        description=(
            "Какая сторона в развёртке становится идеально прямой. "
            "Перемычки лягут строго поперёк неё, вторая сторона сохранит "
            "свою настоящую ширину"
        ),
        items=[
            ('SIDE_A', "Side A", "Вдоль первой выделенной стороны"),
            ('SIDE_B', "Side B", "Вдоль второй выделенной стороны"),
        ],
        default='SIDE_A',
        update=_update_preview,
    )

    bridge_cuts: IntProperty(
        name="Center Lines",
        description=(
            "Сколько дополнительных линий провести вдоль дороги между краями. "
            "0 — только края. Мельче грани — меньше видно перекос текстуры на "
            "повороте, плюс появляется осевая линия под разметку"
        ),
        default=0,
        min=0,
        max=32,
        update=_update_preview,
    )

    close_free_side: BoolProperty(
        name="Cut & Weld Under Curb",
        description=(
            "На Bake удалить грани, оказавшиеся ПОД бордюром (иначе он ложится "
            "вторым слоем), и зашить свободную сторону бордюра в получившуюся "
            "дыру. Иначе это приходится делать руками"
        ),
        default=False,
    )

    bridge_stitch: BoolProperty(
        name="Stitch To Geometry",
        description=(
            "При Bake вшивать заливку в соседнюю объёмную геометрию: там, где "
            "заливка поставила вершину внутри исходного ребра, разрез "
            "протягивается поперёк соседних квадов. Иначе останется T-junction "
            "и сломается плавность"
        ),
        default=True,
    )

    stitch_depth: IntProperty(
        name="Stitch Depth",
        description="Сколько граней вглубь протягивать разрез от края заливки",
        default=4,
        min=1,
        max=64,
    )

    stitch_snap: FloatProperty(
        name="Snap To Verts",
        description=(
            "Если вершина заливки села ближе этого расстояния к существующей "
            "вершине цепочки — использовать существующую. Так рядом со старым "
            "ребром не появляется второе почти вплотную"
        ),
        default=0.01,
        min=0.0,
        subtype='DISTANCE',
        update=_update_preview,
    )

    status_text: StringProperty(
        name="Status",
        description="Подсказка от построителя",
        default="",
        options={'HIDDEN'},
    )

    # ---- профиль ----
    curb_width: FloatProperty(
        name="Width",
        description="Ширина бордюра (горизонтальная полка). При Parts=Wall работает как отступ стенки от ребра",
        default=0.2,
        min=0.0,
        update=_update_preview,
    )

    curb_height: FloatProperty(
        name="Height",
        description="Высота бордюра (вертикальная стенка). При Parts=Shelf работает как подъём полки",
        default=0.2,
        min=0.0,
        update=_update_preview,
    )

    profile_mode: EnumProperty(
        name="Profile",
        description="Тип профиля Г (8 вариантов)",
        items=[
            # Внутрь (INNER) - сторона внутрь от нормали
            ('BOTTOM_INNER', "In ⟶ Dn", "Внутрь → Вниз"),
            ('TOP_INNER', "Up ⟶ In", "Вверх → Внутрь"),
            ('DOWN_INNER', "Dn ⟶ In", "Вниз → Внутрь"),
            ('UP_INNER', "In ⟶ Up", "Внутрь → Вверх"),
            # Наружу (OUTER) - сторона наружу от нормали
            ('BOTTOM_OUTER', "Out ⟶ Dn", "Наружу → Вниз"),
            ('TOP_OUTER', "Up ⟶ Out", "Вверх → Наружу"),
            ('DOWN_OUTER', "Dn ⟶ Out", "Вниз → Наружу"),
            ('UP_OUTER', "Out ⟶ Up", "Наружу → Вверх"),
        ],
        default='BOTTOM_INNER',
        update=_update_preview,
    )

    parts_mode: EnumProperty(
        name="Parts",
        description="Какие части профиля строить",
        items=[
            ('BOTH', "Full Г", "Полка + стенка (полный профиль)"),
            ('SHELF', "Shelf", "Только горизонтальная полка (width)"),
            ('WALL', "Wall", "Только одна боковая стенка (height)"),
        ],
        default='BOTH',
        update=_update_preview,
    )

    flip_direction: BoolProperty(
        name="Flip Direction",
        description="Инвертировать направление бордюра (отзеркалить относительно нормали)",
        default=False,
        update=_update_preview,
    )

    flip_normals: BoolProperty(
        name="Flip Normals",
        description="Перевернуть нормали граней (если геометрия внутри наружу)",
        default=True,
        update=_update_preview,
    )

    up_source: EnumProperty(
        name="Up Vector",
        description=(
            "Источник вертикали для построения профиля. "
            "World Up — стабильно для плоских дорог. "
            "Vertex Normal — старое поведение, наклоняется на изломах. "
            "Smooth — сглаживание нормалей вершин вдоль пути."
        ),
        items=[
            ('WORLD_UP', "World Up (+Z)", "Использовать мировой Up (Z) — без наклона на изломах"),
            ('VERTEX_NORMAL', "Vertex Normal", "Использовать нормаль вершины (legacy, может наклоняться)"),
            ('SMOOTH', "Smooth Path", "Сглаженная нормаль по соседним вершинам (radius=2)"),
        ],
        default='WORLD_UP',
        update=_update_preview,
    )

    smooth_radius: IntProperty(
        name="Smooth Radius",
        description="Радиус сглаживания нормалей (число соседних вершин с каждой стороны)",
        default=2,
        min=1,
        max=10,
        update=_update_preview,
    )

    # ---- UV ----
    uv_orientation: EnumProperty(
        name="UV Orientation",
        description=(
            "Разворот UV. Vertical — полоса профиля идёт по V, длина бордюра по U. "
            "Horizontal — полоса профиля по U, длина бордюра по V (поворот на 90°)"
        ),
        items=[
            ('VERTICAL', "Vertical", "Длина по U, профиль по V (по умолчанию)"),
            ('HORIZONTAL', "Horizontal", "Длина по V, профиль по U (поворот на 90°)"),
        ],
        default='VERTICAL',
        update=_update_preview,
    )

    uv_scale_u: FloatProperty(
        name="UV Length Scale",
        description="Масштаб UV вдоль бордюра (по длине пути)",
        default=1.0,
        update=_update_preview,
    )

    uv_y_size: FloatProperty(
        name="UV Band Size",
        description="Толщина полосы профиля в атласе (width+height укладываются в неё)",
        default=0.34,
        update=_update_preview,
    )

    uv_y_offset: FloatProperty(
        name="UV Band Offset",
        description="Смещение полосы профиля в атласе",
        default=0.0,
        update=_update_preview,
    )

    uv_flip_u: BoolProperty(
        name="Flip U",
        description="Зеркалить UV по U",
        default=False,
        update=_update_preview,
    )

    uv_flip_v: BoolProperty(
        name="Flip V",
        description="Зеркалить UV по V",
        default=False,
        update=_update_preview,
    )

    uv_offset_u: FloatProperty(
        name="UV Offset U",
        description="Финальное смещение всей развёртки по U",
        default=0.0,
        update=_update_preview,
    )

    uv_offset_v: FloatProperty(
        name="UV Offset V",
        description="Финальное смещение всей развёртки по V",
        default=0.0,
        update=_update_preview,
    )

    uv_fit_part: BoolProperty(
        name="Fit UV To Part",
        description=(
            "Когда строится только полка или только стенка — растянуть её на всю полосу. "
            "Выключено: часть сохраняет своё место внутри полосы полного профиля Г"
        ),
        default=False,
        update=_update_preview,
    )

    # ---- Bake ----
    merge_distance: FloatProperty(
        name="Merge Distance",
        description="Радиус Merge by Distance при Bake",
        default=0.001,
        min=0.001,
    )

    material_name: StringProperty(
        name="Material",
        description="Имя материала для бордюра",
        default="Curb_Mat",
        update=_update_preview,
    )

    # ---- Internal tracking ----
    last_edge_hash: StringProperty(
        name="Last Edge Hash",
        description="Хеш последних выделенных рёбер",
        default="",
        options={'HIDDEN'},
    )


# ------------------------------------------------------------------------
# ПОСТРОЕНИЕ ПРЕВЬЮ
# ------------------------------------------------------------------------

def _try_face(bm, verts):
    """Safety net: пропускаем дубликаты вместо падения (Blender 5.0 bmesh)"""
    if len(set(verts)) < len(verts):
        return None
    try:
        return bm.faces.new(verts)
    except ValueError:
        return None


def _opposite_corners(face, v, va, vb):
    """
    Для квада, в который воткнули вершину v на ребре (va, vb), возвращает
    противоположное ребро и его углы (d рядом с va, c рядом с vb).
    """
    others = [x for x in face.verts if x is not v and x is not va and x is not vb]
    if len(others) != 2 or len(face.verts) != 5:
        return None, None, None

    opp = None
    for e in face.edges:
        if set(e.verts) == set(others):
            opp = e
            break
    if opp is None:
        return None, None, None

    d = None
    for o in others:
        for e in face.edges:
            if set(e.verts) == {va, o}:
                d = o
                break
        if d is not None:
            break
    if d is None:
        return None, None, None

    c = others[1] if d is others[0] else others[0]
    return opp, d, c


def _propagate_cut(face, v, va, vb, factor, max_depth):
    """
    Протягивает разрез поперёк полосы квадов — как loop cut, но от
    произвольной точки. Без этого на соседней грани остался бы T-junction.
    """
    made = 0
    depth = 0
    while depth < max_depth and face is not None:
        opp, d, c = _opposite_corners(face, v, va, vb)
        if opp is None:
            break

        next_face = next((f for f in opp.link_faces if f is not face), None)

        try:
            _, v2 = bmesh.utils.edge_split(opp, d, factor)
            bmesh.utils.face_split(face, v, v2)
        except (ValueError, RuntimeError, TypeError):
            break

        made += 1
        if next_face is None or len(next_face.verts) != 5:
            break

        face, v, va, vb = next_face, v2, d, c
        depth += 1

    return made


def _stitch_source_mesh(bm_src, entries, max_depth):
    """
    Вставляет в исходную геометрию рёбра там, где заливка поставила свои
    вершины. Форма не трогается: вершина садится ровно на существующее ребро,
    поэтому силуэт и плавность остаются прежними.
    """
    bm_src.verts.ensure_lookup_table()
    n_verts = len(bm_src.verts)

    # Разрезы двигают таблицу индексов, поэтому вершины разрешаем в объекты
    # ЗАРАНЕЕ — ссылки переживают edge_split, индексы нет.
    todo = []
    for ia, ib, factor in entries:
        if ia >= n_verts or ib >= n_verts:
            continue
        if not (1e-6 < factor < 1.0 - 1e-6):
            continue
        todo.append((bm_src.verts[ia], bm_src.verts[ib], factor))

    cuts = 0
    spread = 0

    for va, vb, factor in todo:
        if not (va.is_valid and vb.is_valid):
            continue
        edge = next((e for e in va.link_edges if e.other_vert(va) is vb), None)
        if edge is None:
            continue

        starts = [(f, factor) for f in edge.link_faces]
        try:
            _, new_v = bmesh.utils.edge_split(edge, va, factor)
        except (ValueError, RuntimeError, TypeError):
            continue
        cuts += 1

        for face, fac in starts:
            if len(face.verts) == 5:
                spread += _propagate_cut(face, new_v, va, vb, fac, max_depth)

    return cuts, spread


def _lscm_uv(bm, uv_layer, scale, ref_dir, finish):
    """
    Раскладка методом наименьших квадратов (LSCM): невязка размазывается по
    всей полосе, а не сваливается в перемычки. Даёт и точные стороны, и
    точные перемычки настолько, насколько это вообще возможно.
    """
    if np is None:
        return False

    verts = list(bm.verts)
    if len(verts) < 3:
        return False
    idx = {v: i for i, v in enumerate(verts)}
    nv = len(verts)

    tris = []
    for f in bm.faces:
        fv = list(f.verts)
        for k in range(1, len(fv) - 1):
            tris.append((fv[0], fv[k], fv[k + 1]))
    if not tris:
        return False

    rows_i, rows_j, rows_v, rhs = [], [], [], []
    r = 0
    for (a, b, c) in tris:
        e1 = b.co - a.co
        e2 = c.co - a.co
        nrm = e1.cross(e2)
        area2 = nrm.length
        if area2 < 1e-12 or e1.length < 1e-12:
            continue
        X = e1.normalized()
        Z = nrm.normalized()
        Y = Z.cross(X)
        x = (0.0, e1.length, e2.dot(X))
        y = (0.0, 0.0, e2.dot(Y))
        d = (x[1] - x[0]) * (y[2] - y[0]) - (x[2] - x[0]) * (y[1] - y[0])
        if abs(d) < 1e-12:
            continue
        dNdx = ((y[1] - y[2]) / d, (y[2] - y[0]) / d, (y[0] - y[1]) / d)
        dNdy = ((x[2] - x[1]) / d, (x[0] - x[2]) / d, (x[1] - x[0]) / d)
        w = math.sqrt(area2 * 0.5)
        ids = (idx[a], idx[b], idx[c])
        for k in range(3):
            rows_i.append(r);     rows_j.append(ids[k]);      rows_v.append(w * dNdx[k])
            rows_i.append(r);     rows_j.append(nv + ids[k]); rows_v.append(-w * dNdy[k])
            rows_i.append(r + 1); rows_j.append(ids[k]);      rows_v.append(w * dNdy[k])
            rows_i.append(r + 1); rows_j.append(nv + ids[k]); rows_v.append(w * dNdx[k])
        rhs.append(0.0)
        rhs.append(0.0)
        r += 2

    if r < 4:
        return False

    # закрепляем две максимально далёкие вершины, иначе решение вырождено
    pts = np.array([[v.co.x, v.co.y, v.co.z] for v in verts])
    p0 = int(np.argmin(pts[:, 0] + pts[:, 1] + pts[:, 2]))
    p1 = int(np.argmax(np.linalg.norm(pts - pts[p0], axis=1)))
    if p0 == p1:
        return False
    pin_len = float(np.linalg.norm(pts[p1] - pts[p0]))
    pinned = {p0: (0.0, 0.0), p1: (pin_len, 0.0)}

    free_cols = [j for j in range(2 * nv)
                 if (j % nv) not in pinned or (j < nv) is None]
    free_cols = [j for j in range(2 * nv) if (j if j < nv else j - nv) not in pinned]
    col_map = {c: k for k, c in enumerate(free_cols)}

    A = np.zeros((r, len(free_cols)), dtype=np.float64)
    B = np.zeros(r, dtype=np.float64)
    for i, j, val in zip(rows_i, rows_j, rows_v):
        base = j if j < nv else j - nv
        if base in pinned:
            B[i] -= val * (pinned[base][0] if j < nv else pinned[base][1])
        else:
            A[i, col_map[j]] += val

    try:
        sol, *_ = np.linalg.lstsq(A, B, rcond=None)
    except Exception as e:
        print(f"[Clean G-Curb] LSCM не решился: {e}")
        return False

    U = np.zeros(nv); V = np.zeros(nv)
    for j, k in col_map.items():
        if j < nv:
            U[j] = sol[k]
        else:
            V[j - nv] = sol[k]
    for b_, (uu, vv) in pinned.items():
        U[b_] = uu; V[b_] = vv

    # единый масштаб: подгоняем длины рёбер к настоящим
    num = den = 0.0
    for e in bm.edges:
        i0, i1 = idx[e.verts[0]], idx[e.verts[1]]
        d3 = (e.verts[0].co - e.verts[1].co).length
        d2 = math.hypot(U[i0] - U[i1], V[i0] - V[i1])
        num += d3 * d2
        den += d2 * d2
    k_scale = (num / den) if den > 1e-12 else 1.0
    U *= k_scale; V *= k_scale

    # доворот: опорное направление кладём на ось U
    if ref_dir is not None and ref_dir[0] is not None:
        va, vb = ref_dir
        i0, i1 = idx[va], idx[vb]
        du, dv = U[i1] - U[i0], V[i1] - V[i0]
        ang = math.atan2(dv, du)
        ca, sa = math.cos(-ang), math.sin(-ang)
        U, V = U * ca - V * sa, U * sa + V * ca

    for f in bm.faces:
        for l in f.loops:
            i = idx[l.vert]
            l[uv_layer].uv = finish(U[i] * scale, V[i] * scale)
    return True


def _finish_preview(context, settings, bm):
    """Общий финал для обоих режимов: нормали → меш → объект → материал"""
    bm.normal_update()

    # Инвертируем нормали если flip_normals ВЫКЛЮЧЕН
    if not settings.flip_normals:
        for face in bm.faces:
            face.normal_flip()

    # ЗЕРКАЛО UV приводим к одной стороне ЗДЕСЬ — когда намотка граней уже
    # окончательная. Раньше проверка шла до переворота нормалей, поэтому
    # результат зависел от Flip Normals и текстура выглядела отражённой.
    uv_lay = bm.loops.layers.uv.active
    if uv_lay is not None:
        bm.normal_update()
        total = 0.0
        for f in bm.faces:
            pts = [l[uv_lay].uv for l in f.loops]
            a2 = 0.0
            for i in range(len(pts)):
                j = (i + 1) % len(pts)
                a2 += pts[i].x * pts[j].y - pts[j].x * pts[i].y
            total += a2 * (1.0 if f.normal.z >= 0.0 else -1.0)
        if total > 0.0:
            for f in bm.faces:
                for l in f.loops:
                    t = l[uv_lay].uv
                    l[uv_lay].uv = (t.x, -t.y)

        # смещения — в самом конце, после зеркала: тогда они всегда двигают
        # развёртку в ту сторону, которую задал пользователь
        if settings.build_mode == 'BRIDGE':
            hor = settings.uv_orientation == 'HORIZONTAL'
            add_u = settings.uv_offset_u + (settings.uv_y_offset if hor else 0.0)
            add_v = settings.uv_offset_v + (0.0 if hor else settings.uv_y_offset)
            if abs(add_u) > 1e-12 or abs(add_v) > 1e-12:
                for f in bm.faces:
                    for l in f.loops:
                        t = l[uv_lay].uv
                        l[uv_lay].uv = (t.x + add_u, t.y + add_v)

    # Удаляем старый preview ПЕРЕД созданием нового
    cleanup_preview_object()

    mesh = bpy.data.meshes.new("Curb_PreviewMesh")
    bm.to_mesh(mesh)
    bm.free()

    curb_obj = bpy.data.objects.new("Curb_Preview", mesh)
    context.scene.collection.objects.link(curb_obj)

    # Помечаем объект как временный preview
    curb_obj["is_curb_preview"] = True

    assign_material(curb_obj, settings.material_name)


def _polyline_length(pts, closed):
    total = 0.0
    for i in range(1, len(pts)):
        total += (pts[i] - pts[i - 1]).length
    if closed and len(pts) > 2:
        total += (pts[0] - pts[-1]).length
    return total


def _cumulative(pts, closed):
    """Накопленная длина по вершинам (для замкнутой — плюс замыкающий сегмент)"""
    s = [0.0]
    for i in range(1, len(pts)):
        s.append(s[-1] + (pts[i] - pts[i - 1]).length)
    if closed and len(pts) > 2:
        s.append(s[-1] + (pts[0] - pts[-1]).length)
    return s


def _sample_polyline(pts, closed, cum, t):
    """Точка на ломаной по нормализованной длине t ∈ [0, 1]"""
    total = cum[-1]
    if total <= 1e-9:
        return pts[0].copy()

    d = min(max(t, 0.0), 1.0) * total
    lo, hi = 0, len(cum) - 2
    while lo < hi:                       # бинарный поиск сегмента
        mid = (lo + hi + 1) // 2
        if cum[mid] <= d:
            lo = mid
        else:
            hi = mid - 1
    i = lo

    seg = cum[i + 1] - cum[i]
    f = 0.0 if seg <= 1e-12 else (d - cum[i]) / seg
    a = pts[i]
    b = pts[(i + 1) % len(pts)] if closed else pts[min(i + 1, len(pts) - 1)]
    return a.lerp(b, f)


def _closest_param(pts, closed, cum, p):
    """Нормализованный параметр ближайшей точки ломаной к p"""
    total = cum[-1]
    if total <= 1e-9:
        return 0.0

    n_seg = len(cum) - 1
    best_d = float('inf')
    best_t = 0.0

    for i in range(n_seg):
        a = pts[i]
        b = pts[(i + 1) % len(pts)] if closed else pts[min(i + 1, len(pts) - 1)]
        ab = b - a
        len_sq = ab.length_squared
        if len_sq < 1e-12:
            f = 0.0
            q = a
        else:
            f = min(1.0, max(0.0, (p - a).dot(ab) / len_sq))
            q = a + ab * f
        d = (p - q).length_squared
        if d < best_d:
            best_d = d
            best_t = (cum[i] + (cum[i + 1] - cum[i]) * f) / total

    return best_t


def _rib_columns(pts_a, cum_a, pts_b, cum_b, closed):
    """
    Колонки (t_a, t_b) по КРАТЧАЙШЕМУ расстоянию, а не по доле длины.

    На изгибе внутренняя дуга короче внешней, поэтому «одинаковая доля длины»
    даёт косые перемычки и тянет текстуру. Здесь каждая исходная вершина
    получает проекцию на противоположную сторону: старые вершины сохраняются,
    новые добавляются там, где их не хватало.
    """
    total_a = cum_a[-1]
    total_b = cum_b[-1]
    if total_a <= 1e-9 or total_b <= 1e-9:
        return []

    # src: 'A' — колонка от вершины A (t_b получен проекцией), 'B' — наоборот
    cols = [[cum_a[i] / total_a, _closest_param(pts_b, closed, cum_b, p), 'A', False]
            for i, p in enumerate(pts_a)]
    cols += [[_closest_param(pts_a, closed, cum_a, p), cum_b[j] / total_b, 'B', False]
             for j, p in enumerate(pts_b)]

    cols.sort(key=lambda c: (c[0], c[1]))

    if not closed:
        # концы прижимаем к настоящим концам, чтобы полоса не обрезалась
        cols[0][0], cols[0][1] = 0.0, 0.0
        cols[-1][0], cols[-1][1] = 1.0, 1.0

        # монотонность по B: иначе полосу складывает саму на себя
        last_b = 0.0
        for col in cols:
            last_b = max(last_b, min(col[1], 1.0))
            col[1] = last_b

    # На повороте вершина внешней стороны проецируется в УГОЛ внутренней,
    # а вершина внутренней — в СЕРЕДИНУ хорды внешней. Получаются две почти
    # совпавшие колонки, у которых одна сторона стоит на месте. Настоящая
    # перемычка здесь — вершина↔вершина, поэтому такие пары сливаем.
    tol = 1e-3 * max(total_a, total_b)
    eps_a = tol / total_a
    eps_b = tol / total_b

    def _combine(prev, col):
        t_a = col[0] if col[2] == 'A' else (prev[0] if prev[2] == 'A' else prev[0])
        t_b = col[1] if col[2] == 'B' else (prev[1] if prev[2] == 'B' else prev[1])
        both = prev[2] != col[2]
        return [t_a, t_b, prev[2], prev[3] or both]

    merged = [cols[0]]
    for col in cols[1:]:
        prev = merged[-1]
        a_close = abs(col[0] - prev[0]) <= eps_a
        b_close = abs(col[1] - prev[1]) <= eps_b

        if a_close and b_close:
            merged[-1] = _combine(prev, col)          # полный дубль
        elif (a_close or b_close) and prev[2] != col[2] and not prev[3]:
            merged[-1] = _combine(prev, col)          # вершина ↔ вершина
        else:
            merged.append(col)

    return [(c[0], c[1]) for c in merged]


def _locate_on_polyline(pts, closed, cum, t):
    """Как _sample_polyline, но возвращает ещё сегмент и долю внутри него"""
    total = cum[-1]
    if total <= 1e-9:
        return pts[0].copy(), 0, 0.0

    d = min(max(t, 0.0), 1.0) * total
    lo, hi = 0, len(cum) - 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cum[mid] <= d:
            lo = mid
        else:
            hi = mid - 1
    i = lo

    seg = cum[i + 1] - cum[i]
    f = 0.0 if seg <= 1e-12 else (d - cum[i]) / seg
    a = pts[i]
    b = pts[(i + 1) % len(pts)] if closed else pts[min(i + 1, len(pts) - 1)]
    return a.lerp(b, f), i, f


def _snap_columns_to_verts(columns, cum_a, cum_b, snap_dist):
    """
    Колонку, севшую почти на существующую вершину цепочки, подтягиваем
    ровно к ней: не плодим ребро в миллиметре от старого.
    """
    if snap_dist <= 0.0:
        return columns

    def _snap(t, cum):
        total = cum[-1]
        if total <= 1e-9:
            return t
        pos = t * total
        best = min(cum, key=lambda c: abs(c - pos))
        return best / total if abs(best - pos) <= snap_dist else t

    snapped = [(_snap(t_a, cum_a), _snap(t_b, cum_b)) for t_a, t_b in columns]

    out = [snapped[0]]
    for col in snapped[1:]:
        if abs(col[0] - out[-1][0]) < 1e-9 and abs(col[1] - out[-1][1]) < 1e-9:
            continue
        out.append(col)
    return out


def _align_second_path(pts_a, pts_b, closed_b):
    """
    Разворачивает (и для замкнутой — прокручивает) вторую цепочку так,
    чтобы полосу между ними не перекручивало восьмёркой.
    """
    pts_b = list(pts_b)

    if closed_b and len(pts_b) > 2:
        start = min(range(len(pts_b)),
                    key=lambda i: (pts_b[i] - pts_a[0]).length_squared)
        pts_b = pts_b[start:] + pts_b[:start]
        if len(pts_a) > 1 and len(pts_b) > 2:
            probe = pts_a[1]
            if (pts_b[-1] - probe).length_squared < (pts_b[1] - probe).length_squared:
                pts_b = [pts_b[0]] + pts_b[:0:-1]
        return pts_b

    direct = (pts_a[0] - pts_b[0]).length + (pts_a[-1] - pts_b[-1]).length
    crossed = (pts_a[0] - pts_b[-1]).length + (pts_a[-1] - pts_b[0]).length
    if crossed < direct:
        pts_b.reverse()
    return pts_b


def _build_bridge_geometry(bm, uv_layer, settings, bm_src, mw, paths_info):
    """
    Заливка между двумя выделенными цепочками рёбер.
    Ширина берётся из самой геометрии, UV тайлится по реальным размерам.
    Возвращает (успех, сообщение для панели).
    """
    global _stitch_points
    _stitch_points = []

    paths = []
    for idxs, closed in paths_info:
        if len(idxs) < 2:
            continue
        pts = [mw @ bm_src.verts[i].co for i in idxs]
        paths.append((pts, closed, _polyline_length(pts, closed), list(idxs)))

    if len(paths) < 2:
        return False, "Bridge: нужны две отдельные цепочки рёбер"

    note = ""
    if len(paths) > 2:
        paths.sort(key=lambda p: p[2], reverse=True)
        note = f"Bridge: взяты 2 самые длинные цепочки (ещё {len(paths) - 2} пропущено)"
        paths = paths[:2]

    (pts_a, closed_a, _, idxs_a), (pts_b, closed_b, _, idxs_b) = paths

    # вторую цепочку могли развернуть и прокрутить — индексы едут вместе с
    # точками (сверяем по id, разворот не всегда меняет первый элемент)
    order = {id(p): k for k, p in enumerate(pts_b)}
    aligned = _align_second_path(pts_a, pts_b, closed_b)
    idxs_b = [idxs_b[order[id(p)]] for p in aligned]
    pts_b = aligned

    wrap = closed_a and closed_b
    cum_a = _cumulative(pts_a, wrap)
    cum_b = _cumulative(pts_b, wrap)

    # Колонки: либо умные рёбра по кратчайшему расстоянию, либо по доле длины
    heavy = len(pts_a) * len(pts_b)
    if settings.bridge_pairing == 'PROJECT' and heavy <= 400000:
        columns = _rib_columns(pts_a, cum_a, pts_b, cum_b, wrap)
    else:
        if settings.bridge_pairing == 'PROJECT':
            note = "Bridge: цепочки очень плотные — перемычки по доле длины"

        def _params(cum):
            total = cum[-1]
            if total <= 1e-9:
                return [0.0, 1.0]
            return [c / total for c in cum]

        shared = sorted({round(t, 6) for t in _params(cum_a)} |
                        {round(t, 6) for t in _params(cum_b)})
        columns = [(t, t) for t in shared]

    columns = _snap_columns_to_verts(columns, cum_a, cum_b, settings.stitch_snap)

    if wrap:
        columns = [c for c in columns if c[0] < 1.0 - 1e-9]
    if len(columns) < 2:
        return False, "Bridge: цепочки слишком короткие"

    loc_a = [_locate_on_polyline(pts_a, wrap, cum_a, t_a) for t_a, _ in columns]
    loc_b = [_locate_on_polyline(pts_b, wrap, cum_b, t_b) for _, t_b in columns]
    row_a = [p for p, _, _ in loc_a]
    row_b = [p for p, _, _ in loc_b]

    # Точки, севшие ВНУТРЬ исходного ребра, — это будущие стежки:
    # без них у соседней объёмной геометрии останется T-junction.
    for locs, idxs in ((loc_a, idxs_a), (loc_b, idxs_b)):
        for _, seg, f in locs:
            if not (1e-6 < f < 1.0 - 1e-6):
                continue
            if seg >= len(idxs):
                continue
            nxt = idxs[(seg + 1) % len(idxs)]
            _stitch_points.append((idxs[seg], nxt, f))

    # Колонки, совпавшие целиком, выбрасываем
    tol = 1e-4 * max(cum_a[-1], cum_b[-1])
    keep_a, keep_b = [row_a[0]], [row_b[0]]
    for i in range(1, len(row_a)):
        if ((row_a[i] - keep_a[-1]).length < tol and
                (row_b[i] - keep_b[-1]).length < tol):
            continue
        keep_a.append(row_a[i])
        keep_b.append(row_b[i])
    row_a, row_b = keep_a, keep_b

    if len(row_a) < 2:
        return False, "Bridge: цепочки слишком короткие"

    # Направление обхода зависит от того, с какого ребра начали выделять.
    # Приводим его к мировым осям, иначе у соседних кусков дороги развёртка
    # смотрит в разные стороны и текстура выглядит перевёрнутой.
    if not wrap:
        run_dir = (row_a[-1] + row_b[-1]) - (row_a[0] + row_b[0])
        key = (run_dir.x, run_dir.y, run_dir.z)
        flip_run = False
        for c in key:
            if abs(c) > 1e-6:
                flip_run = c < 0.0
                break
        if flip_run:
            row_a.reverse()
            row_b.reverse()

    n = len(row_a)

    # Если на крутом повороте одна сторона стоит на месте (несколько вершин
    # внешней смотрят в один угол внутренней) — вершину переиспользуем, а не
    # плодим совпадающие. Такой кусок станет треугольником, а не вырожденным квадом.
    def _make_row(points):
        verts = []
        for i, p in enumerate(points):
            if i > 0 and (p - points[i - 1]).length < tol:
                verts.append(verts[-1])
            else:
                verts.append(bm.verts.new(p))
        return verts

    # Центральные линии: полосу режем вдоль на несколько рядов. Мельче грани —
    # меньше видно перекос на повороте (в Along Path), плюс дорога получает
    # осевые линии, за которые потом удобно цеплять разметку.
    lanes = settings.bridge_cuts + 1
    rows_pts = []
    for r in range(lanes + 1):
        t = r / lanes
        rows_pts.append([row_a[i].lerp(row_b[i], t) for i in range(n)])

    rows_v = []
    for pts in rows_pts:
        verts = _make_row(pts)
        if wrap and len(verts) > 1 and (pts[0] - pts[-1]).length < tol:
            verts[-1] = verts[0]
        rows_v.append(verts)
    bm.verts.ensure_lookup_table()

    # U — длина по центральной линии, V — реальная ширина в этой колонке.
    # Ничего не нормализуем: текстура тайлится, а не растягивается.
    mids = [(row_a[i] + row_b[i]) * 0.5 for i in range(n)]
    u_at = [0.0] * n
    for i in range(1, n):
        u_at[i] = u_at[i - 1] + (mids[i] - mids[i - 1]).length
    u_wrap = u_at[-1] + (mids[0] - mids[-1]).length
    widths = [(row_b[i] - row_a[i]).length for i in range(n)]

    scale = settings.uv_scale_u
    horizontal = settings.uv_orientation == 'HORIZONTAL'
    flip_u = settings.uv_flip_u
    flip_v = settings.uv_flip_v
    off_u = settings.uv_offset_u
    off_v = settings.uv_offset_v
    v_shift = settings.uv_y_offset

    def _finish_uv(u, v):
        if horizontal:
            u, v = v, u
        if flip_u:
            u = -u
        if flip_v:
            v = -v
        return (u, v)

    # ---- PLANAR: проекция на плоскость дороги ----
    # Полоса на повороте — это сектор кольца, он ПЛОСКИЙ. Значит существует
    # раскладка вообще без искажений: проекция на плоскость. Раскладка «вдоль
    # пути» этого дать не может — между двумя рёбрами лежит трапеция, а не
    # прямоугольник, поэтому плитка на внутреннем радиусе всегда ужимается.
    # ---- Раскладка ----
    # SIDES («методика user'а»): КАЖДАЯ сторона раскатывается в свою настоящую
    # длину, а расхождение между ними уходит в перемычки — они наклоняются.
    # Именно так плитка тротуара ложится правильно вдоль обоих бордюров.
    # Замер эталона: граничные рёбра 89% строго в масштабе, внутренние 59%.
    #
    # PLANAR: проекция на плоскость — форма как сверху, ноль искажений,
    # но плитка не поворачивается вслед за дорогой.
    planar = settings.uv_method == 'PLANAR'

    normal = Vector((0.0, 0.0, 0.0))
    for i in range(n - 1):
        normal += (row_a[i + 1] - row_a[i]).cross(row_b[i] - row_a[i])
    if normal.length_squared < 1e-12:
        normal = Vector((0.0, 0.0, 1.0))
    normal.normalize()
    # у дороги «верх» всегда мировой +Z: иначе нормаль (а с ней и UV)
    # переворачивается в зависимости от того, с какой стороны выделили рёбра
    if normal.z < 0.0:
        normal = -normal

    if planar:
        axis_u = mids[-1] - mids[0] if n > 1 else Vector((1.0, 0.0, 0.0))
        axis_u = axis_u - normal * axis_u.dot(normal)
        if axis_u.length_squared < 1e-12:
            axis_u = normal.orthogonal()
        axis_u.normalize()
        axis_v = normal.cross(axis_u).normalized()
        origin = row_a[0].copy()
    else:
        # ось выпрямления — выбранная сторона. Вдоль берём её длину, поперёк —
        # настоящее смещение вершины. Координата вдоль ОДНА на всю колонку,
        # поэтому перемычка в UV строго горизонтальна, а на повороте между
        # колонками получается разрыв.
        axis_row = row_a if settings.uv_align == 'SIDE_A' else row_b
        axis_len = [0.0] * n
        axis_dir = []
        for i in range(n):
            j = (i + 1) % n
            d = axis_row[j] - axis_row[i]
            if d.length_squared < 1e-12 and i > 0:
                d = axis_dir[i - 1].copy()
            elif d.length_squared < 1e-12:
                d = Vector((1.0, 0.0, 0.0))
            axis_dir.append(d.normalized())
        for i in range(1, n):
            axis_len[i] = axis_len[i - 1] + (axis_row[i] - axis_row[i - 1]).length
        other_row = row_b if settings.uv_align == 'SIDE_A' else row_a
        rib_dir = []
        for i in range(n):
            d = other_row[i] - axis_row[i]
            if d.length_squared < 1e-12:
                d = normal.cross(axis_dir[i])
            rib_dir.append(d.normalized())

        # V берём ИЗ ТОЙ ЖЕ системы координат, что и U (n × t), а не отдельным
        # знаком. Тогда разворот цепочки переворачивает обе оси сразу — это
        # поворот на 180°, а не зеркало, и Flip руками не нужен.
        # ОДИН знак на всю заливку: если считать его на каждый угол отдельно,
        # соседние фейсы получают разную ориентацию и часть острова выходит
        # зеркальной — тогда приходится жать Flip на каждый фейс.
        vote = 0.0
        for i in range(n):
            vote += rib_dir[i].dot(normal.cross(axis_dir[i]))
        sign = 1.0 if vote >= 0.0 else -1.0

    def _uv_corner(vert, seg, col, frac):
        """
        Каждый блок кладётся ЖЁСТКО в своей системе координат: ось U — вдоль
        именно этого куска дороги, ось V — поперёк. Внутри блока это движение
        твёрдого тела, поэтому кирпичи сохраняют размер, а на изгибе между
        блоками получается разрыв — как при ручном довороте UV.
        """
        if planar:
            d = vert.co - origin
            return _finish_uv(d.dot(axis_u) * scale,
                              d.dot(axis_v) * scale)
        # U — из жёсткой системы блока, V — вдоль перемычки этой колонки.
        # Перемычка общая для двух соседних блоков, поэтому линия кирпичей
        # переходит через стык без разрыва, а блок довёрнут по своему изгибу.
        d = vert.co - axis_row[seg]
        across = (vert.co - axis_row[col]).dot(rib_dir[col]) * sign
        return _finish_uv((axis_len[seg] + d.dot(axis_dir[seg])) * scale,
                          across * scale)

    made = 0
    for i in range(n if wrap else n - 1):
        j = (i + 1) % n
        for r in range(lanes):
            top, bot = rows_v[r], rows_v[r + 1]
            f_top = r / lanes
            f_bot = (r + 1) / lanes

            a_i, a_j = top[i], top[j]
            b_i, b_j = bot[i], bot[j]

            # Обход всегда один и тот же (a_i → a_j → b_j → b_i); если сторона
            # стоит на месте, просто выкидываем повторённый угол → треугольник.
            corners = [(a_i, i, i, f_top), (a_j, i, j, f_top),
                       (b_j, i, j, f_bot), (b_i, i, i, f_bot)]
            if a_i is a_j:
                corners.pop(1)
            elif b_i is b_j:
                corners.pop(2)
            if len({c[0] for c in corners}) < 3:
                continue

            face = _try_face(bm, tuple(c[0] for c in corners))
            if face is None:
                continue
            for loop, (vert, sg, cl, frac) in zip(face.loops, corners):
                loop[uv_layer].uv = _uv_corner(vert, sg, cl, frac)
            made += 1

    # ФИНАЛЬНАЯ НОРМАЛИЗАЦИЯ ОРИЕНТАЦИИ.
    # Оси развёртки не трогаем (они заданы настройками), убираем только два
    # артефакта порядка выделения: зеркало и разворот на 180°.
    if not planar and np is not None:
        # опора МИРОВАЯ: направление дороги с канонизированным знаком.
        # Если брать axis_dir, она переворачивается вместе с обходом и
        # проверка ничего не ловит.
        base_p = row_a[0]
        ax1 = (row_a[-1] + row_b[-1]) - (row_a[0] + row_b[0])
        ax1 = ax1 - normal * ax1.dot(normal)
        if ax1.length_squared < 1e-12:
            ax1 = axis_dir[0]
        ax1.normalize()
        for comp in (ax1.x, ax1.y, ax1.z):
            if abs(comp) > 1e-6:
                if comp < 0.0:
                    ax1 = -ax1
                break
        ax2 = normal.cross(ax1)
        W, T = [], []
        for f in bm.faces:
            for l in f.loops:
                d = l.vert.co - base_p
                W.append((d.dot(ax1), d.dot(ax2)))
                t = l[uv_layer].uv
                T.append((t.x, t.y))
        if len(W) >= 3:
            Wm = np.asarray(W, dtype=np.float64)
            Tm = np.asarray(T, dtype=np.float64)
            Wc = Wm - Wm.mean(0)
            Tc = Tm - Tm.mean(0)
            # U должен расти вдоль дороги, V — поперёк, в мировых осях.
            # Два независимых знака: это и убирает переворот при любом
            # порядке выделения.
            # при uv_orientation=Horizontal оси U и V меняются местами,
            # поэтому сначала определяем, какая мировая ось какой соответствует
            c00 = float(np.dot(Wc[:, 0], Tc[:, 0]))
            c01 = float(np.dot(Wc[:, 0], Tc[:, 1]))
            c10 = float(np.dot(Wc[:, 1], Tc[:, 0]))
            c11 = float(np.dot(Wc[:, 1], Tc[:, 1]))
            if abs(c00) + abs(c11) >= abs(c01) + abs(c10):
                flip_u = c00 < 0.0
            else:
                flip_u = c10 < 0.0

            flip_v = False

            if flip_u or flip_v:
                # переворачиваем вокруг заданных смещений, чтобы Offset U/V
                # продолжали двигать развёртку в ту же сторону
                for f in bm.faces:
                    for l in f.loops:
                        t = l[uv_layer].uv
                        l[uv_layer].uv = (-t.x if flip_u else t.x,
                                          -t.y if flip_v else t.y)

    # смещения применяем в самом конце: тогда они всегда двигают развёртку
    # в одну и ту же сторону, независимо от нормализации ориентации
    # v_shift («поперёк») кладём на ту ось, куда его увёл разворот UV
    if made == 0:
        return False, "Bridge: не удалось построить грани"

    return True, note


def _mesh_uv_name(uv_layers):
    """Имя UV-слоя, в который пишем бордюр: активный UV меша (UVChannel_1,
    UVMap…). Join склеивает UV по имени — с другим именем у меша появится
    второй UV-слой, а у бордюра в основном слое будут нули."""
    uv = uv_layers.active
    if uv is None and len(uv_layers):
        uv = uv_layers[0]
    return uv.name if uv is not None else "UVMap"


def build_curb_preview(context):
    settings = context.scene.curb_settings
    src_obj = context.active_object

    if src_obj is None or src_obj.type != 'MESH' or src_obj.mode != 'EDIT':
        cleanup_preview_object()
        return

    bm_src = bmesh.from_edit_mesh(src_obj.data)
    bm_src.verts.ensure_lookup_table()
    bm_src.edges.ensure_lookup_table()

    sel_edges = [e for e in bm_src.edges if e.select]
    if not sel_edges:
        cleanup_preview_object()
        return

    # Обновляем хеш выделения
    new_hash = get_selected_edges_hash(context)
    if new_hash is not None:
        settings.last_edge_hash = str(new_hash)

    paths_info = build_paths_from_edges(sel_edges)
    if not paths_info:
        cleanup_preview_object()
        return

    global _footprint
    _footprint = []

    bm = bmesh.new()
    uv_layer = bm.loops.layers.uv.new(_mesh_uv_name(bm_src.loops.layers.uv))

    mw = src_obj.matrix_world
    n_mat = mw.to_3x3()

    # ---- Bridge Fill: заливка между двумя цепочками рёбер ----
    if settings.build_mode == 'BRIDGE':
        ok, note = _build_bridge_geometry(bm, uv_layer, settings, bm_src, mw, paths_info)
        if ok:
            # Намотка граней зависит от того, какую цепочку выделили первой,
            # из-за чего заливка выходила то лицом вверх, то вниз — и текстура
            # выглядела зеркальной. Приводим к одной стороне.
            bm.normal_update()
            up = Vector((0.0, 0.0, 0.0))
            for f in bm.faces:
                up += f.normal * f.calc_area()
            if up.z < 0.0:
                for f in bm.faces:
                    f.normal_flip()
        settings.status_text = note
        if not ok:
            bm.free()
            cleanup_preview_object()
            return
        _finish_preview(context, settings, bm)
        return

    settings.status_text = ""

    width = settings.curb_width
    height = settings.curb_height
    uscale = settings.uv_scale_u
    mode = settings.profile_mode

    y_size = settings.uv_y_size
    y_offset = settings.uv_y_offset

    # ---- какие части строим ----
    # Ряды точек профиля: 0 = P0 (на ребре), 1 = P1 (излом), 2 = P2 (конец)
    # В BOTTOM_*/UP_* сначала идёт полка (P0→P1), потом стенка (P1→P2).
    # В TOP_*/DOWN_* наоборот: сначала стенка, потом полка.
    first_is_shelf = mode in ('BOTTOM_INNER', 'BOTTOM_OUTER', 'UP_INNER', 'UP_OUTER')
    if first_is_shelf:
        shelf_rows, wall_rows = (0, 1), (1, 2)
    else:
        wall_rows, shelf_rows = (0, 1), (1, 2)

    want_shelf = settings.parts_mode in ('BOTH', 'SHELF')
    want_wall = settings.parts_mode in ('BOTH', 'WALL')

    needed_rows = set()
    if want_shelf:
        needed_rows.update(shelf_rows)
    if want_wall:
        needed_rows.update(wall_rows)
    if not needed_rows:
        bm.free()
        cleanup_preview_object()
        return
    row_ids = sorted(needed_rows)

    # ---- полосы UV по профилю ----
    # Профиль по Y (распрямлённая буква Г: сперва width, потом height)
    profile_len = width + height
    if profile_len <= 1e-8:
        profile_len = 1.0
    width_frac = width / profile_len
    height_frac = height / profile_len

    if settings.uv_fit_part and settings.parts_mode != 'BOTH':
        # одна часть — растягиваем её на всю полосу
        shelf_band = (0.0, 1.0)
        wall_band = (0.0, 1.0)
    else:
        shelf_band = (0.0, width_frac)
        wall_band = (width_frac, width_frac + height_frac)

    # ---- финальное преобразование UV (разворот / зеркала / смещения) ----
    horizontal = settings.uv_orientation == 'HORIZONTAL'
    flip_u = settings.uv_flip_u
    flip_v = settings.uv_flip_v
    off_u = settings.uv_offset_u
    off_v = settings.uv_offset_v

    def _uv(a, b):
        """a — координата вдоль бордюра, b — позиция поперёк профиля (0..1)"""
        band = b * y_size + y_offset
        if horizontal:
            u, v = band, a
        else:
            u, v = a, band
        if flip_u:
            u = -u
        if flip_v:
            v = -v
        return (u, v)

    def _set_face_uv(face, u_i, u_j, band):
        b0, b1 = band
        lp = face.loops
        lp[0][uv_layer].uv = _uv(u_i, b0)
        lp[1][uv_layer].uv = _uv(u_j, b0)
        lp[2][uv_layer].uv = _uv(u_j, b1)
        lp[3][uv_layer].uv = _uv(u_i, b1)

    for idxs, is_closed in paths_info:
        if len(idxs) < 2:
            continue

        pts = [mw @ bm_src.verts[i].co for i in idxs]
        norms = [(n_mat @ bm_src.verts[i].normal).normalized() for i in idxs]
        n = len(pts)

        # длина вдоль пути (для U)
        s = [0.0] * n
        for i in range(1, n):
            s[i] = s[i - 1] + (pts[i] - pts[i - 1]).length
        total_len = s[-1]
        if is_closed:
            total_len += (pts[0] - pts[-1]).length

        # Pre-compute up vectors per vertex according to up_source
        up_mode = settings.up_source
        if up_mode == 'SMOOTH':
            r = settings.smooth_radius
            up_vecs = []
            for i in range(n):
                acc = Vector((0.0, 0.0, 0.0))
                for k in range(-r, r + 1):
                    if is_closed:
                        idx = (i + k) % n
                    else:
                        idx = max(0, min(n - 1, i + k))
                    acc += norms[idx]
                if acc.length_squared > 0.0:
                    acc.normalize()
                else:
                    acc = Vector((0.0, 0.0, 1.0))
                up_vecs.append(acc)
        elif up_mode == 'WORLD_UP':
            up_vecs = [Vector((0.0, 0.0, 1.0))] * n
        else:  # VERTEX_NORMAL (legacy)
            up_vecs = norms

        # точки профиля вдоль пути: (P0 на ребре, P1 излом, P2 конец)
        positions = []

        for i, p in enumerate(pts):
            # тангенс вдоль пути
            if i == 0:
                t = (pts[1] - pts[0]).normalized()
            elif i == n - 1:
                t = (pts[-1] - pts[-2]).normalized()
            else:
                t = (pts[i + 1] - pts[i - 1]).normalized()

            n_vec = up_vecs[i]
            if n_vec.length_squared == 0.0:
                n_vec = Vector((0.0, 0.0, 1.0))

            # направление ширины в плоскости: side = n × t
            side = n_vec.cross(t)
            if side.length_squared == 0.0:
                side = Vector((1.0, 0.0, 0.0))
            else:
                side.normalize()

            # Определяем направление side в зависимости от профиля
            # INNER = внутрь (-side), OUTER = наружу (+side)
            if 'INNER' in mode:
                side_dir = -side
            else:
                side_dir = side

            # Применяем Flip если включен
            if settings.flip_direction:
                side_dir = -side_dir

            p0 = p  # на ребре

            # 8 вариантов профиля
            if mode == 'BOTTOM_INNER':
                # ⌞ Внутрь → Вниз (горизонталь внутрь, потом вертикаль вниз)
                p1 = p0 + side_dir * width
                p2 = p1 - n_vec * height
            elif mode == 'TOP_INNER':
                # ⌜ Вверх → Внутрь (вертикаль вверх, потом горизонталь внутрь)
                p1 = p0 + n_vec * height
                p2 = p1 + side_dir * width
            elif mode == 'DOWN_INNER':
                # ⌟ Вниз → Внутрь (вертикаль вниз, потом горизонталь внутрь)
                p1 = p0 - n_vec * height
                p2 = p1 + side_dir * width
            elif mode == 'UP_INNER':
                # ⌝ Внутрь → Вверх (горизонталь внутрь, потом вертикаль вверх)
                p1 = p0 + side_dir * width
                p2 = p1 + n_vec * height
            elif mode == 'BOTTOM_OUTER':
                # ⌞ Наружу → Вниз (горизонталь наружу, потом вертикаль вниз)
                p1 = p0 + side_dir * width
                p2 = p1 - n_vec * height
            elif mode == 'TOP_OUTER':
                # ⌜ Вверх → Наружу (вертикаль вверх, потом горизонталь наружу)
                p1 = p0 + n_vec * height
                p2 = p1 + side_dir * width
            elif mode == 'DOWN_OUTER':
                # ⌟ Вниз → Наружу (вертикаль вниз, потом горизонталь наружу)
                p1 = p0 - n_vec * height
                p2 = p1 + side_dir * width
            else:  # UP_OUTER
                # ⌝ Наружу → Вверх (горизонталь наружу, потом вертикаль вверх)
                p1 = p0 + side_dir * width
                p2 = p1 + n_vec * height

            positions.append((p0, p1, p2))

        # след бордюра сверху: крайние ряды профиля
        near_row = [trio[row_ids[0]] for trio in positions]
        far_row = [trio[row_ids[-1]] for trio in positions]
        if len(near_row) > 1:
            zs = [p.z for p in near_row] + [p.z for p in far_row]
            _footprint.append({
                "poly": [(p.x, p.y) for p in near_row] +
                        [(p.x, p.y) for p in reversed(far_row)],
                "zmin": min(zs),
                "zmax": max(zs),
            })

        # создаём только те ряды вершин, которые реально нужны
        # (иначе при одной стенке в меше остаются висячие вершины)
        rows = {k: [] for k in row_ids}
        for trio in positions:
            for k in row_ids:
                rows[k].append(bm.verts.new(trio[k]))

        bm.verts.ensure_lookup_table()

        max_i = n if is_closed else n - 1
        for i in range(max_i):
            j = (i + 1) % n

            u_i = s[i] * uscale
            u_j = (s[j] if j > i else total_len) * uscale

            # Грани строятся всегда в одном порядке (как в рабочей версии),
            # инверсия нормалей — потом через face.normal_flip()
            if want_shelf:
                a, b = shelf_rows
                f = _try_face(bm, (rows[a][i], rows[a][j], rows[b][j], rows[b][i]))
                if f is not None:
                    _set_face_uv(f, u_i, u_j, shelf_band)

            if want_wall:
                a, b = wall_rows
                f = _try_face(bm, (rows[a][i], rows[a][j], rows[b][j], rows[b][i]))
                if f is not None:
                    _set_face_uv(f, u_i, u_j, wall_band)

    _finish_preview(context, settings, bm)


# ------------------------------------------------------------------------
# HANDLERS - Отслеживание изменений
# ------------------------------------------------------------------------

_PREVIEW_HANDLER_NAMES = ("check_selection_change", "check_mode_change")


def _preview_handlers():
    """Все хендлеры, удаляющие превью, — включая копии из старого отдельного
    аддона Clean G-Curb, если он включён вместе с MadOut2 Tools."""
    found = []
    for lst in (bpy.app.handlers.depsgraph_update_post, bpy.app.handlers.frame_change_post):
        for fn in list(lst):
            if getattr(fn, "__name__", "") in _PREVIEW_HANDLER_NAMES:
                found.append((lst, fn))
    return found


def _foreign_curb_modules():
    return sorted({fn.__module__ for _, fn in _preview_handlers() if fn.__module__ != __name__})


@persistent
def check_selection_change(scene, depsgraph):
    """Проверяет изменения выделения и удаляет preview при необходимости"""
    global _IN_HANDLER
    if _IN_HANDLER or _SUSPEND:
        return

    context = bpy.context

    # Проверяем режим и наличие активного объекта
    if context.active_object is None or context.active_object.type != 'MESH':
        cleanup_preview_object()
        return

    # Если не в режиме Edit - удаляем preview
    if context.active_object.mode != 'EDIT':
        cleanup_preview_object()
        return

    # Проверяем наличие preview объекта
    preview_obj = bpy.data.objects.get("Curb_Preview")
    if preview_obj is None:
        return

    settings = getattr(scene, "curb_settings", None)
    if settings is None or not settings.live_preview:
        return

    _IN_HANDLER = True
    try:
        # Проверяем изменилось ли выделение
        current_hash = get_selected_edges_hash(context)

        if current_hash is None:
            # Нет выделенных рёбер - удаляем preview
            cleanup_preview_object()
        elif str(current_hash) != settings.last_edge_hash:
            # Выделение изменилось - обновляем preview
            build_curb_preview(context)
    finally:
        _IN_HANDLER = False


@persistent
def check_mode_change(scene):
    """Удаляет preview при выходе из Edit Mode"""
    context = bpy.context
    if context.active_object is None or context.active_object.mode != 'EDIT':
        cleanup_preview_object()


@persistent
def restore_active_preset(dummy):
    """
    После загрузки .blend восстанавливаем выбранный пресет по имени.
    Динамические enum'ы хранятся индексом, а список пресетов мог поменяться.
    """
    load_presets(force=True)
    # материалы и текстуры в новом файле другие — превью пересобрать
    invalidate_previews()
    presets = load_presets()
    for scene in bpy.data.scenes:
        settings = getattr(scene, "curb_settings", None)
        if settings is None:
            continue
        name = settings.active_preset_name
        set_active_preset_silent(settings, name if name in presets else NO_PRESET)


# ------------------------------------------------------------------------
# ОПЕРАТОРЫ — ПРЕСЕТЫ
# ------------------------------------------------------------------------

class CURB_OT_preset_save(Operator):
    bl_idname = "curb.preset_save"
    bl_label = "Save Preset"
    bl_description = "Сохранить текущие настройки как пресет (профиль, части, UV, материал)"
    bl_options = {'REGISTER', 'INTERNAL'}

    preset_name: StringProperty(
        name="Name",
        description="Имя пресета. Существующий пресет с таким именем будет перезаписан",
        default="New Curb Preset",
    )

    def invoke(self, context, event):
        s = context.scene.curb_settings
        self.preset_name = s.active_preset_name or "New Curb Preset"
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "preset_name")
        if self.preset_name.strip() in load_presets():
            layout.label(text="Пресет с таким именем будет перезаписан", icon='ERROR')

    def execute(self, context):
        name = self.preset_name.strip()
        if not name:
            self.report({'ERROR'}, "Имя пресета не может быть пустым")
            return {'CANCELLED'}

        s = context.scene.curb_settings
        presets = load_presets()
        presets[name] = capture_preset_values(s)

        if not save_presets():
            self.report({'ERROR'}, "Не удалось записать файл пресетов (см. консоль)")
            return {'CANCELLED'}

        invalidate_previews()
        set_active_preset_silent(s, name)
        self.report({'INFO'}, f"Пресет «{name}» сохранён")
        return {'FINISHED'}


def _analyse_curb_object(obj, faces=None, bm=None):
    """
    Разбирает бордюр по ВЫДЕЛЕННЫМ граням: ширина, высота, части, материал, UV.
    Сечение считаем по «перемычкам» — коротким рёбрам граней.
    """
    own = bm is None
    if own:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        faces = list(bm.faces)
    if not faces:
        if own:
            bm.free()
        return None, "не выделено ни одной грани"

    mw = obj.matrix_world
    nm = mw.to_3x3()

    flat_len, wall_len = [], []
    flat_n = wall_n = 0
    uv_lay = bm.loops.layers.uv.active

    along_ratio, across_span = [], []
    mat_idx = {}
    orient_sum = [0.0]
    for f in faces:
        n = nm @ f.normal
        if n.length_squared < 1e-12:
            continue
        n.normalize()
        horizontal = abs(n.z) >= 0.5
        # рёбра грани: поперечные — те, что короче и не общие с соседями
        lens = []
        for e in f.edges:
            a = mw @ e.verts[0].co
            b = mw @ e.verts[1].co
            lens.append(((a - b).length, e))
        lens.sort(key=lambda t: t[0])
        rib = lens[0][0]
        if horizontal:
            flat_len.append(rib); flat_n += 1
        else:
            wall_len.append(rib); wall_n += 1

        mat_idx[f.material_index] = mat_idx.get(f.material_index, 0) + 1

        # ориентация UV грани относительно её нормали — по ней ловим зеркало
        if uv_lay is not None:
            pts = [l[uv_lay].uv for l in f.loops]
            a2 = 0.0
            for i in range(len(pts)):
                j = (i + 1) % len(pts)
                a2 += pts[i].x * pts[j].y - pts[j].x * pts[i].y
            orient_sum[0] += a2 * (1.0 if n.z >= 0.0 else -1.0)

        if uv_lay is not None:
            for l in f.loops:
                nxt = l.link_loop_next
                d3 = ((mw @ l.vert.co) - (mw @ nxt.vert.co)).length
                d2 = (Vector((l[uv_lay].uv.x, l[uv_lay].uv.y, 0.0)) -
                      Vector((nxt[uv_lay].uv.x, nxt[uv_lay].uv.y, 0.0))).length
                if d3 > 1e-6:
                    along_ratio.append(d2 / d3)

    uv_stats = None
    if uv_lay is not None:
        us = [l[uv_lay].uv.x for f in faces for l in f.loops]
        vs = [l[uv_lay].uv.y for f in faces for l in f.loops]
        across_span = [max(us) - min(us), max(vs) - min(vs)]
        uv_stats = (min(us), max(us), min(vs), max(vs))
    if own:
        bm.free()

    if not flat_len and not wall_len:
        return None, "не удалось разобрать сечение"

    data = {}
    if flat_len and wall_len:
        data["parts_mode"] = 'BOTH'
    elif flat_len:
        data["parts_mode"] = 'SHELF'
    else:
        data["parts_mode"] = 'WALL'
    data["curb_width"] = (sum(flat_len) / len(flat_len)) if flat_len else 0.0
    data["curb_height"] = (sum(wall_len) / len(wall_len)) if wall_len else 0.0

    if mat_idx and obj.material_slots:
        idx = max(mat_idx, key=mat_idx.get)
        if idx < len(obj.material_slots) and obj.material_slots[idx].material:
            data["material_name"] = obj.material_slots[idx].material.name

    if along_ratio:
        data["uv_scale_u"] = sorted(along_ratio)[len(along_ratio) // 2]
    if across_span and len(across_span) == 2 and uv_stats:
        u0, u1, v0, v1 = uv_stats
        # поперёк — та ось, по которой размах меньше: это полоса в атласе
        if across_span[0] < across_span[1]:
            data["uv_orientation"] = 'HORIZONTAL'
            data["uv_y_size"] = across_span[0]
            data["uv_y_offset"] = u0          # ГДЕ полоса лежит в атласе
        else:
            data["uv_orientation"] = 'VERTICAL'
            data["uv_y_size"] = across_span[1]
            data["uv_y_offset"] = v0
        data["uv_offset_u"] = 0.0
        data["uv_offset_v"] = 0.0
        data["uv_flip_u"] = False
        # Аддон приводит развёртку к своей ориентации (площадь UV с учётом
        # нормали — отрицательная). Если у захваченного объекта она обратная,
        # значит нужен Flip, иначе текстура ляжет зеркально.
        data["uv_flip_v"] = orient_sum[0] > 0.0

    return data, ""


class CURB_OT_preset_from_object(Operator):
    bl_idname = "curb.preset_from_object"
    bl_label = "Preset From Object"
    bl_description = (
        "Разобрать выделенный объект-бордюр и сохранить его как пресет: "
        "ширина, высота, части, материал и UV"
    )
    bl_options = {'REGISTER', 'INTERNAL'}

    preset_name: StringProperty(name="Name", default="")

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH'

    def invoke(self, context, event):
        self.preset_name = context.active_object.name
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        o = context.active_object
        if o.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(o.data)
            n = sum(1 for f in bm.faces if f.select)
            layout.label(text=f"Выделено граней: {n}", icon='FACESEL')
        else:
            layout.label(text=f"Весь объект: {o.name}", icon='MESH_DATA')
        layout.prop(self, "preset_name")

    def execute(self, context):
        obj = context.active_object
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(obj.data)
            sel = [f for f in bm.faces if f.select]
            if not sel:
                self.report({'ERROR'}, "Выдели грани бордюра")
                return {'CANCELLED'}
            data, err = _analyse_curb_object(obj, sel, bm)
        else:
            data, err = _analyse_curb_object(obj)
        if data is None:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}

        name = self.preset_name.strip() or obj.name
        s = context.scene.curb_settings
        global _SUSPEND
        _SUSPEND += 1
        try:
            for k, v in data.items():
                try:
                    setattr(s, k, v)
                except Exception as e:
                    print(f"[Clean G-Curb] Пропущено «{k}»: {e}")
        finally:
            _SUSPEND -= 1

        presets = load_presets()
        presets[name] = capture_preset_values(s)
        if not save_presets():
            self.report({'ERROR'}, "Не удалось записать файл пресетов")
            return {'CANCELLED'}

        invalidate_previews()
        set_active_preset_silent(s, name)
        self.report({'INFO'},
                    f"Пресет «{name}»: {data.get('parts_mode')} "
                    f"W={data.get('curb_width', 0):.3f} H={data.get('curb_height', 0):.3f} "
                    f"мат={data.get('material_name', '—')}")
        return {'FINISHED'}


class CURB_OT_preset_rename(Operator):
    bl_idname = "curb.preset_rename"
    bl_label = "Rename Preset"
    bl_description = "Переименовать активный пресет"
    bl_options = {'REGISTER', 'INTERNAL'}

    new_name: StringProperty(
        name="New Name",
        description="Новое имя пресета",
        default="",
    )

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "curb_settings", None)
        return bool(s and s.active_preset_name and s.active_preset_name in load_presets())

    def invoke(self, context, event):
        self.new_name = context.scene.curb_settings.active_preset_name
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        old = context.scene.curb_settings.active_preset_name
        layout.label(text=f"Было: {old}", icon='PRESET')
        layout.prop(self, "new_name")
        new = self.new_name.strip()
        if new and new != old and new in load_presets():
            layout.label(text="Пресет с таким именем будет перезаписан", icon='ERROR')

    def execute(self, context):
        s = context.scene.curb_settings
        old = s.active_preset_name
        new = self.new_name.strip()

        if not new:
            self.report({'ERROR'}, "Имя пресета не может быть пустым")
            return {'CANCELLED'}

        presets = load_presets()
        if old not in presets:
            self.report({'ERROR'}, "Активный пресет не найден")
            return {'CANCELLED'}
        if new == old:
            return {'CANCELLED'}

        # пересобираем словарь, чтобы пресет остался на своём месте в файле
        renamed = {}
        for key, values in presets.items():
            if key == old:
                renamed[new] = values
            elif key == new:
                continue  # перезаписываем одноимённый
            else:
                renamed[key] = values

        presets.clear()
        presets.update(renamed)

        if not save_presets():
            self.report({'ERROR'}, "Не удалось записать файл пресетов (см. консоль)")
            return {'CANCELLED'}

        invalidate_previews()
        set_active_preset_silent(s, new)
        self.report({'INFO'}, f"«{old}» → «{new}»")
        return {'FINISHED'}


class CURB_OT_preset_update(Operator):
    bl_idname = "curb.preset_update"
    bl_label = "Update Preset"
    bl_description = "Перезаписать активный пресет текущими настройками"
    bl_options = {'REGISTER', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "curb_settings", None)
        return bool(s and s.active_preset_name and s.active_preset_name in load_presets())

    def execute(self, context):
        s = context.scene.curb_settings
        name = s.active_preset_name
        presets = load_presets()
        if name not in presets:
            self.report({'ERROR'}, "Активный пресет не найден")
            return {'CANCELLED'}

        presets[name] = capture_preset_values(s)
        if not save_presets():
            self.report({'ERROR'}, "Не удалось записать файл пресетов (см. консоль)")
            return {'CANCELLED'}

        invalidate_previews()
        set_active_preset_silent(s, name)
        self.report({'INFO'}, f"Пресет «{name}» обновлён")
        return {'FINISHED'}


class CURB_OT_preset_delete(Operator):
    bl_idname = "curb.preset_delete"
    bl_label = "Delete Preset"
    bl_description = "Удалить активный пресет"
    bl_options = {'REGISTER', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "curb_settings", None)
        return bool(s and s.active_preset_name and s.active_preset_name in load_presets())

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = context.scene.curb_settings
        name = s.active_preset_name
        presets = load_presets()
        if name not in presets:
            self.report({'ERROR'}, "Активный пресет не найден")
            return {'CANCELLED'}

        del presets[name]
        save_presets()
        invalidate_previews()
        set_active_preset_silent(s, NO_PRESET)
        self.report({'INFO'}, f"Пресет «{name}» удалён")
        return {'FINISHED'}


class CURB_OT_preset_reload(Operator):
    bl_idname = "curb.preset_reload"
    bl_label = "Reload Presets From Disk"
    bl_description = "Перечитать файл пресетов (если правили его руками или на другой машине)"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        presets = load_presets(force=True)
        invalidate_previews()
        s = context.scene.curb_settings
        name = s.active_preset_name
        set_active_preset_silent(s, name if name in presets else NO_PRESET)
        self.report({'INFO'}, f"Загружено пресетов: {len(presets)}")
        return {'FINISHED'}


class CURB_OT_preset_restore_factory(Operator):
    bl_idname = "curb.preset_restore_factory"
    bl_label = "Restore Factory Presets"
    bl_description = "Вернуть заводские пресеты (Border 1-3, Horizontal UV, Wall/Shelf Only). Свои пресеты не удаляются"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        presets = load_presets()
        for name, values in FACTORY_PRESETS.items():
            presets[name] = dict(values)
        save_presets()
        invalidate_previews()
        self.report({'INFO'}, "Заводские пресеты восстановлены")
        return {'FINISHED'}


class CURB_OT_preset_export(Operator):
    bl_idname = "curb.preset_export"
    bl_label = "Export Presets (.json)"
    bl_description = "Выгрузить все пресеты в JSON-файл"
    bl_options = {'REGISTER', 'INTERNAL'}

    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        self.filepath = PRESET_FILENAME
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(
                    {"version": PRESET_FILE_VERSION, "presets": load_presets()},
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except Exception as e:
            self.report({'ERROR'}, f"Не удалось сохранить: {e}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Пресеты выгружены: {self.filepath}")
        return {'FINISHED'}


class CURB_OT_preset_import(Operator):
    bl_idname = "curb.preset_import"
    bl_label = "Import Presets (.json)"
    bl_description = "Загрузить пресеты из JSON-файла (совпадающие имена перезаписываются)"
    bl_options = {'REGISTER', 'INTERNAL'}

    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"Не удалось прочитать: {e}")
            return {'CANCELLED'}

        incoming = _sanitize_presets(raw.get("presets", raw) if isinstance(raw, dict) else {})
        if not incoming:
            self.report({'WARNING'}, "В файле не найдено пресетов")
            return {'CANCELLED'}

        presets = load_presets()
        presets.update(incoming)
        save_presets()
        invalidate_previews()
        self.report({'INFO'}, f"Импортировано пресетов: {len(incoming)}")
        return {'FINISHED'}


class CURB_OT_preset_open_folder(Operator):
    bl_idname = "curb.preset_open_folder"
    bl_label = "Open Presets Folder"
    bl_description = "Открыть папку, где лежит файл пресетов"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=preset_dir())
        return {'FINISHED'}


class CURB_MT_preset_specials(Menu):
    bl_idname = "CURB_MT_preset_specials"
    bl_label = "Preset Options"

    def draw(self, context):
        layout = self.layout
        layout.operator("curb.preset_from_object", icon='IMPORT')
        layout.separator()
        layout.operator("curb.preset_rename", icon='GREASEPENCIL')
        layout.separator()
        layout.operator("curb.preset_reload", icon='FILE_REFRESH')
        layout.operator("curb.preset_restore_factory", icon='RECOVER_LAST')
        layout.separator()
        layout.operator("curb.preset_import", icon='IMPORT')
        layout.operator("curb.preset_export", icon='EXPORT')
        layout.separator()
        layout.operator("curb.preset_open_folder", icon='FILE_FOLDER')


# ------------------------------------------------------------------------
# ОПЕРАТОРЫ — ПРЕВЬЮ И BAKE
# ------------------------------------------------------------------------

class CURB_OT_update_preview(Operator):
    bl_idname = "object.curb_update_preview"
    bl_label = "Update Preview From Selection"
    bl_description = "Создать/обновить бордюр по выделенным рёбрам (живое превью)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.active_object and context.active_object.mode == 'EDIT':
            build_curb_preview(context)
            self.report({'INFO'}, "Preview updated")
        else:
            self.report({'WARNING'}, "Select edges in Edit Mode first")
            cleanup_preview_object()
        return {'FINISHED'}


class CURB_OT_bake_and_merge(Operator):
    bl_idname = "object.curb_bake_and_merge"
    bl_label = "Bake Curb & Merge"
    bl_description = (
        "Запечь Curb_Preview в активный меш и сделать Merge by Distance"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.curb_settings
        src_obj = context.active_object

        if src_obj is None or src_obj.type != 'MESH':
            self.report({'ERROR'}, "Active object must be a mesh")
            return {'CANCELLED'}

        # Получаем curb объект
        curb_obj = bpy.data.objects.get("Curb_Preview")
        if curb_obj is None:
            self.report({'ERROR'}, "Curb_Preview not found (сначала сделай превью)")
            return {'CANCELLED'}

        # Вшиваем заливку в соседнюю геометрию, пока ещё в Edit Mode
        stitch_note = ""
        if (settings.build_mode == 'BRIDGE' and settings.bridge_stitch
                and _stitch_points and src_obj.mode == 'EDIT'):
            try:
                bm_src = bmesh.from_edit_mesh(src_obj.data)
                cuts, spread = _stitch_source_mesh(
                    bm_src, _stitch_points, settings.stitch_depth)
                if cuts:
                    bmesh.update_edit_mesh(src_obj.data)
                    stitch_note = f", вшито рёбер: {cuts} (+{spread} вглубь)"
            except Exception as e:
                print(f"[Clean G-Curb] Не удалось вшить заливку: {e}")
                stitch_note = ", вшить не удалось (см. консоль)"

        cut_note = ""

        # габариты бордюра — по ним потом найдём его свободную сторону
        gap_box = None
        if settings.close_free_side and curb_obj.data.vertices:
            cos = [curb_obj.matrix_world @ v.co for v in curb_obj.data.vertices]
            mn = Vector((min(c.x for c in cos), min(c.y for c in cos), min(c.z for c in cos)))
            mx = Vector((max(c.x for c in cos), max(c.y for c in cos), max(c.z for c in cos)))
            pad = max(0.25, (mx - mn).length * 0.05)
            gap_box = (mn - Vector((pad, pad, pad)), mx + Vector((pad, pad, pad)))

        # Сохраняем режим для возврата
        original_mode = src_obj.mode

        # ВРЕМЕННО снимаем ВСЕ хендлеры, удаляющие preview (и чужие копии) —
        # иначе переход в Object Mode перед join убьёт превью
        suspended = _preview_handlers()
        for lst, fn in suspended:
            lst.remove(fn)

        try:
            # Грани под бордюром удаляем ДО join — иначе он ляжет вторым слоем.
            # Делается после снятия хендлеров, иначе смена режима убьёт превью.
            if settings.close_free_side and _footprint:
                try:
                    killed = _cut_under(src_obj, list(_footprint))
                    if killed:
                        cut_note = f", вырезано под бордюром: {killed}"
                except Exception as e:
                    print(f"[Clean G-Curb] Не удалось вырезать под бордюром: {e}")

            # Переходим в Object Mode
            if src_obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')

            # Проверяем что объект всё ещё существует
            curb_obj = bpy.data.objects.get("Curb_Preview")
            if curb_obj is None:
                self.report({'ERROR'}, "Curb_Preview was removed")
                return {'CANCELLED'}

            # UV меша могли переименовать после превью — сверяем перед Join
            if src_obj.data.uv_layers and curb_obj.data.uv_layers:
                curb_obj.data.uv_layers[0].name = _mesh_uv_name(src_obj.data.uv_layers)

            # Join объектов (нормали уже правильные в preview)
            bpy.ops.object.select_all(action='DESELECT')
            src_obj.select_set(True)
            curb_obj.select_set(True)
            context.view_layer.objects.active = src_obj
            bpy.ops.object.join()

            # Merge by Distance
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            try:
                bpy.ops.mesh.remove_doubles(threshold=settings.merge_distance)
            except Exception:
                pass

            # Зашиваем свободную сторону бордюра в вырезанную дыру
            if gap_box is not None:
                try:
                    _close_free_side.info = ""
                    filled = _close_free_side(src_obj, gap_box)
                    if filled:
                        stitch_note += f", зашито граней: {filled}"
                    else:
                        stitch_note += f", зашивать нечего [{_close_free_side.info}]"
                except Exception as e:
                    print(f"[Clean G-Curb] Не удалось зашить свободную сторону: {e}")
                    stitch_note += ", зашить не удалось (см. консоль)"

            # Возвращаем режим
            bpy.ops.object.mode_set(mode=original_mode)

            # Очищаем mesh данные
            mesh = bpy.data.meshes.get("Curb_PreviewMesh")
            if mesh and mesh.users == 0:
                bpy.data.meshes.remove(mesh)

            self.report({'INFO'},
                        "Curb baked and merged with Merge by Distance"
                        + cut_note + stitch_note)
            return {'FINISHED'}

        finally:
            # Восстанавливаем handlers
            for lst, fn in suspended:
                if fn not in lst:
                    lst.append(fn)


def _point_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            t = (y - y1) / (y2 - y1) if abs(y2 - y1) > 1e-12 else 0.0
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def _seg_hit(p1, p2, a, b):
    """Пересечение двух ОТРЕЗКОВ на плоскости XY: (t на p1p2, точка) или None"""
    r = (p2[0] - p1[0], p2[1] - p1[1])
    sv = (b[0] - a[0], b[1] - a[1])
    den = r[0] * sv[1] - r[1] * sv[0]
    if abs(den) < 1e-12:
        return None
    qp = (a[0] - p1[0], a[1] - p1[1])
    t = (qp[0] * sv[1] - qp[1] * sv[0]) / den
    u = (qp[0] * r[1] - qp[1] * r[0]) / den
    if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
        return t
    return None


def _knife_segment(bm, mw, a2, b2, zmin, zmax):
    """
    Прорезает грани строго ПО ОТРЕЗКУ контура: находим, где отрезок входит
    и выходит из грани, режем эти два ребра и соединяем. Никаких бесконечных
    плоскостей — поэтому ничего не рубит через весь меш.
    """
    band = max(0.05, (zmax - zmin) * 2.0)
    made = 0
    for f in list(bm.faces):
        if not f.is_valid:
            continue
        c = mw @ f.calc_center_median()
        if not (zmin - band <= c.z <= zmax + band):
            continue
        n = mw.to_3x3() @ f.normal
        if n.length_squared < 1e-12 or abs(n.normalized().z) < 0.5:
            continue

        hits = []
        for e in list(f.edges):
            p1 = mw @ e.verts[0].co
            p2 = mw @ e.verts[1].co
            t = _seg_hit((p1.x, p1.y), (p2.x, p2.y), a2, b2)
            if t is None:
                continue
            if t < 1e-6 or t > 1.0 - 1e-6:
                continue            # попали в существующую вершину — резать нечего
            hits.append((e, t))
        if len(hits) != 2:
            continue

        try:
            _, v1 = bmesh.utils.edge_split(hits[0][0], hits[0][0].verts[0], hits[0][1])
            _, v2 = bmesh.utils.edge_split(hits[1][0], hits[1][0].verts[0], hits[1][1])
        except (ValueError, RuntimeError, TypeError):
            continue

        target = None
        for cand in v1.link_faces:
            if v2 in cand.verts:
                target = cand
                break
        if target is None:
            continue
        try:
            bmesh.utils.face_split(target, v1, v2)
            made += 1
        except (ValueError, RuntimeError, TypeError):
            continue
    return made


def _slice_along(bm, mw, stamps, up):
    """Прорезает исходные грани по всему контуру следа бордюра"""
    cuts = 0
    for st in stamps:
        poly = st["poly"]
        for i in range(len(poly)):
            a2 = poly[i]
            b2 = poly[(i + 1) % len(poly)]
            if math.hypot(b2[0] - a2[0], b2[1] - a2[1]) < 1e-6:
                continue
            cuts += _knife_segment(bm, mw, a2, b2, st["zmin"], st["zmax"])
    return cuts


def _cut_under(obj, stamps):
    """
    Удаляет грани исходного меша, попавшие ПОД бордюр (двойная геометрия).
    Только ГОРИЗОНТАЛЬНЫЕ грани и только рядом по высоте — иначе сносит
    вертикальные стенки, которые просто оказались под следом бордюра сверху.
    """
    if not stamps:
        return 0
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='DESELECT')

    bm = bmesh.from_edit_mesh(obj.data)
    mw = obj.matrix_world
    nm = mw.to_3x3()

    # сначала прорезаем контур, иначе большие грани не удалить
    _slice_along(bm, mw, stamps, Vector((0.0, 0.0, 1.0)))
    bm.faces.ensure_lookup_table()

    doomed = 0
    for f in bm.faces:
        c = mw @ f.calc_center_median()
        n = (nm @ f.normal)
        if n.length_squared < 1e-12:
            continue
        n.normalize()
        if abs(n.z) < 0.5:          # вертикальная стенка — не трогаем
            continue
        for st in stamps:
            band = max(0.05, (st["zmax"] - st["zmin"]) * 2.0)
            if not (st["zmin"] - band <= c.z <= st["zmax"] + band):
                continue
            if _point_in_poly(c.x, c.y, st["poly"]):
                f.select = True
                doomed += 1
                break
    bm.select_flush(True)          # без флуша оператор не увидит выделение
    bmesh.update_edit_mesh(obj.data)
    if doomed:
        bpy.ops.mesh.delete(type='FACE')
        # разрез может оставить нулевые лоскуты на месте старых вершин
        bpy.ops.mesh.select_all(action='SELECT')
        try:
            bpy.ops.mesh.dissolve_degenerate(threshold=1e-4)
        except RuntimeError:
            pass
        bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    return doomed


def _drop_degenerate(obj):
    """Заливка/мост могут оставить нулевые грани на совпавших вершинах"""
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(obj.data)
    bad = 0
    for f in bm.faces:
        if f.calc_area() < 1e-9:
            f.select = True
            bad += 1
    bm.select_flush(True)
    bmesh.update_edit_mesh(obj.data)
    if bad:
        bpy.ops.mesh.delete(type='FACE')
    bpy.ops.object.mode_set(mode='OBJECT')
    return bad


def _close_free_side(obj, box):
    """
    Мостит свободную сторону бордюра с ближайшим краем вырезанной дыры.
    Берём РОВНО две границы: ту, что внутри габаритов бордюра, и ближайшую
    к ней — иначе замащивает вообще все дыры в меше.
    """
    mn, mx = box
    inv = obj.matrix_world.inverted()
    c_lo, c_hi = inv @ mn, inv @ mx
    lo_x, hi_x = min(c_lo.x, c_hi.x), max(c_lo.x, c_hi.x)
    lo_y, hi_y = min(c_lo.y, c_hi.y), max(c_lo.y, c_hi.y)
    lo_z, hi_z = min(c_lo.z, c_hi.z), max(c_lo.z, c_hi.z)

    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='EDGE')
    bpy.ops.mesh.select_all(action='DESELECT')

    bm = bmesh.from_edit_mesh(obj.data)
    border = [e for e in bm.edges if len(e.link_faces) == 1]
    _close_free_side.info = f"границ={len(border)}"
    if len(border) < 2:
        bpy.ops.object.mode_set(mode='OBJECT')
        return 0

    # разбиваем границы на связные петли
    rest = set(border)
    loops = []
    while rest:
        start = rest.pop()
        group, stack = [start], [start]
        while stack:
            cur = stack.pop()
            for v in cur.verts:
                for e2 in v.link_edges:
                    if e2 in rest:
                        rest.discard(e2)
                        group.append(e2)
                        stack.append(e2)
        loops.append(group)

    def inside(e):
        for v in e.verts:
            c = v.co
            if not (lo_x <= c.x <= hi_x and lo_y <= c.y <= hi_y and lo_z <= c.z <= hi_z):
                return False
        return True

    free = max(loops, key=lambda g: sum(1 for e in g if inside(e)))
    n_in = sum(1 for e in free if inside(e))
    _close_free_side.info += f" петель={len(loops)} в габаритах={n_in}"
    if not any(inside(e) for e in free):
        bpy.ops.object.mode_set(mode='OBJECT')
        return 0

    others = [g for g in loops if g is not free]
    if not others:
        # Одна общая граница: заливать её нельзя — bpy.ops.mesh.fill
        # триангулирует петлю насквозь и оставляет длинные рёбра через меш.
        _close_free_side.info += " одна общая граница, мостить нечего"
        bpy.ops.object.mode_set(mode='OBJECT')
        return 0

    free_pts = [v.co for e in free for v in e.verts]

    def gap(group):
        best = 1e30
        for e in group:
            for v in e.verts:
                for p2 in free_pts:
                    d = (v.co - p2).length_squared
                    if d < best:
                        best = d
        return best

    target = min(others, key=gap)

    for e in free:
        e.select = True
    for e in target:
        e.select = True
    bmesh.update_edit_mesh(obj.data)

    bpy.ops.object.mode_set(mode='OBJECT')
    before = len(obj.data.polygons)
    limit = 0.0
    for e in obj.data.edges:
        d = (obj.data.vertices[e.vertices[0]].co -
             obj.data.vertices[e.vertices[1]].co).length
        if d > limit:
            limit = d
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bpy.ops.mesh.bridge_edge_loops()
    except RuntimeError as e:
        _close_free_side.info += f" bridge:{e}"
        bpy.ops.object.mode_set(mode='OBJECT')
        return 0
    bpy.ops.object.mode_set(mode='OBJECT')
    made = max(0, len(obj.data.polygons) - before)

    # ЗАЩИТА: мост мог зацепиться за дальний контур и протянуть рёбра через
    # весь меш. Такое лучше не делать вовсе — откатываем.
    worst = 0.0
    for e in obj.data.edges:
        d = (obj.data.vertices[e.vertices[0]].co -
             obj.data.vertices[e.vertices[1]].co).length
        if d > worst:
            worst = d
    if made and worst > limit * 1.2 + 1e-6:
        bpy.ops.ed.undo_push(message="curb stitch")
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bm2 = bmesh.from_edit_mesh(obj.data)
        killed = 0
        for f in bm2.faces:
            for e in f.edges:
                if (e.verts[0].co - e.verts[1].co).length > limit * 1.2:
                    f.select = True
                    killed += 1
                    break
        bm2.select_flush(True)
        bmesh.update_edit_mesh(obj.data)
        if killed:
            bpy.ops.mesh.delete(type='FACE')
        bpy.ops.object.mode_set(mode='OBJECT')
        _close_free_side.info += f" мост тянул через меш ({worst:.1f}м) — откатил"
        return 0

    _drop_degenerate(obj)
    return made


class CURB_OT_clear_preview(Operator):
    bl_idname = "object.curb_clear_preview"
    bl_label = "Clear Preview"
    bl_description = "Удалить preview объект"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        cleanup_preview_object()
        self.report({'INFO'}, "Preview cleared")
        return {'FINISHED'}


# ------------------------------------------------------------------------
# ПАНЕЛЬ
# ------------------------------------------------------------------------

class CURB_PT_panel(Panel):
    bl_label = "Clean G-Curb (Live)"
    bl_idname = "CURB_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_order = 3
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        foreign = _foreign_curb_modules()
        if foreign:
            box = layout.box()
            box.alert = True
            box.label(text="Включён старый аддон G-Curb:", icon='ERROR')
            box.label(text=", ".join(foreign))
            box.label(text="Выключите его в Preferences → Add-ons")
        s = context.scene.curb_settings

        # ---- Пресеты ----
        box = layout.box()
        box.label(text="Presets:", icon='PRESET')
        row = box.row(align=True)
        row.prop(s, "active_preset", text="")
        row.operator("curb.preset_save", text="", icon='ADD')
        row.operator("curb.preset_update", text="", icon='FILE_TICK')
        row.operator("curb.preset_rename", text="", icon='GREASEPENCIL')
        row.operator("curb.preset_delete", text="", icon='REMOVE')
        row.menu("CURB_MT_preset_specials", text="", icon='DOWNARROW_HLT')
        if s.active_preset_name:
            box.label(text=s.active_preset_name, icon='CHECKMARK')

        v = bl_info["version"]
        layout.label(text=f"v{v[0]}.{v[1]}.{v[2]}", icon='CHECKMARK')

        col = layout.column(align=True)
        col.label(text="Mode:")
        row = col.row(align=True)
        row.prop_enum(s, "build_mode", 'CURB')
        row.prop_enum(s, "build_mode", 'BRIDGE')

        bridge = s.build_mode == 'BRIDGE'

        col.separator()
        col.prop(s, "live_preview", toggle=True, icon='HIDE_OFF')

        if s.status_text:
            col.separator()
            col.box().label(text=s.status_text, icon='INFO')

        if bridge:
            col.separator()
            box = col.box()
            box.label(text="Выдели по ребру с каждой стороны", icon='EDGESEL')
            box.label(text="Ширина берётся из геометрии")
            box.label(text="UV тайлится, а не растягивается")

            col.separator()
            col.label(text="Ribs:")
            row = col.row(align=True)
            row.prop_enum(s, "bridge_pairing", 'PROJECT')
            row.prop_enum(s, "bridge_pairing", 'LENGTH')
            col.prop(s, "stitch_snap")
            col.prop(s, "bridge_cuts")

            col.separator()
            col.prop(s, "bridge_stitch", toggle=True, icon='MOD_BOOLEAN')
            if s.bridge_stitch:
                col.prop(s, "stitch_depth")

            col.separator()
            col.prop(s, "flip_normals", toggle=True)
        else:
            col.separator()
            col.label(text="Profile (Г):")
            col.prop(s, "curb_width")
            col.prop(s, "curb_height")

            col.separator()
            col.label(text="Type (Inner):")

            # Кнопки для INNER профилей (4 варианта)
            row = col.row(align=True)
            row.prop_enum(s, "profile_mode", 'BOTTOM_INNER')
            row.prop_enum(s, "profile_mode", 'TOP_INNER')

            row = col.row(align=True)
            row.prop_enum(s, "profile_mode", 'DOWN_INNER')
            row.prop_enum(s, "profile_mode", 'UP_INNER')

            col.separator()
            col.label(text="Type (Outer):")

            # Кнопки для OUTER профилей (4 варианта)
            row = col.row(align=True)
            row.prop_enum(s, "profile_mode", 'BOTTOM_OUTER')
            row.prop_enum(s, "profile_mode", 'TOP_OUTER')

            row = col.row(align=True)
            row.prop_enum(s, "profile_mode", 'DOWN_OUTER')
            row.prop_enum(s, "profile_mode", 'UP_OUTER')

            col.separator()
            col.label(text="Parts:")
            row = col.row(align=True)
            row.prop_enum(s, "parts_mode", 'BOTH')
            row.prop_enum(s, "parts_mode", 'SHELF')
            row.prop_enum(s, "parts_mode", 'WALL')
            if s.parts_mode == 'WALL':
                col.label(text="Width = отступ стенки от ребра", icon='INFO')
            elif s.parts_mode == 'SHELF':
                col.label(text="Height = подъём полки над ребром", icon='INFO')

            col.separator()
            col.prop(s, "flip_direction", toggle=True)
            col.prop(s, "flip_normals", toggle=True)

            col.separator()
            col.label(text="Up Vector:")
            col.prop(s, "up_source", text="")
            if s.up_source == 'SMOOTH':
                col.prop(s, "smooth_radius")

        col.separator()
        col.label(text="UV:")
        row = col.row(align=True)
        row.prop_enum(s, "uv_orientation", 'VERTICAL')
        row.prop_enum(s, "uv_orientation", 'HORIZONTAL')
        if bridge:
            col.label(text="UV Method:")
            row = col.row(align=True)
            row.prop_enum(s, "uv_method", 'SIDES')
            row.prop_enum(s, "uv_method", 'PLANAR')
            row = col.row(align=True)
            row.prop_enum(s, "uv_align", 'SIDE_A')
            row.prop_enum(s, "uv_align", 'SIDE_B')
        col.prop(s, "uv_scale_u", text="UV Tiling Scale" if bridge else "UV Length Scale")
        if not bridge:
            col.prop(s, "uv_y_size")
        col.prop(s, "uv_y_offset", text="UV Across Offset" if bridge else "UV Band Offset")
        row = col.row(align=True)
        row.prop(s, "uv_flip_u", toggle=True)
        row.prop(s, "uv_flip_v", toggle=True)
        col.prop(s, "uv_offset_u")
        col.prop(s, "uv_offset_v")
        if not bridge:
            sub = col.row()
            sub.enabled = s.parts_mode != 'BOTH'
            sub.prop(s, "uv_fit_part", toggle=True)

        col.separator()
        col.label(text="Material:")
        col.prop(s, "material_name")

        col.separator()
        col.label(text="Actions:")
        col.operator("object.curb_update_preview", icon='FILE_REFRESH')

        # Показываем статус preview
        preview_obj = bpy.data.objects.get("Curb_Preview")
        if preview_obj:
            box = col.box()
            box.label(text="Preview Active", icon='HIDE_OFF')

        col.separator()
        col.label(text="Bake:")
        col.prop(s, "close_free_side", toggle=True, icon='MOD_BOOLEAN')
        col.prop(s, "merge_distance")
        col.operator("object.curb_bake_and_merge", icon='CHECKMARK')
        col.operator("object.curb_clear_preview", icon='X')


class CURB_PT_catalog(Panel):
    bl_label = "Preset Catalog"
    bl_idname = "CURB_PT_catalog"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadOut2"
    bl_parent_id = "CURB_PT_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.curb_settings

        if np is None:
            layout.label(text="numpy недоступен — каталог отключён", icon='ERROR')
            return

        # галерея: та же самая active_preset, только карточками
        layout.template_icon_view(
            s, "active_preset",
            show_labels=True,
            scale=s.catalog_scale,
            scale_popup=s.catalog_scale,
        )

        row = layout.row(align=True)
        row.prop(s, "catalog_scale")

        box = layout.box()
        box.label(text="Текущие настройки:", icon='TOOL_SETTINGS')
        col = box.column(align=True)
        col.alignment = 'CENTER'
        col.template_icon(icon_value=current_settings_icon_id(s), scale=s.catalog_scale)
        mode_line = ("BRIDGE" if s.build_mode == 'BRIDGE'
                     else f"Г · {s.parts_mode}")
        col.label(text=f"{mode_line} · UV {s.uv_orientation.capitalize()}")
        col.label(text=s.material_name, icon='MATERIAL')


# ------------------------------------------------------------------------
# РЕГИСТРАЦИЯ
# ------------------------------------------------------------------------

classes = (
    CurbSettings,
    CURB_OT_preset_save,
    CURB_OT_preset_update,
    CURB_OT_preset_rename,
    CURB_OT_preset_from_object,
    CURB_OT_preset_delete,
    CURB_OT_preset_reload,
    CURB_OT_preset_restore_factory,
    CURB_OT_preset_export,
    CURB_OT_preset_import,
    CURB_OT_preset_open_folder,
    CURB_MT_preset_specials,
    CURB_OT_update_preview,
    CURB_OT_bake_and_merge,
    CURB_OT_clear_preview,
    CURB_PT_panel,
    CURB_PT_catalog,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.curb_settings = PointerProperty(type=CurbSettings)

    # Коллекция превью для визуального каталога
    _ensure_pcoll()

    # Читаем пресеты с диска (и раскладываем заводские при первом запуске)
    load_presets(force=True)
    invalidate_previews()

    # Добавляем handlers для отслеживания изменений
    if check_selection_change not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(check_selection_change)

    if check_mode_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(check_mode_change)

    if restore_active_preset not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(restore_active_preset)


def unregister():
    # Удаляем handlers
    if check_selection_change in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(check_selection_change)

    if check_mode_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(check_mode_change)

    if restore_active_preset in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(restore_active_preset)

    # Очищаем preview при выгрузке аддона
    cleanup_preview_object()

    # Освобождаем превью каталога
    global _pcoll
    if _pcoll is not None:
        try:
            bpy.utils.previews.remove(_pcoll)
        except Exception as e:
            print(f"[Clean G-Curb] Не удалось освободить превью: {e}")
        _pcoll = None
    _icon_cache.clear()
    _state["current_key"] = None

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.curb_settings


if __name__ == "__main__":
    register()
