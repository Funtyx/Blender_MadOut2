"""Pure Python matching. No vertex index assumptions or color-space conversions."""
import itertools
import math

TRANSFORMS = [(axes, signs) for axes in itertools.permutations(range(3))
              for signs in itertools.product((-1, 1), repeat=3)]


def bounds(points):
    lower = [min(point[i] for point in points) for i in range(3)]
    upper = [max(point[i] for point in points) for i in range(3)]
    center = [(a + b) / 2 for a, b in zip(lower, upper)]
    diagonal = math.sqrt(sum((a - b) ** 2 for a, b in zip(lower, upper)))
    return center, diagonal


def match_colors(samples, corners, tolerance=0.00001, uv_tolerance=0.00001,
                 transform_index=-1):
    """Fit uniform scale, translation and signed axis permutation; fail atomically.

    corners: [(local_position_xyz, uv_xy), ...], one entry per Blender loop.
    All coincident position/UV matches must agree in RGBA. No nearest-color guesses.
    """
    if not samples or not corners:
        raise ValueError("Empty mesh")
    positions = [tuple(sample['position'][axis] for axis in 'xyz') for sample in samples]
    coordinates = [tuple(sample['uv'][axis] for axis in 'xy') for sample in samples]
    colors = [tuple(sample['color'][axis] for axis in 'rgba') for sample in samples]
    for values in positions + coordinates + colors:
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Non-finite values in JSON")
    source_center, source_size = bounds(positions)
    target_center, target_size = bounds([corner[0] for corner in corners])
    if min(source_size, target_size) <= 1e-12:
        raise ValueError("Degenerate mesh bounds")
    scale = source_size / target_size
    epsilon = max(source_size * tolerance, 1e-8)
    grid = {}
    def key(position):
        return tuple(math.floor(value / epsilon) for value in position)
    for i, position in enumerate(positions):
        grid.setdefault(key(position), []).append(i)

    def lookup(corner, transform):
        axes, signs = transform
        position, uv = corner
        mapped = tuple((position[axes[i]] - target_center[axes[i]]) * signs[i] * scale
                       + source_center[i] for i in range(3))
        cell = key(mapped)
        result = None
        for offset in itertools.product((-1, 0, 1), repeat=3):
            for index in grid.get(tuple(cell[i] + offset[i] for i in range(3)), ()):
                if sum((positions[index][i] - mapped[i]) ** 2 for i in range(3)) > epsilon ** 2:
                    continue
                if max(abs(coordinates[index][i] - uv[i]) for i in range(2)) > uv_tolerance:
                    continue
                color = colors[index]
                if result is not None and max(abs(a-b) for a, b in zip(result, color)) > 1e-6:
                    raise ValueError("Conflicting colors at identical position/UV (normal split or overlapping geometry)")
                result = color
        return result

    indices = range(48) if transform_index == -1 else [transform_index]
    if transform_index < -1 or transform_index >= 48:
        raise ValueError("Axis transform must be -1 or 0..47")
    probe = corners[::max(1, len(corners) // 64)]
    accepted = None
    accepted_index = -1
    for index in indices:
        transform = TRANSFORMS[index]
        # A conflicting match in an incorrect orientation must not reject a valid one.
        try:
            if any(lookup(corner, transform) is None for corner in probe):
                continue
            candidate = [lookup(corner, transform) for corner in corners]
            if any(color is None for color in candidate):
                continue
        except ValueError:
            if transform_index != -1:
                raise
            continue
        if accepted is not None and candidate != accepted:
            raise ValueError("Ambiguous axis orientation. Set Axis transform explicitly (0..47); see README")
        accepted, accepted_index = candidate, index
    if accepted is None:
        raise ValueError("No complete position + UV match. Check UV map, modifiers, topology, axes and scale; no colors changed")
    return accepted, accepted_index


def match_topology(samples, indices, corners, faces, tolerance=0.00001):
    """Match face corners using geometry connectivity before UV tie-breaking.

    FBX may repair UVs on ngons. Geometric triangle/edge membership identifies
    the face even then. Ambiguous different RGBA values still reject the import.
    """
    if not samples or not corners or len(indices) % 3:
        raise ValueError("Некорректная геометрия в JSON")
    positions = [tuple(sample['position'][axis] for axis in 'xyz') for sample in samples]
    uvs = [tuple(sample['uv'][axis] for axis in 'xy') for sample in samples]
    colors = [tuple(sample['color'][axis] for axis in 'rgba') for sample in samples]
    if not all(math.isfinite(x) for values in positions + uvs + colors for x in values):
        raise ValueError("JSON содержит некорректные числа")
    if any(not isinstance(i, int) or i < 0 or i >= len(samples) for i in indices):
        raise ValueError("Некорректные индексы треугольников в JSON")
    center, size = bounds(positions)
    target_center, target_size = bounds([corner[0] for corner in corners])
    if min(size, target_size) < 1e-12:
        raise ValueError("Пустая или вырожденная геометрия")
    scale = size / target_size
    epsilon = max(size * tolerance, 1e-8)
    unique = list(dict.fromkeys(positions))
    position_ids = {point: index for index, point in enumerate(unique)}
    vertex_positions = [position_ids[point] for point in positions]
    grid = {}
    def cell(point):
        return tuple(math.floor(x / epsilon) for x in point)
    for index, point in enumerate(unique):
        grid.setdefault(cell(point), []).append(index)
    triangles = [tuple(indices[i:i+3]) for i in range(0, len(indices), 3)]
    incident = [[] for _ in unique]
    for index, triangle in enumerate(triangles):
        for position in set(vertex_positions[v] for v in triangle):
            incident[position].append(index)

    def locate(point, transform):
        axes, signs = transform
        mapped = tuple((point[axes[i]] - target_center[axes[i]]) * signs[i] * scale + center[i]
                       for i in range(3))
        key = cell(mapped)
        best_distance, best = epsilon * epsilon, None
        for offset in itertools.product((-1, 0, 1), repeat=3):
            for index in grid.get(tuple(key[i] + offset[i] for i in range(3)), ()):
                distance = sum((unique[index][i] - mapped[i]) ** 2 for i in range(3))
                if distance <= best_distance:
                    best_distance, best = distance, index
        return best

    accepted, accepted_index = None, -1
    probe = corners[::max(1, len(corners)//64)]
    for transform_index, transform in enumerate(TRANSFORMS):
        if any(locate(corner[0], transform) is None for corner in probe):
            continue
        corner_positions = [locate(corner[0], transform) for corner in corners]
        if any(position is None for position in corner_positions):
            continue
        result = [None] * len(corners)
        valid = True
        for loops in faces:
            face_positions = {corner_positions[loop] for loop in loops}
            possible = {t for position in face_positions for t in incident[position]}
            full = [triangles[t] for t in possible
                    if all(vertex_positions[v] in face_positions for v in triangles[t])]
            for loop in loops:
                position = corner_positions[loop]
                choices = {v for triangle in full for v in triangle if vertex_positions[v] == position}
                if not choices:
                    # FBX can retriangulate a non-planar ngon. Require a shared edge.
                    choices = {v for t in incident[position]
                               if sum(vertex_positions[v] in face_positions for v in triangles[t]) >= 2
                               for v in triangles[t] if vertex_positions[v] == position}
                if not choices:
                    valid = False
                    break
                candidate_colors = [colors[v] for v in choices]
                same = all(max(abs(a-b) for a, b in zip(candidate_colors[0], c)) <= 1e-6
                           for c in candidate_colors)
                if same:
                    result[loop] = candidate_colors[0]
                    continue
                coordinate = corners[loop][1]
                distances = {v: sum((uvs[v][i] - coordinate[i]) ** 2 for i in range(2)) for v in choices}
                minimum = min(distances.values())
                # Large UV changes cannot resolve different colors safely.
                if minimum > 0.002 ** 2:
                    valid = False
                    break
                tied = [colors[v] for v in choices if abs(distances[v]-minimum) <= 1e-14]
                if any(max(abs(a-b) for a, b in zip(tied[0], c)) > 1e-6 for c in tied):
                    valid = False
                    break
                result[loop] = tied[0]
            if not valid:
                break
        if not valid or any(c is None for c in result):
            continue
        if accepted is not None and any(max(abs(a-b) for a, b in zip(c, d)) > 1e-6
                                        for c, d in zip(accepted, result)):
            raise ValueError("Неоднозначные оси: симметричная геометрия с разными цветами")
        accepted, accepted_index = result, transform_index
    if accepted is None:
        raise ValueError("Не удалось однозначно сопоставить грани меша. Проверьте, что JSON относится к выбранному объекту. Цвета не изменены")
    return accepted, accepted_index
