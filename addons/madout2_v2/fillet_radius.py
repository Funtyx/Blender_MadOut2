# =====================================================
# Новый радиус скругления для Fillet Sections: дуга строится
# заново касательной к тем же двум прямым, кольца — концентрично
# (ширина бордюра и тротуара не меняется), концы выделения стоят
# на месте. Чистая геометрия в мировой плоскости XY, без bpy.
# =====================================================

import math

from mathutils import Vector

# Two tangent lines closer to parallel than this have no corner to round (a U-turn)
MIN_CORNER = math.radians(5.0)
# How far past the end of the selection a tangent point may land
REACH_SLACK = 0.02
# A straight shorter than this between the selection's end and the new arc is no straight at all
MIN_STRAIGHT = 0.01


def _cross(a, b):
    return a.x * b.y - a.y * b.x


def _rot(v, ang):
    c, s = math.cos(ang), math.sin(ang)
    return Vector((v.x * c - v.y * s, v.x * s + v.y * c))


def _segment_turns(pts):
    """Each segment's own turn in the plan, signed counter-clockwise. A joint turns by the mean of
    its two segments' turns, so the turns are recovered up to one free number, taken as the median
    of what each pair of neighbours says (the same reading fillet_sections uses, in 2D)."""
    n = len(pts) - 1
    t = []
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        d0, d1 = (b - a).xy, (c - b).xy
        if d0.length < 1e-9 or d1.length < 1e-9:
            t.append(0.0)
        else:
            t.append(math.atan2(_cross(d0, d1), d0.dot(d1)))
    a = [0.0]
    for i in range(n - 1):
        a.append(2.0 * t[i] - a[i])
    guesses = sorted((a[i + 1] - a[i]) / 2.0 * (1.0 if i % 2 == 0 else -1.0) for i in range(n - 1))
    c = guesses[len(guesses) // 2] if guesses else 0.0
    return [a[j] + (c if j % 2 == 0 else -c) for j in range(n)]


def frame(pts):
    """The two tangent lines of a ring and where they meet, in the plan.

    The tangent at an end is NOT the end chord: where the selection stops at the tangent point the
    end chord is already half a step into the arc. It is the chord turned back by half its own
    turn. Returns None when the ring is too short to tell or the lines are near parallel."""
    if len(pts) < 3:
        return None
    s = _segment_turns(pts)
    c0 = (pts[1] - pts[0]).xy
    cl = (pts[-1] - pts[-2]).xy
    if c0.length < 1e-9 or cl.length < 1e-9:
        return None
    ta = _rot(c0.normalized(), -s[0] / 2.0)
    tb = _rot(cl.normalized(), s[-1] / 2.0)
    den = _cross(ta, tb)
    if abs(den) < math.sin(MIN_CORNER):
        return None
    pa, pb = pts[0].xy, pts[-1].xy
    d = pb - pa
    la = _cross(d, tb) / den
    lb = -_cross(d, ta) / den
    return {
        "pa": pa, "pb": pb, "ta": ta, "tb": tb,
        "corner": pa + ta * la, "la": la, "lb": lb,
        "theta": math.atan2(den, ta.dot(tb)),
    }


def arc_radius(pts, straight):
    """Radius of a ring's arc from its own chords (2 R sin(s / 2) each), straights left out."""
    s = _segment_turns(pts)
    length = chord = 0.0
    for j in range(len(pts) - 1):
        if straight[j]:
            continue
        length += (pts[j + 1] - pts[j]).length
        chord += 2.0 * math.sin(abs(s[j]) / 2.0)
    return length / chord if chord > 1e-9 else 0.0


def reshape(frames, radii, ref, new_radius):
    """Every ring re-laid on an arc tangent to its own two lines, its radius changed by the same
    amount as the reference ring's, so the rings stay concentric and the band keeps its width.

    Returns (layouts, error, largest): a layout per ring - the straight up to the arc on each side
    (possibly none) and the arc - or an error naming the radius that does fit. A bigger radius
    pushes the tangent points away from the corner, and past the end of the selection there is
    nothing to lay the arc on; the ends of the selection are the seams with the geometry beyond
    and are never moved."""
    theta = frames[ref]["theta"]
    half = math.tan(abs(theta) / 2.0)
    delta = new_radius - radii[ref]
    largest = min(min(f["la"], f["lb"]) / half - r for f, r in zip(frames, radii)) + radii[ref]
    smallest = radii[ref] - min(radii) + 0.05
    # Two centimetres of slack at the tangent points: a rounding drawn by hand is a few millimetres
    # off symmetric, and without it its own current radius did not fit (3.24 m asked, 3.22 allowed)
    if new_radius > largest + REACH_SLACK / half:
        return None, (f"Радиус {new_radius:.2f} м не помещается в выделение: больше {largest:.2f} м нельзя. "
                      f"Чтобы сделать больше, выделите вместе со скруглением и прямые за ним"), largest
    if new_radius < smallest - 1e-4:
        return None, f"Радиус меньше {smallest:.2f} м нельзя: внутреннее кольцо сойдётся в точку", largest
    layouts = []
    for f, r in zip(frames, radii):
        rr = r + delta
        t = rr * half
        a0 = f["corner"] - f["ta"] * t
        b0 = f["corner"] + f["tb"] * t
        side = 1.0 if theta > 0 else -1.0
        normal = Vector((-f["ta"].y, f["ta"].x)) * side
        layouts.append({
            "pa": f["pa"], "pb": f["pb"], "a0": a0, "b0": b0,
            "centre": a0 + normal * rr, "radius": rr, "theta": theta,
            "straight_a": f["la"] - t, "straight_b": f["lb"] - t,
        })
    return layouts, "", largest


def lay_out(layout, pieces):
    """The plan points of one ring for the pieces ('a', 'arc', 'b') with the arc in k steps."""
    out = [layout["pa"].copy()]
    for kind, k in pieces:
        if kind == "a":
            out.append(layout["a0"].copy())
        elif kind == "b":
            out.append(layout["pb"].copy())
        else:
            start = layout["a0"] - layout["centre"]
            for j in range(1, k + 1):
                out.append(layout["centre"] + _rot(start, layout["theta"] * j / k))
    if pieces[-1][0] == "arc":
        out[-1] = layout["pb"].copy()  # no straight after the arc: it ends on the selection's end
    if pieces[0][0] == "arc":
        out[0] = layout["pa"].copy()
    return out


def heights(old, new_xy):
    """Heights for new plan points: each takes the old ring's height at the same share of its
    length, so a rounding on a slope keeps its slope."""
    acc = [0.0]
    for a, b in zip(old, old[1:]):
        acc.append(acc[-1] + (b - a).length)
    nacc = [0.0]
    for a, b in zip(new_xy, new_xy[1:]):
        nacc.append(nacc[-1] + (b - a).length)
    total, ntotal = acc[-1] or 1.0, nacc[-1] or 1.0
    out = []
    j = 0
    for d in nacc:
        s = d / ntotal * total
        while j < len(acc) - 2 and acc[j + 1] < s:
            j += 1
        span = acc[j + 1] - acc[j]
        f = 0.0 if span <= 0 else min(1.0, max(0.0, (s - acc[j]) / span))
        out.append(old[j].z + (old[j + 1].z - old[j].z) * f)
    return out
