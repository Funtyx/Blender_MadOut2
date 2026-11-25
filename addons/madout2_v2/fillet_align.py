# =====================================================
# Ровные сечения для Fillet Sections: точки всех колец дуги берутся
# на одних и тех же углах от её центра, так что каждое сечение идёт
# поперёк полосы. Чистая геометрия в мировой плоскости, без bpy.
# =====================================================

import math

from mathutils import Vector

# How much of the arc at each end eases the rings' own end angles in; the middle stays radial
EASE_ZONE = 0.3


def fit_circle(points):
    """Centre of the circle through points in the plan (least squares), or None if they are a line."""
    n = len(points)
    if n < 3:
        return None
    sx = sum(p.x for p in points) / n
    sy = sum(p.y for p in points) / n
    u = [p.x - sx for p in points]
    v = [p.y - sy for p in points]
    suu = sum(a * a for a in u)
    svv = sum(b * b for b in v)
    suv = sum(a * b for a, b in zip(u, v))
    suuu = sum(a ** 3 for a in u)
    svvv = sum(b ** 3 for b in v)
    suvv = sum(a * b * b for a, b in zip(u, v))
    svuu = sum(b * a * a for a, b in zip(u, v))
    det = suu * svv - suv * suv
    if abs(det) < 1e-12 * max(1.0, suu * svv):
        return None
    uc = (0.5 * (suuu + suvv) * svv - 0.5 * (svvv + svuu) * suv) / det
    vc = (0.5 * (svvv + svuu) * suu - 0.5 * (suuu + suvv) * suv) / det
    return Vector((uc + sx, vc + sy))


def _angles(points, centre):
    """Angles of points about centre, unwrapped so they run on without a jump."""
    out = []
    for p in points:
        a = math.atan2(p.y - centre.y, p.x - centre.x)
        if out:
            while a - out[-1] > math.pi:
                a -= 2.0 * math.pi
            while a - out[-1] < -math.pi:
                a += 2.0 * math.pi
        out.append(a)
    return out


def _ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def _at_angle(dense, angles, target):
    """The point of a dense polyline at an angle about the centre (angles run one way)."""
    up = angles[-1] >= angles[0]
    for i in range(len(dense) - 1):
        a0, a1 = angles[i], angles[i + 1]
        lo, hi = (a0, a1) if up else (a1, a0)
        if lo - 1e-12 <= target <= hi + 1e-12 and abs(a1 - a0) > 1e-12:
            return dense[i].lerp(dense[i + 1], (target - a0) / (a1 - a0))
    return dense[0].copy() if (target < angles[0]) == up else dense[-1].copy()


def align(denses, ref_targets, ref):
    """Section points for every ring of one arc, taken at the reference ring's angles.

    denses: per ring a dense polyline of the arc (world space), from the piece's first point to its
    last; ref_targets: the reference ring's k + 1 section points. Returns per ring k + 1 points, or
    None where it cannot be done honestly (no centre, a ring that does not turn one way about it).

    Why not the equal share of each ring's own length, which is what a strip resample does: on
    Dima's sidewalk the inner kerb ends 2 degrees short of the outer one, and shares spread that
    over every section - 0 degrees off radial at one end, 7 at the other ("должно ровно
    распределиться"). Here the middle is radial and each ring's own end angle is eased in over the
    last EASE_ZONE of the arc at each end, the only place the mismatch exists."""
    centre = fit_circle([p.xy for p in denses[ref]])
    if centre is None:
        return None
    ref_angles = _angles([p.xy for p in ref_targets], centre)
    k = len(ref_targets) - 1
    span = ref_angles[-1] - ref_angles[0]
    if abs(span) < 1e-6:
        return None
    out = []
    for dense in denses:
        ang = _angles([p.xy for p in dense], centre)
        steps = [b - a for a, b in zip(ang, ang[1:])]
        if any(s * span < -1e-9 for s in steps):
            return None  # this ring turns back on itself about the centre
        # same turn as the reference ring, not a whole revolution apart
        shift = round((ang[0] - ref_angles[0]) / (2.0 * math.pi)) * 2.0 * math.pi
        ang = [a - shift for a in ang]
        d0 = ang[0] - ref_angles[0]
        d1 = ang[-1] - ref_angles[-1]
        pts = [dense[0].copy()]
        for j in range(1, k):
            u = (ref_angles[j] - ref_angles[0]) / span
            delta = d0 * (1.0 - _ease(u / EASE_ZONE)) + d1 * _ease((u - (1.0 - EASE_ZONE)) / EASE_ZONE)
            pts.append(_at_angle(dense, ang, ref_angles[j] + delta))
        pts.append(dense[-1].copy())
        # the eased angles must still run one way along the ring
        got = _angles([p.xy for p in pts], centre)
        if any((b - a) * span <= 1e-9 for a, b in zip(got, got[1:])):
            return None
        out.append(pts)
    return out
