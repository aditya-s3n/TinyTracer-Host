"""Scene compiler: turns a scene .txt file into the 512-word TinyTracer SRAM image.

Pipeline (design doc, section 9.2):

    parse -> expand quad/box -> validate -> translate -> camera vectors
    -> bounding volumes -> memory layout -> quantize -> emit words + hex dump

A decoder that reverses layout / quantize / emit ships alongside, so that
encode -> decode gives back the same scene to within one quantization step.

MEMORY MAP
    The word layout follows the HW team's "Rendering Flow" page ("What the SRAM
    holds"), word for word:

        header   w0-8 F, R, U (DIR) | w9-11 sky horizon, top | w12-14 ground A, B
                 | w15 BV count [7:3], log2 spp [2:0] | w16 camera z (POS)
        BV       w0 first object address [15:7], count [6:0] | w1 radius
                 | w2-4 centre z, y, x
        object   w0 blue [15:8], BV index [7:1], T [0] (1 = sphere, 0 = triangle)
                 | w1 red [15:8], green [7:0] | w2 strength Q4.10 [15:2], material [1:0]
        sphere   w3 radius | w4-6 centre z, y, x
        triangle w3-5 edge 2 z, y, x | w6-8 edge 1 z, y, x | w9-11 corner v0 z, y, x

    That page notes the header layout itself is assumed on the HW side ("the docs
    define no header"). Still this module's own choice: rounding (to nearest,
    halves up) and how bounding spheres are fitted (they reproduce the radii the
    page quotes for the demo scene).
"""

import math
from dataclasses import dataclass, field

SRAM_WORDS = 512          # total SRAM size; compile fails if the scene needs more
HEADER_WORDS = 17         # header occupies words 0..16
BV_BASE = 17              # bounding volumes start at word 17
BV_WORDS = 5
SPHERE_WORDS = 7
TRI_WORDS = 12
MAX_BV = 16               # bounding-volume ids 0..15
MAX_BV_OBJECTS = 127      # the object count field is 7 bits
POS_LIMIT = 256           # positions must be within +/-256
MAX_FOV_DEG = 90          # chip camera FOV cap
MAX_STRENGTH = 15.99      # glow strength, Q4.10
IMAGE_SIZE = 512
VALID_SPP = (1, 2, 4, 8, 16, 32)
MATERIALS = ("matte", "mirror", "glass", "glow")

# Fixed-point formats as (bits before the point, bits after, signed).
POS = (9, 7, True)        # positions, radii, edges: +/-256, step 1/128
DIR = (2, 14, True)       # camera vectors: +/-2, step 1/16384
GLOW = (4, 10, False)     # glow strength: 0..15.99, step 1/1024
POS_STEP = 1 / 128

# Object words w0..w2 (Rendering Flow, "What the SRAM holds").
TYPE_SPHERE, TYPE_TRI = 1, 0      # w0 bit 0
BV_INDEX_SHIFT = 1                # w0 bits 7:1; the chip never reads it
STRENGTH_SHIFT = 2                # w2 bits 15:2
MATERIAL_MASK = 0x3               # w2 bits 1:0, in MATERIALS order
OBJECT_WORDS = {"sphere": SPHERE_WORDS, "tri": TRI_WORDS}

# A bounding sphere is this much bigger than the tightest fit. A bounce starts
# 0.031 off the surface it left (the chip's "push off") and must still be inside
# its own volume. 0.05 reproduces the radii Rendering Flow quotes for the demo scene.
BV_MARGIN = 0.05
# Distances along a ray are POS numbers, so the chip cannot measure past this.
MAX_DISTANCE = 256

# Automatic bounding volumes: aim for about this many objects in each.
AUTO_BV_TARGET = 4


class SceneError(Exception):
    """A scene rule was broken. `line` is the source line (or None) and `rule` says what is wrong."""

    def __init__(self, line, rule):
        super().__init__(f"line {line}: {rule}" if line else rule)
        self.line = line
        self.rule = rule


@dataclass
class SceneObject:
    """One sphere or triangle. Quads and boxes are already split into triangles."""

    kind: str                       # "sphere" or "tri"
    points: list                    # sphere: [centre]; tri: [corner 0, corner 1, corner 2]
    radius: float = 0.0             # spheres only
    material: str = "matte"
    colour: tuple = (255, 255, 255)
    strength: float = 0.0           # glow only
    bv: int | None = None           # bounding volume id, or None for automatic
    line: int | None = None         # source line, for error messages
    source: str = ""                # the keyword it came from: sphere, tri, quad or box


@dataclass
class BoundingVolume:
    """A sphere around a group of objects. The chip skips the whole group when a ray misses it."""

    centre: tuple
    radius: float
    objects: list = field(default_factory=list)
    first: int = 0                  # SRAM address of its first object, set by layout_memory


@dataclass
class Scene:
    """A parsed scene. Header fields are None until their line has been read."""

    image: int = IMAGE_SIZE
    spp: int | None = None
    camera: tuple | None = None     # x, y, z, yaw (deg from +x), pitch (deg), field of view (deg)
    sky: tuple | None = None        # (horizon rgb, top rgb)
    ground: tuple | None = None     # checker colour A
    checker: tuple | None = None    # checker colour B
    objects: list = field(default_factory=list)
    lines: dict = field(default_factory=dict)     # keyword -> source line
    # Filled in by the compiler and the decoder:
    vectors: tuple | None = None    # camera F, R, U
    bvs: list = field(default_factory=list)


@dataclass
class Compiled:
    """Everything one successful compile produces."""

    words: list                     # the SRAM image, one 16-bit int per address
    fields: list                    # (field name, decoded value) for each word, for the hex dump
    scene: Scene                    # the scene as stored: camera at x = y = 0, objects in memory order
    warnings: list


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def load_scene(path):
    """Read a scene .txt file and parse it. Raises SceneError for the first broken line."""
    with open(path, encoding="utf-8") as f:
        scene, errors = parse_txt(f.read())
    if errors:
        raise SceneError(errors[0]["line"], errors[0]["rule"])
    return scene


def parse_txt(text):
    """Parse the line-based scene format (section 9.1). Returns (scene, errors).

    Blank lines and `#` comments are skipped. Every bad line adds one
    {"line", "rule"} entry to errors and parsing carries on, so one compile
    reports every problem. Quads and boxes are split into triangles here.
    """
    scene = Scene()
    errors = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        fields = raw.split("#", 1)[0].split()
        if not fields:
            continue
        try:
            parse_line(scene, fields[0], fields[1:], line_no)
        except SceneError as e:
            errors.append({"line": e.line, "rule": e.rule})
    return scene, errors


def parse_line(scene, keyword, fields, line_no):
    """Parse one line into the scene. Raises SceneError on a bad keyword, field count or number."""
    if keyword in ("sphere", "tri", "quad", "box"):
        scene.objects.extend(parse_object(keyword, fields, line_no))
        return
    if keyword not in ("image", "spp", "camera", "sky", "ground", "checker"):
        raise SceneError(line_no, f"unknown keyword {keyword!r}")
    if keyword in scene.lines:
        raise SceneError(line_no, f"{keyword} is already set on line {scene.lines[keyword]}")
    scene.lines[keyword] = line_no

    counts = {"image": 1, "spp": 1, "camera": 6, "sky": 6, "ground": 3, "checker": 3}
    if len(fields) != counts[keyword]:
        raise SceneError(line_no, f"{keyword} needs {counts[keyword]} numbers, got {len(fields)}")
    numbers = [parse_number(f, line_no) for f in fields]

    if keyword in ("image", "spp"):
        if numbers[0] != int(numbers[0]):
            raise SceneError(line_no, f"{keyword} must be a whole number")
        setattr(scene, keyword, int(numbers[0]))
    elif keyword == "camera":
        scene.camera = tuple(numbers)
    elif keyword == "sky":
        scene.sky = (parse_colour(numbers[:3], line_no), parse_colour(numbers[3:], line_no))
    else:
        setattr(scene, keyword, parse_colour(numbers, line_no))


def parse_number(text, line_no):
    """A field as a float. Raises SceneError for anything that is not a finite number."""
    try:
        value = float(text)
    except ValueError:
        raise SceneError(line_no, f"{text!r} is not a number") from None
    if not math.isfinite(value):
        raise SceneError(line_no, f"{text!r} is not a finite number")
    return value


def parse_colour(numbers, line_no):
    """Three whole numbers 0..255 as an (r, g, b) tuple."""
    for v in numbers:
        if v != int(v) or not 0 <= v <= 255:
            raise SceneError(line_no, f"colour value {v:g} must be a whole number from 0 to 255")
    return tuple(int(v) for v in numbers)


def parse_object(keyword, fields, line_no):
    """Parse a sphere, tri, quad or box line and return its spheres / triangles."""
    n_geometry = {"sphere": 4, "tri": 9, "quad": 12, "box": 6}[keyword]
    if len(fields) <= n_geometry or fields[n_geometry] not in MATERIALS:
        raise SceneError(line_no, f"{keyword} needs {n_geometry} numbers, then a material "
                                  f"({', '.join(MATERIALS)})")
    g = [parse_number(f, line_no) for f in fields[:n_geometry]]
    material, colour, strength, bv = parse_trailing_fields(fields[n_geometry:], line_no)
    common = dict(material=material, colour=colour, strength=strength, bv=bv, line=line_no, source=keyword)

    if keyword == "sphere":
        return [SceneObject("sphere", [tuple(g[:3])], radius=g[3], **common)]
    corners = [tuple(g[i:i + 3]) for i in range(0, n_geometry, 3)]
    triangles = {"tri": lambda c: [c], "quad": expand_quad, "box": expand_box}[keyword](corners)
    return [SceneObject("tri", list(t), **common) for t in triangles]


def parse_trailing_fields(fields, line_no):
    """Parse the fields every object line ends with: material R G B [strength] [bv].

    Returns (material, colour, strength, bv). A missing strength is 0 and a
    missing bv is None, which means "pick a bounding volume automatically".
    """
    material, rest = fields[0], fields[1:]
    if not 3 <= len(rest) <= 5:
        raise SceneError(line_no, f"after {material} expected R G B [strength] [bv], got {len(rest)} values")
    numbers = [parse_number(f, line_no) for f in rest]
    colour = parse_colour(numbers[:3], line_no)
    strength = numbers[3] if len(numbers) > 3 else 0.0
    bv = None
    if len(numbers) > 4:
        if numbers[4] != int(numbers[4]):
            raise SceneError(line_no, f"bv {numbers[4]:g} must be a whole number")
        bv = int(numbers[4])
    return material, colour, strength, bv


# ---------------------------------------------------------------------------
# Shape expansion
# ---------------------------------------------------------------------------

def expand_quad(corners):
    """Split a quad (four corners in order around its edge) into 2 triangles."""
    a, b, c, d = corners
    return [(a, b, c), (a, c, d)]


def expand_box(corners):
    """Turn a box (two opposite corners) into 12 triangles, 2 per face."""
    a, b = corners

    def corner(i):
        # Corner i takes x from b when bit 0 is set, y for bit 1, z for bit 2.
        return tuple(b[axis] if i >> axis & 1 else a[axis] for axis in range(3))

    faces = ((0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5))
    triangles = []
    for face in faces:
        triangles += expand_quad([corner(i) for i in face])
    return triangles


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_scene(scene):
    """Check the scene against the chip's rules (section 9.2) and return every error found.

    Each error is {"line", "rule"}. Positions are checked later, after the
    scene has been moved to put the camera at x = 0, y = 0.
    """
    errors = []

    def error(line, rule):
        errors.append({"line": line, "rule": rule})

    for keyword in ("spp", "camera", "sky", "ground", "checker"):
        if keyword not in scene.lines:
            error(None, f"the scene has no {keyword} line")

    if scene.image != IMAGE_SIZE:
        error(scene.lines.get("image"), f"image {scene.image} must be {IMAGE_SIZE}")
    if scene.spp is not None and scene.spp not in VALID_SPP:
        error(scene.lines["spp"], f"spp {scene.spp} must be 1, 2, 4, 8, 16 or 32")
    if scene.camera is not None:
        fov = scene.camera[5]
        if fov > MAX_FOV_DEG:
            error(scene.lines["camera"], f"field of view {fov:g} is over the limit of {MAX_FOV_DEG}")
        elif fov <= 0:
            error(scene.lines["camera"], f"field of view {fov:g} must be above 0")
        if scene.camera[2] <= 0:
            error(scene.lines["camera"], f"camera is at z = {scene.camera[2]:g}; it must be above the ground")
        elif scene.camera[2] >= POS_LIMIT:
            error(scene.lines["camera"], f"camera is at z = {scene.camera[2]:g}; it must be below {POS_LIMIT}")

    for obj in scene.objects:
        for rule in check_object(obj):
            error(obj.line, rule)
    return errors


def check_object(obj):
    """Return the rules one sphere or triangle breaks (not counting its position range)."""
    rules = []
    if obj.material == "glass" and obj.kind != "sphere":
        rules.append("glass is for spheres only")
    if not 0 <= obj.strength <= MAX_STRENGTH:
        rules.append(f"strength {obj.strength:g} must be from 0 to {MAX_STRENGTH}")
    if obj.bv is not None and not 0 <= obj.bv < MAX_BV:
        rules.append(f"bv {obj.bv} must be from 0 to {MAX_BV - 1}")

    if obj.kind == "sphere":
        if obj.radius <= 0:
            rules.append(f"sphere radius {obj.radius:g} must be above 0")
        elif obj.points[0][2] - obj.radius < 0:
            rules.append(f"sphere bottom is at z = {obj.points[0][2] - obj.radius:g}; objects must stay at z >= 0")
    else:
        lowest = min(p[2] for p in obj.points)
        if lowest < 0:
            rules.append(f"{obj.source} corner is at z = {lowest:g}; objects must stay at z >= 0")
        # The chip sizes a triangle from its largest edge part; with none it has nothing to go on.
        v0, v1, v2 = ([round_to_step(c) for c in p] for p in obj.points)
        e1, e2 = [b - a for a, b in zip(v0, v1)], [b - a for a, b in zip(v0, v2)]
        cross = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        if not any(cross) and obj.source in ("tri", "quad"):
            rules.append(f"{obj.source} has a triangle with no area (its corners are in a line)")
    return rules


def check_positions(scene):
    """After translation: every sphere centre and triangle corner must be within +/-256."""
    errors = []
    for obj in scene.objects:
        what = "sphere centre" if obj.kind == "sphere" else f"{obj.source} corner"
        for point in obj.points:
            bad = [(axis, v) for axis, v in zip("xyz", point) if abs(v) > POS_LIMIT]
            if bad:
                axis, v = bad[0]
                errors.append({"line": obj.line, "rule": f"{what} {axis} = {v:g} is outside +-{POS_LIMIT} "
                                                         f"after moving the camera to x = 0"})
                break
    return errors


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

def translate_to_camera_origin(scene):
    """Shift every object so the camera sits at x = 0, y = 0 (modifies the scene).

    The chip's camera is fixed on the z axis, so the host moves the world
    instead. The camera height is kept and becomes CAM_Z.
    """
    cx, cy, cz, yaw, pitch, fov = scene.camera
    for obj in scene.objects:
        obj.points = [(x - cx, y - cy, z) for x, y, z in obj.points]
    scene.camera = (0.0, 0.0, cz, yaw, pitch, fov)


def compute_camera_vectors(yaw_deg, pitch_deg, fov_deg):
    """The chip's camera basis from yaw (from +x), pitch and field of view (section 6.4).

    Returns (F, R, U): F is the unit forward vector; R and U point to the right
    and top edge of the image and have length tan(fov / 2). The designer's
    viewport uses the same vectors, so its framing matches the chip's render.
    """
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    half = math.tan(math.radians(fov_deg) / 2)
    f = (cp * cy, cp * sy, sp)
    r = (sy * half, -cy * half, 0.0)
    # U = R x F, which also has length tan(fov / 2) because R and F are at right angles.
    u = (r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0])
    return f, r, u


def camera_from_vectors(f, r):
    """Reverse compute_camera_vectors: (yaw, pitch, fov) in degrees from F and R."""
    yaw = math.degrees(math.atan2(f[1], f[0])) % 360
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, f[2]))))
    fov = 2 * math.degrees(math.atan(math.sqrt(sum(c * c for c in r))))
    return yaw, pitch, fov


# ---------------------------------------------------------------------------
# Bounding volumes
# ---------------------------------------------------------------------------

def object_centre(obj):
    """The point used to cluster an object: a sphere's centre or a triangle's centroid."""
    return tuple(sum(p[i] for p in obj.points) / len(obj.points) for i in range(3))


def group_by_bv(objects):
    """Split objects into ({bv id: objects}, objects with no bv id)."""
    groups, unassigned = {}, []
    for obj in objects:
        if obj.bv is None:
            unassigned.append(obj)
        else:
            groups.setdefault(obj.bv, []).append(obj)
    return groups, unassigned


def cluster_unassigned(objects, free_ids, k=None):
    """Group objects that have no bv id with k-means on their centres.

    Uses at most `k` of the ids in `free_ids` (default: about AUTO_BV_TARGET
    objects per volume). Returns {bv id: objects}. Deterministic: the same
    scene always gives the same groups.
    """
    if not objects:
        return {}
    if not free_ids:
        raise SceneError(objects[0].line, f"no bounding volume is free for objects without a bv number "
                                          f"(all {MAX_BV} are used)")
    if k is None:
        k = math.ceil(len(objects) / AUTO_BV_TARGET)
    k = max(1, min(k, len(free_ids), len(objects)))
    centres = [object_centre(o) for o in objects]

    def dist2(a, b):
        return sum((x - y) ** 2 for x, y in zip(a, b))

    # Seeds: the first object, then repeatedly the object farthest from every seed so far.
    means = [centres[0]]
    while len(means) < k:
        means.append(max(centres, key=lambda c: min(dist2(c, m) for m in means)))

    assignment = None
    for _ in range(50):
        new = [min(range(k), key=lambda j: dist2(c, means[j])) for c in centres]
        if new == assignment:
            break
        assignment = new
        for j in range(k):
            members = [c for c, a in zip(centres, assignment) if a == j]
            if members:
                means[j] = tuple(sum(c[i] for c in members) / len(members) for i in range(3))

    clusters = {}
    for obj, a in zip(objects, assignment):
        clusters.setdefault(a, []).append(obj)
    # Hand out ids in the order the clusters first appear in the file.
    return {free_ids[n]: members for n, members in enumerate(clusters.values())}


def fit_bounding_sphere(objects):
    """A sphere (centre, radius) that fully contains every object in the group.

    The centre is the middle of the group's bounding box, already rounded to
    the position grid so the stored volume is exactly the one that was fitted.
    The radius is rounded up to the grid. Full containment matters: the chip
    assumes a bounce starts inside its own volume.
    """
    lows, highs = [], []
    for obj in objects:
        pad = obj.radius if obj.kind == "sphere" else 0.0
        for p in obj.points:
            lows.append(tuple(v - pad for v in p))
            highs.append(tuple(v + pad for v in p))
    centre = tuple(round_to_step((min(p[i] for p in lows) + max(p[i] for p in highs)) / 2) for i in range(3))

    radius = 0.0
    for obj in objects:
        pad = obj.radius if obj.kind == "sphere" else 0.0
        for p in obj.points:
            radius = max(radius, math.dist(centre, p) + pad)
    return centre, math.ceil((radius + BV_MARGIN) / POS_STEP) * POS_STEP


def build_bounding_volumes(objects, auto_k=None):
    """Group the objects and fit a sphere to each group. Returns the volumes in bv id order.

    Objects with a bv number keep it; the rest are clustered into the unused
    ids. Raises SceneError when a volume holds more than 127 objects.
    """
    groups, unassigned = group_by_bv(objects)
    free_ids = [i for i in range(MAX_BV) if i not in groups]
    groups.update(cluster_unassigned(unassigned, free_ids, auto_k))

    volumes = []
    for bv_id in sorted(groups):
        members = groups[bv_id]
        if len(members) > MAX_BV_OBJECTS:
            raise SceneError(None, f"bv {bv_id} has {len(members)} objects; the limit is {MAX_BV_OBJECTS}")
        centre, radius = fit_bounding_sphere(members)
        volumes.append(BoundingVolume(centre, radius, members))
    return volumes


# ---------------------------------------------------------------------------
# Memory layout
# ---------------------------------------------------------------------------

def object_word_count(obj):
    """How many SRAM words a sphere (7) or triangle (12) takes."""
    return OBJECT_WORDS[obj.kind]


def count_words(objects, n_bvs):
    """Total words for these objects in `n_bvs` bounding volumes: header + BVs + objects."""
    return HEADER_WORDS + BV_WORDS * n_bvs + sum(object_word_count(o) for o in objects)


def budget_error(objects, n_bvs):
    """The "does not fit" message, with where the words went."""
    spheres = sum(SPHERE_WORDS for o in objects if o.kind == "sphere")
    triangles = sum(TRI_WORDS for o in objects if o.kind == "tri")
    return (f"scene needs {count_words(objects, n_bvs)} words; the chip has {SRAM_WORDS} "
            f"(spheres {spheres}, triangles {triangles}, BVs {BV_WORDS * n_bvs}, header {HEADER_WORDS})")


def layout_memory(bvs):
    """Give every bounding volume the SRAM address of its first object.

    Layout: header at 0..16, BV records from 17, then the objects grouped by
    BV in BV order. Returns the total number of words used.
    """
    address = BV_BASE + BV_WORDS * len(bvs)
    for bv in bvs:
        bv.first = address
        address += sum(object_word_count(o) for o in bv.objects)
    return address


# ---------------------------------------------------------------------------
# Quantization
# ---------------------------------------------------------------------------

def round_half_up(x):
    """Round to the nearest whole number; exact halves go up (towards +infinity)."""
    return math.floor(x + 0.5)


def round_to_step(value, step=POS_STEP):
    """Round a position to the Q9.7 grid."""
    return round_half_up(value / step) * step


def to_fixed(value, fmt, name, warnings):
    """Quantize a float to a 16-bit word in the given fixed-point format.

    Values outside the format's range saturate to the nearest limit and add a
    message that names the field to `warnings`. The compiler treats those as
    errors: a saturated edge or radius would silently draw a different shape.
    """
    int_bits, frac_bits, signed = fmt
    bits = int_bits + frac_bits
    low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    raw = round_half_up(value * (1 << frac_bits))
    if not low <= raw <= high:
        raw = max(low, min(high, raw))
        warnings.append(f"{name} = {value:g} does not fit the chip's number range "
                        f"({low / (1 << frac_bits):g} to {high / (1 << frac_bits):g})")
    return raw & 0xFFFF


def from_fixed(word, fmt):
    """Convert a 16-bit word back to a float in the given fixed-point format."""
    int_bits, frac_bits, signed = fmt
    bits = int_bits + frac_bits
    raw = word & ((1 << bits) - 1)
    if signed and raw >= 1 << (bits - 1):
        raw -= 1 << bits
    return raw / (1 << frac_bits)


def pack_colours(a, b):
    """Two 24-bit colours as three words: aR aG | aB bR | bG bB."""
    r1, g1, b1 = a
    r2, g2, b2 = b
    return [r1 << 8 | g1, b1 << 8 | r2, g2 << 8 | b2]


def unpack_colours(words):
    """Reverse pack_colours: three words to two (r, g, b) tuples."""
    w0, w1, w2 = words
    return (w0 >> 8, w0 & 0xFF, w1 >> 8), (w1 & 0xFF, w2 >> 8, w2 & 0xFF)


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

class _Emitter:
    """Collects words together with the field name and value shown beside each in the hex dump."""

    def __init__(self):
        self.words = []
        self.fields = []
        self.warnings = []

    def raw(self, word, name, value):
        self.words.append(word & 0xFFFF)
        self.fields.append((name, value))

    def fixed(self, value, fmt, name, unit):
        word = to_fixed(value, fmt, name, self.warnings)
        self.raw(word, name, f"{from_fixed(word, fmt):.4f}  {unit}")

    def pos(self, value, name):
        self.fixed(value, POS, name, "POS")


def encode_header(out, scene, vectors, n_bvs):
    """Header words 0..16: F, R, U, sky colours, ground colours, BV count | log2 spp, CAM_Z."""
    for label, vector in zip("FRU", vectors):
        for axis, value in zip("xyz", vector):
            out.fixed(value, DIR, f"hdr {label}.{axis}", "DIR")

    for name, (a, b) in (("sky horizon/top", scene.sky), ("ground A/B", (scene.ground, scene.checker))):
        value = f"{a[0]} {a[1]} {a[2]} / {b[0]} {b[1]} {b[2]}"
        for word in pack_colours(a, b):
            out.raw(word, f"hdr {name}", value)

    log2_spp = VALID_SPP.index(scene.spp)
    out.raw(n_bvs << 3 | log2_spp, "hdr bv_count|log2spp", f"{n_bvs} | {log2_spp}")
    out.pos(scene.camera[2], "hdr CAM_Z")


def encode_bv(out, index, bv):
    """One bounding volume, 5 words: first address | count, radius, centre z, y, x."""
    name = f"bv{index}"
    out.raw(bv.first << 7 | len(bv.objects), f"{name} first|count",
            f"first object at {bv.first:03X}, {len(bv.objects)} objects")
    out.pos(bv.radius, f"{name} radius")
    for axis in (2, 1, 0):
        out.pos(bv.centre[axis], f"{name} centre {'xyz'[axis]}")


def encode_material(out, obj, name, bv_index):
    """Object words 0..2: blue | BV index | type bit, red | green, glow strength | material."""
    type_bit = TYPE_SPHERE if obj.kind == "sphere" else TYPE_TRI
    r, g, b = obj.colour
    strength = to_fixed(obj.strength if obj.material == "glow" else 0.0, GLOW, f"{name} strength", out.warnings)
    summary = f"{obj.material} {r} {g} {b}"
    out.raw(b << 8 | bv_index << BV_INDEX_SHIFT | type_bit, f"{name} B|bv|type", summary)
    out.raw(r << 8 | g, f"{name} R|G", summary)
    out.raw(strength << STRENGTH_SHIFT | MATERIALS.index(obj.material), f"{name} strength|mat",
            f"{from_fixed(strength, GLOW):.4f} | {obj.material}")


def encode_sphere(out, obj, name, bv_index):
    """A sphere, 7 words: material words, then radius and centre z, y, x."""
    encode_material(out, obj, name, bv_index)
    out.pos(obj.radius, f"{name} radius")
    for axis in (2, 1, 0):
        out.pos(obj.points[0][axis], f"{name} centre {'xyz'[axis]}")


def encode_triangle(out, obj, name, bv_index):
    """A triangle, 12 words: material words, then e2, e1 and v0, each as z, y, x.

    e1 and e2 are the edges from corner 0 to corners 1 and 2. They are taken
    from the corners after rounding, so the stored triangle is exactly the one
    a decoder rebuilds.
    """
    encode_material(out, obj, name, bv_index)
    v0, v1, v2 = ([round_to_step(c) for c in p] for p in obj.points)
    e1 = [b - a for a, b in zip(v0, v1)]
    e2 = [b - a for a, b in zip(v0, v2)]
    for label, vector in (("e2", e2), ("e1", e1), ("v0", v0)):
        for axis in (2, 1, 0):
            out.pos(vector[axis], f"{name} {label} {'xyz'[axis]}")


def emit_words(scene, vectors, bvs):
    """Encode the whole image. Returns (words, fields, warnings)."""
    out = _Emitter()
    encode_header(out, scene, vectors, len(bvs))
    for i, bv in enumerate(bvs):
        encode_bv(out, i, bv)
    for i, bv in enumerate(bvs):
        for obj in bv.objects:
            name = f"obj {obj.kind} bv{i}"
            (encode_sphere if obj.kind == "sphere" else encode_triangle)(out, obj, name, i)
    return out.words, out.fields, out.warnings


def hex_dump(words, fields=None, title="", warnings=()):
    """A readable dump of the image (section 9.4): address, word, field name and decoded value.

    Without `fields` (a raw dump from the RTL, say) only address and word are written.
    """
    lines = ["# tthost sram image v1"]
    if title:
        lines.append(f"# scene   {title}")
    lines.append(f"# words   {len(words)} / {SRAM_WORDS}")
    lines.append("# addr  word  field                      value")
    for address, word in enumerate(words):
        line = f"  {address:03X}   {word:04X}"
        if fields:
            name, value = fields[address]
            line += f"  {name:<25}  {value}"
        lines.append(line.rstrip())
    lines.append("# warnings: " + ("; ".join(warnings) if warnings else "none"))
    return "\n".join(lines) + "\n"


def parse_hex_dump(text):
    """Read the words back out of a hex dump (or any "addr word" listing). Returns the word list."""
    words = {}
    for line_no, raw in enumerate(text.splitlines(), 1):
        fields = raw.split("#", 1)[0].split()
        if not fields:
            continue
        try:
            address, word = int(fields[0].rstrip(":"), 16), int(fields[1], 16)
        except (ValueError, IndexError):
            raise SceneError(line_no, "expected an address and a word in hex") from None
        if not (0 <= address < SRAM_WORDS and 0 <= word <= 0xFFFF):
            raise SceneError(line_no, f"address {address:X} or word {word:X} is out of range")
        words[address] = word
    return [words.get(a, 0) for a in range(max(words) + 1)] if words else []


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------

def compile_checked(text):
    """Run the whole pipeline. Returns (Compiled or None, errors).

    Never raises for a bad scene: every broken rule becomes one {"line", "rule"}
    entry, sorted by line.
    """
    scene, errors = parse_txt(text)
    errors += validate_scene(scene)
    if not errors:
        translate_to_camera_origin(scene)
        errors += check_positions(scene)
    if errors:
        return None, sorted(errors, key=lambda e: (e["line"] is None, e["line"] or 0))

    try:
        bvs = build_bounding_volumes(scene.objects)
        # Automatic volumes cost 5 words each: if the scene does not fit, try fewer of them.
        unassigned = sum(o.bv is None for o in scene.objects)
        auto_k = math.ceil(unassigned / AUTO_BV_TARGET) if unassigned else 0
        while count_words(scene.objects, len(bvs)) > SRAM_WORDS and auto_k > 1:
            auto_k -= 1
            bvs = build_bounding_volumes(scene.objects, auto_k)
    except SceneError as e:
        return None, [{"line": e.line, "rule": e.rule}]
    if count_words(scene.objects, len(bvs)) > SRAM_WORDS:
        return None, [{"line": None, "rule": budget_error(scene.objects, len(bvs))}]

    layout_memory(bvs)
    vectors = compute_camera_vectors(*scene.camera[3:])
    words, fields, saturated = emit_words(scene, vectors, bvs)
    if saturated:
        return None, [{"line": None, "rule": message} for message in saturated]
    warnings = distance_warnings(scene)

    # Keep the scene the way it is stored: objects in memory order, each with its final bv.
    scene.objects = []
    for i, bv in enumerate(bvs):
        for obj in bv.objects:
            obj.bv = i
            scene.objects.append(obj)
    scene.vectors = vectors
    scene.bvs = bvs
    return Compiled(words, fields, scene, warnings), []


def distance_warnings(scene):
    """Warn about objects the chip cannot measure the distance to.

    A ray's distance t is a POS number, so it stops at 256. An object can be
    inside the +-256 position range and still be farther than that from the camera.
    """
    eye = (0.0, 0.0, scene.camera[2])
    farthest = {}   # (line, keyword) -> distance; a box is 12 triangles but one warning
    for obj in scene.objects:
        far = max(math.dist(eye, p) for p in obj.points) + obj.radius
        key = (obj.line, obj.source)
        farthest[key] = max(far, farthest.get(key, 0.0))

    warnings = []
    for (line, source), far in farthest.items():
        if far > MAX_DISTANCE:
            where = f"line {line}: " if line else ""
            warnings.append(f"{where}{source} reaches {far:.0f} from the camera; the chip measures "
                            f"distances up to {MAX_DISTANCE}, so it may be cut off or missing")
    return warnings


def compile_text(text, filename="<scene>"):
    """Compile scene text. Returns (words, report); words is None when there are errors.

    report has words, bvs, spheres, triangles, errors, warnings and hex (the
    sram.hex text). This is the run controller's compiler hook and what the
    "checked" IPC message is built from.
    """
    compiled, errors = compile_checked(text)
    if compiled is None:
        return None, {"words": 0, "bvs": 0, "spheres": 0, "triangles": 0,
                      "errors": errors, "warnings": [], "hex": ""}
    objects = compiled.scene.objects
    return compiled.words, {
        "words": len(compiled.words),
        "bvs": len(compiled.scene.bvs),
        "spheres": sum(o.kind == "sphere" for o in objects),
        "triangles": sum(o.kind == "tri" for o in objects),
        "errors": [],
        "warnings": compiled.warnings,
        "hex": hex_dump(compiled.words, compiled.fields, filename, compiled.warnings),
    }


def compile_scene(path):
    """Compile a scene file. Returns (words, warnings, metadata); raises SceneError on the first error."""
    with open(path, encoding="utf-8") as f:
        words, report = compile_text(f.read(), str(path))
    if words is None:
        first = report["errors"][0]
        raise SceneError(first["line"], first["rule"])
    return words, report["warnings"], {k: report[k] for k in ("words", "bvs", "spheres", "triangles")}


# ---------------------------------------------------------------------------
# Decoder (reverses layout / quantize / emit)
# ---------------------------------------------------------------------------

def decode_words(words):
    """Rebuild a scene from an SRAM image. Raises ValueError if the image is not a valid one.

    The result is the scene as the chip sees it: camera at x = 0, y = 0, quads
    and boxes as plain triangles, every object with its bounding volume number.
    Used for the encode -> decode round-trip test, by `tthost decode` to read a
    memory dump, and by the stand-in chip to know what to draw.
    """
    if len(words) < HEADER_WORDS:
        raise ValueError(f"image has {len(words)} words; the header alone is {HEADER_WORDS}")

    def pos(address):
        return from_fixed(words[address], POS)

    f, r, u = (tuple(from_fixed(words[base + i], DIR) for i in range(3)) for base in (0, 3, 6))
    n_bvs, log2_spp = words[15] >> 3, words[15] & 0x7
    if n_bvs > MAX_BV or log2_spp >= len(VALID_SPP):
        raise ValueError(f"header word 15 is {words[15]:04X}: {n_bvs} BVs, log2 spp {log2_spp}")
    if len(words) < BV_BASE + BV_WORDS * n_bvs:
        raise ValueError(f"image ends inside the {n_bvs} BV records")

    scene = Scene(spp=VALID_SPP[log2_spp], vectors=(f, r, u))
    scene.sky = unpack_colours(words[9:12])
    scene.ground, scene.checker = unpack_colours(words[12:15])
    scene.camera = (0.0, 0.0, pos(16), *camera_from_vectors(f, r))

    for i in range(n_bvs):
        base = BV_BASE + BV_WORDS * i
        first, count = words[base] >> 7, words[base] & 0x7F
        bv = BoundingVolume((pos(base + 4), pos(base + 3), pos(base + 2)), pos(base + 1), first=first)
        address = first
        for _ in range(count):
            is_tri = address < len(words) and words[address] & 1 == TYPE_TRI
            size = TRI_WORDS if is_tri else SPHERE_WORDS
            if address + size > len(words):
                raise ValueError(f"bv {i}: object at {address:03X} runs past the end of the image")
            obj = decode_object(words, address, i)
            address += size
            bv.objects.append(obj)
            scene.objects.append(obj)
        scene.bvs.append(bv)
    return scene


def decode_object(words, address, bv):
    """Decode the sphere or triangle that starts at `address`."""
    w0, w1, w2 = words[address:address + 3]
    material = MATERIALS[w2 & MATERIAL_MASK]
    colour = (w1 >> 8, w1 & 0xFF, w0 >> 8)
    strength = from_fixed(w2 >> STRENGTH_SHIFT, GLOW)

    def xyz(base):   # stored as z, y, x
        return tuple(from_fixed(words[base + i], POS) for i in (2, 1, 0))

    if w0 & 1 == TYPE_SPHERE:
        return SceneObject("sphere", [xyz(address + 4)], radius=from_fixed(words[address + 3], POS),
                           material=material, colour=colour, strength=strength, bv=bv, source="sphere")
    e2, e1, v0 = xyz(address + 3), xyz(address + 6), xyz(address + 9)
    corners = [v0, tuple(a + b for a, b in zip(v0, e1)), tuple(a + b for a, b in zip(v0, e2))]
    return SceneObject("tri", corners, material=material, colour=colour, strength=strength, bv=bv, source="tri")


def scene_to_text(scene):
    """Write a scene (usually a decoded one) back out in the .txt format."""
    def nums(values):
        return " ".join(f"{v:.4f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v) for v in values)

    lines = [
        f"image   {scene.image}",
        f"spp     {scene.spp}",
        f"camera  {nums(scene.camera)}",
        f"sky     {nums(scene.sky[0])}  {nums(scene.sky[1])}",
        f"ground  {nums(scene.ground)}",
        f"checker {nums(scene.checker)}",
        "",
    ]
    for obj in scene.objects:
        geometry = "  ".join(nums(p) for p in obj.points)
        if obj.kind == "sphere":
            geometry += f"  {nums([obj.radius])}"
        bv = "" if obj.bv is None else f"  {obj.bv}"
        lines.append(f"{obj.kind} {geometry}   {obj.material}  {nums(obj.colour)}  {nums([obj.strength])}{bv}")
    return "\n".join(lines) + "\n"


def scenes_match(a, b, tolerance_steps=1):
    """True if two scenes agree to within `tolerance_steps` quantization steps.

    Both must be in stored form (camera at x = y = 0, objects in memory order),
    which is what compile_checked and decode_words return.
    """
    def close(x, y, step):
        return abs(x - y) <= tolerance_steps * step + 1e-9

    def angle_close(x, y):
        # One DIR step turns a unit vector by about 0.0035 degrees.
        return abs((x - y + 180) % 360 - 180) <= 0.02 * tolerance_steps

    if (a.spp, a.sky, a.ground, a.checker) != (b.spp, b.sky, b.ground, b.checker):
        return False
    if not close(a.camera[2], b.camera[2], POS_STEP):
        return False
    if not all(angle_close(x, y) for x, y in zip(a.camera[3:], b.camera[3:])):
        return False
    if len(a.objects) != len(b.objects):
        return False

    for p, q in zip(a.objects, b.objects):
        strength = p.strength if p.material == "glow" else 0.0
        if (p.kind, p.material, p.colour, p.bv) != (q.kind, q.material, q.colour, q.bv):
            return False
        if not close(strength, q.strength, 1 / 1024) or not close(p.radius, q.radius, POS_STEP):
            return False
        if not all(close(x, y, POS_STEP) for pp, qp in zip(p.points, q.points) for x, y in zip(pp, qp)):
            return False
    return True
