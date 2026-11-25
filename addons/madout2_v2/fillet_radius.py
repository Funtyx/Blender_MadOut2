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


def _layouts(frames, ref, radius):
    """Every ring laid out round ONE centre: the reference ring's arc of this radius fixes it, and
    each ring touches its own two lines at the foot of the perpendicular from it.

    The obvious version - each ring its own arc, its radius changed by the same amount as the
    reference ring's - put each ring's centre wherever its old, hand-drawn radius said, a few
    centimetres apart, and the sections, which run between the rings' points, leant over (Dima:
    "сечения наклоняются"). With one centre every ring's points are taken at the same angles from
    it, so every section points at the centre."""
    fr = frames[ref]
    theta = fr["theta"]
    half = math.tan(abs(theta) / 2.0)
    side = 1.0 if theta > 0 else -1.0
    a0r = fr["corner"] - fr["ta"] * (radius * half)
    centre = a0r + Vector((-fr["ta"].y, fr["ta"].x)) * side * radius
    out = []
    for f in frames:
        a0 = f["pa"] + f["ta"] * (centre - f["pa"]).dot(f["ta"])
        b0 = f["pb"] + f["tb"] * (centre - f["pb"]).dot(f["tb"])
        out.append({
            "pa": f["pa"], "pb": f["pb"], "a0": a0, "b0": b0, "centre": centre,
            "ra": (a0 - centre).length, "rb": (b0 - centre).length, "theta": theta,
            "straight_a": (a0 - f["pa"]).dot(f["ta"]), "straight_b": (f["pb"] - b0).dot(f["tb"]),
        })
    return out


def _short(layouts):
    """The ends where an arc would start past the end of the selection."""
    short = set()
    for lay in layouts:
        if lay["straight_a"] < -REACH_SLACK:
            short.add("a")
        if lay["straight_b"] < -REACH_SLACK:
            short.add("b")
    return short


def reshape(frames, radii, ref, new_radius):
    """The rounding re-laid at new_radius on the reference ring, every ring round the same centre.

    Returns (layouts, error, largest, short): a layout per ring - the straight up to the arc on each
    side (possibly none) and the arc - or an error naming the radius that does fit, and which end
    ran out. A bigger radius pushes the tangent points away from the corner, and past the end of
    the selection there is nothing to lay the arc on; the ends of the selection are the seams with
    the geometry beyond and are never moved."""
    layouts = _layouts(frames, ref, new_radius)
    short = _short(layouts)
    if short:
        # The largest radius that fits, found by halving: it is worked out by the same construction
        # that lays the rings out, so the two cannot disagree
        lo, hi = 0.0, new_radius
        for _ in range(40):
            mid = (lo + hi) / 2.0
            if _short(_layouts(frames, ref, mid)):
                hi = mid
            else:
                lo = mid
        return None, f"Радиус {new_radius:.2f} м не помещается: больше {lo:.2f} м нельзя", lo, short
    least = min(min(lay["ra"], lay["rb"]) for lay in layouts)
    if least < 0.05:
        # every ring is a fixed offset from the reference one, so this is the smallest that keeps
        # the innermost ring a real arc
        smallest = new_radius - least + 0.05
        return None, f"Радиус меньше {smallest:.2f} м нельзя: внутреннее кольцо сойдётся в точку", 0.0, set()
    return layouts, "", new_radius, set()


def lay_out(layout, pieces):
    """The plan points of one ring for the pieces ('a', 'arc', 'b') with the arc in k steps, taken
    at equal angles from the common centre. A ring whose two lines are not quite the same distance
    from it (a hand-drawn band, a mm or two) eases from one radius to the other along the arc."""
    out = [layout["pa"].copy()]
    c = layout["centre"]
    start = layout["a0"] - c
    ang0 = math.atan2(start.y, start.x)
    for kind, k in pieces:
        if kind == "a":
            out.append(layout["a0"].copy())
        elif kind == "b":
            out.append(layout["pb"].copy())
        else:
            for j in range(1, k + 1):
                u = j / k
                ang = ang0 + layout["theta"] * u
                r = layout["ra"] + (layout["rb"] - layout["ra"]) * u
                out.append(c + Vector((math.cos(ang), math.sin(ang))) * r)
            out[-1] = layout["b0"].copy()
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
