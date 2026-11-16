"""Tests for tthost.scene_compiler: parsing, the chip's rules, the memory image and the decoder.

Expected values come from the design doc: the scene format and rules (section
9.1, 9.2), the memory layout (9.3), the hex dump of the demo scene (9.4) and
the fixed-point table (4.4).

RUN from root:  python -m pytest tests
"""

import math
import random
from pathlib import Path

import pytest

from tthost import scene_compiler as sc

DEMO = Path(__file__).resolve().parent.parent / "scene_demo.txt"

HEADER = ("image 512\nspp 8\ncamera 5 5 12 45 -12 90\n"
          "sky 235 240 255 110 160 255\nground 205 205 200\nchecker 120 120 130\n")


def compile_ok(text):
    compiled, errors = sc.compile_checked(text)
    assert errors == []
    return compiled


def rules(text):
    """The error messages a scene produces, as (line, rule) pairs."""
    compiled, errors = sc.compile_checked(text)
    assert compiled is None
    return [(e["line"], e["rule"]) for e in errors]


@pytest.fixture(scope="module")
def demo():
    return compile_ok(DEMO.read_text())


# Fixed point
def test_fixed_point_values_from_the_design_doc():
    warnings = []
    assert sc.to_fixed(12.0, sc.POS, "z", warnings) == 0x0600
    assert sc.to_fixed(-0.2079, sc.DIR, "F.z", warnings) == 0xF2B2
    assert sc.to_fixed(8.0, sc.GLOW, "glow", warnings) == 0x2000
    assert warnings == []


def test_fixed_point_round_trips_and_rounds_halves_up():
    warnings = []
    for value in (0.0, 1.5, -1.5, 255.9921875, -256.0, 17.0078125):
        assert sc.from_fixed(sc.to_fixed(value, sc.POS, "p", warnings), sc.POS) == value
    # Exactly half a step: up, for positive and negative values alike.
    half = sc.POS_STEP / 2
    assert sc.from_fixed(sc.to_fixed(half, sc.POS, "p", warnings), sc.POS) == sc.POS_STEP
    assert sc.from_fixed(sc.to_fixed(-half, sc.POS, "p", warnings), sc.POS) == 0.0
    assert warnings == []


def test_out_of_range_values_saturate_with_a_warning():
    warnings = []
    assert sc.to_fixed(300.0, sc.POS, "radius", warnings) == 0x7FFF
    assert sc.to_fixed(-300.0, sc.POS, "x", warnings) == 0x8000
    assert sc.to_fixed(2.5, sc.DIR, "R.x", warnings) == 0x7FFF
    assert sc.to_fixed(20.0, sc.GLOW, "glow", warnings) == 0x3FFF
    assert len(warnings) == 4
    assert warnings[0] == "radius = 300 does not fit the chip's number range (-256 to 255.992)"


# Camera
def test_camera_vectors_match_the_design_doc():
    f, r, u = sc.compute_camera_vectors(45, -12, 90)
    assert f == pytest.approx((0.692, 0.692, -0.208), abs=1e-3)
    assert r == pytest.approx((0.707, -0.707, 0.0), abs=1e-3)
    assert u == pytest.approx((0.147, 0.147, 0.978), abs=1e-3)


@pytest.mark.parametrize("yaw, pitch, fov", [(0, 0, 90), (130, 35, 60), (300, -60, 20), (45, -12, 90)])
def test_camera_vectors_lengths_and_inverse(yaw, pitch, fov):
    f, r, u = sc.compute_camera_vectors(yaw, pitch, fov)
    half = math.tan(math.radians(fov) / 2)
    assert math.hypot(*f) == pytest.approx(1.0)
    assert math.hypot(*r) == pytest.approx(half)
    assert math.hypot(*u) == pytest.approx(half)
    assert sum(a * b for a, b in zip(f, r)) == pytest.approx(0, abs=1e-12)
    assert sum(a * b for a, b in zip(f, u)) == pytest.approx(0, abs=1e-12)
    assert u[2] > 0, "up points up"
    assert sc.camera_from_vectors(f, r) == pytest.approx((yaw, pitch, fov))


# Parsing
def test_parse_reads_every_line_kind_and_skips_comments():
    scene, errors = sc.parse_txt(HEADER + "\n# a comment\n"
                                 "sphere 32 22 6 6 matte 220 60 50   # trailing comment\n"
                                 "sphere 1 2 3 1.5 glow 255 170 80 8\n"
                                 "tri 0 0 0  1 0 0  0 1 0 mirror 1 2 3 0 5\n")
    assert errors == []
    assert (scene.image, scene.spp) == (512, 8)
    assert scene.camera == (5, 5, 12, 45, -12, 90)
    assert scene.sky == ((235, 240, 255), (110, 160, 255))
    assert (scene.ground, scene.checker) == ((205, 205, 200), (120, 120, 130))

    red, lamp, tri = scene.objects
    assert (red.kind, red.points, red.radius, red.material, red.colour) == (
        "sphere", [(32, 22, 6)], 6, "matte", (220, 60, 50))
    assert (red.strength, red.bv, red.line) == (0, None, 9)
    assert (lamp.material, lamp.strength, lamp.bv) == ("glow", 8, None)
    assert (tri.kind, tri.material, tri.bv) == ("tri", "mirror", 5)
    assert tri.points == [(0, 0, 0), (1, 0, 0), (0, 1, 0)]


def test_quad_and_box_become_triangles():
    scene, errors = sc.parse_txt("quad 0 0 0  2 0 0  2 0 3  0 0 3 glow 255 170 80 8 1\n"
                                 "box 1 2 0  3 6 5 matte 70 110 230 0 2\n")
    assert errors == []
    quad, box = scene.objects[:2], scene.objects[2:]
    assert [o.points for o in quad] == [[(0, 0, 0), (2, 0, 0), (2, 0, 3)], [(0, 0, 0), (2, 0, 3), (0, 0, 3)]]
    assert len(box) == 12
    assert all((o.kind, o.source, o.bv, o.line) == ("tri", "box", 2, 2) for o in box)

    def area(points):
        a, b, c = points
        e1, e2 = [q - p for p, q in zip(a, b)], [q - p for p, q in zip(a, c)]
        cross = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        return math.hypot(*cross) / 2

    # The 12 triangles cover the box's surface exactly: 2 * (2*4 + 2*5 + 4*5).
    assert sum(area(o.points) for o in box) == pytest.approx(76)
    assert sum(area(o.points) for o in quad) == pytest.approx(6)


@pytest.mark.parametrize("line, rule", [
    ("blob 1 2 3", "unknown keyword 'blob'"),
    ("spp 8", "spp is already set on line 2"),
    ("sphere 1 2 3 matte 1 2 3", "sphere needs 4 numbers, then a material (matte, mirror, glass, glow)"),
    ("sphere 1 2 3 1 wood 1 2 3", "sphere needs 4 numbers, then a material (matte, mirror, glass, glow)"),
    ("sphere 1 2 x 1 matte 1 2 3", "'x' is not a number"),
    ("sphere 1 2 3 1 matte 1 2", "after matte expected R G B [strength] [bv], got 2 values"),
    ("sphere 1 2 3 1 matte 1 2 3 0 1 9", "after matte expected R G B [strength] [bv], got 6 values"),
    ("sphere 1 2 3 1 matte 1 2 3 0 1.5", "bv 1.5 must be a whole number"),
    ("sphere 1 2 nan 1 matte 1 2 3", "'nan' is not a finite number"),
])
def test_parse_errors_name_the_line(line, rule):
    assert rules(HEADER + line + "\n") == [(7, rule)]


# Validation: the rules and messages of section 9.2
@pytest.mark.parametrize("change, expected", [
    (("camera 5 5 12 45 -12 90", "camera 5 5 12 45 -12 100"), (3, "field of view 100 is over the limit of 90")),
    (("camera 5 5 12 45 -12 90", "camera 5 5 12 45 -12 0"), (3, "field of view 0 must be above 0")),
    (("camera 5 5 12", "camera 5 5 0"), (3, "camera is at z = 0; it must be above the ground")),
    (("camera 5 5 12", "camera 5 5 256"), (3, "camera is at z = 256; it must be below 256")),
    (("spp 8", "spp 24"), (2, "spp 24 must be 1, 2, 4, 8, 16 or 32")),
    (("image 512", "image 256"), (1, "image 256 must be 512")),
    (("spp 8\n", ""), (None, "the scene has no spp line")),
    (("camera 5 5 12 45 -12 90", "camera 1 2 3"), (3, "camera needs 6 numbers, got 3")),
    (("110 160 255", "110 160 300"), (4, "colour value 300 must be a whole number from 0 to 255")),
])
def test_header_rules(change, expected):
    assert rules(HEADER.replace(*change)) == [expected]


@pytest.mark.parametrize("line, rule", [
    ("sphere 10 10 1 1.5 matte 1 2 3", "sphere bottom is at z = -0.5; objects must stay at z >= 0"),
    ("sphere 10 10 5 0 matte 1 2 3", "sphere radius 0 must be above 0"),
    ("tri 0 0 0  1 0 0  0 1 -2 matte 1 2 3", "tri corner is at z = -2; objects must stay at z >= 0"),
    ("tri 0 0 0  1 0 0  0 1 0 glass 1 2 3", "glass is for spheres only"),
    ("box 0 0 0  1 1 1 glass 1 2 3", "glass is for spheres only"),
    ("sphere 10 10 5 1 glow 1 2 3 16", "strength 16 must be from 0 to 15.99"),
    ("sphere 10 10 5 1 matte 1 2 3 0 16", "bv 16 must be from 0 to 15"),
    ("sphere 268 5 6 6 matte 1 2 3",
     "sphere centre x = 263 is outside +-256 after moving the camera to x = 0"),
    ("tri 0 0 0  1 0 0  0 -260 0 matte 1 2 3",
     "tri corner y = -265 is outside +-256 after moving the camera to x = 0"),
])
def test_object_rules(line, rule):
    found = rules(HEADER + line + "\n")
    # A box is 12 triangles, so its rule is reported once per triangle; the line is what matters.
    assert set(found) == {(7, rule)}


def test_position_range_is_checked_after_moving_the_camera():
    # x = 260 is outside +-256 in the file, but the camera at x = 5 brings it to 255.
    compile_ok(HEADER + "sphere 260 5 6 6 matte 1 2 3\n")


def test_every_error_is_reported_at_once_in_line_order():
    text = HEADER.replace("spp 8", "spp 3") + "tri 0 0 0 1 0 0 0 1 0 glass 1 2 3\nsphere 1 1 1 2 matte 1 2 3\n"
    assert [line for line, _ in rules(text)] == [2, 7, 8]


# Bounding volumes
def spheres(n, bv="", spacing=3.0):
    return "".join(f"sphere {10 + spacing * i} 20 1 1 matte 1 2 3 0 {bv}\n" for i in range(n))


def test_objects_keep_their_bv_number_and_are_stored_in_bv_order():
    compiled = compile_ok(HEADER + "sphere 10 10 1 1 matte 1 1 1 0 7\n"
                                   "sphere 20 10 1 1 matte 2 2 2 0 2\n"
                                   "sphere 30 10 1 1 matte 3 3 3 0 7\n")
    # Ids 2 and 7 become volumes 0 and 1; memory order follows the volumes.
    assert [(o.colour[0], o.bv) for o in compiled.scene.objects] == [(2, 0), (1, 1), (3, 1)]
    assert [len(bv.objects) for bv in compiled.scene.bvs] == [1, 2]


def test_objects_without_a_bv_are_clustered_by_position():
    near = "".join(f"sphere {10 + i} 10 1 1 matte 1 1 1\n" for i in range(4))
    far = "".join(f"sphere {100 + i} 100 1 1 matte 2 2 2\n" for i in range(4))
    compiled = compile_ok(HEADER + near + far)
    assert len(compiled.scene.bvs) == 2
    for bv in compiled.scene.bvs:
        assert len({o.colour for o in bv.objects}) == 1, "the two groups are not mixed"
    # The same scene always gives the same image.
    assert compile_ok(HEADER + near + far).words == compiled.words


def test_automatic_volumes_avoid_the_numbers_already_used():
    compiled = compile_ok(HEADER + "sphere 10 10 1 1 matte 9 9 9 0 0\n" + spheres(3))
    assert [len(bv.objects) for bv in compiled.scene.bvs] == [1, 3]
    assert compiled.scene.bvs[0].objects[0].colour == (9, 9, 9)


def test_every_object_lies_inside_its_stored_bounding_volume(demo):
    decoded = sc.decode_words(demo.words)
    assert len(decoded.bvs) == 4
    for bv in decoded.bvs:
        assert bv.objects
        for obj in bv.objects:
            for point in obj.points:
                assert math.dist(bv.centre, point) + obj.radius <= bv.radius + 1e-9


def test_a_bounding_volume_holds_at_most_127_objects():
    # 128 objects cannot fit in memory anyway, so check the rule where it lives.
    objects = [sc.SceneObject("sphere", [(i, 0.0, 1.0)], radius=1.0, bv=3) for i in range(128)]
    with pytest.raises(sc.SceneError, match="bv 3 has 128 objects; the limit is 127"):
        sc.build_bounding_volumes(objects)
    assert len(sc.build_bounding_volumes(objects[:127])[0].objects) == 127


# Memory budget and layout
def test_scene_that_does_not_fit_says_where_the_words_went():
    text = HEADER + spheres(6, bv=0) + "".join(
        f"box {10 * i} 30 0  {10 * i + 5} 35 5 matte 1 2 3 0 1\n" for i in range(3)) + \
        "tri 0 0 0 1 0 0 0 1 0 matte 1 2 3 0 2\ntri 0 0 0 1 0 0 0 1 0 matte 1 2 3 0 3\n" \
        "tri 0 0 0 1 0 0 0 1 0 matte 1 2 3 0 3\n"
    assert rules(text) == [(None, "scene needs 547 words; the chip has 512 "
                                  "(spheres 42, triangles 468, BVs 20, header 17)")]


def test_a_scene_of_exactly_512_words_fits():
    # 17 header + 5 for one BV + 70 spheres * 7 = 512.
    compiled = compile_ok(HEADER + spheres(70, bv=0))
    assert len(compiled.words) == 512
    assert rules(HEADER + spheres(71, bv=0))[0][1].startswith("scene needs 519 words")


def test_automatic_volumes_are_dropped_to_make_a_scene_fit():
    # 69 spheres would get 18 -> capped at 16 volumes (580 words); with 2 they just fit (510).
    compiled = compile_ok(HEADER + spheres(69))
    assert len(compiled.words) <= 512
    assert len(compiled.scene.objects) == 69


def test_layout_is_header_then_bvs_then_objects_grouped_by_bv(demo):
    words = demo.words
    assert len(words) == 295                       # the doc's demo budget
    address = sc.BV_BASE + sc.BV_WORDS * 4
    for i, bv in enumerate(demo.scene.bvs):
        first, count = words[sc.BV_BASE + 5 * i] >> 7, words[sc.BV_BASE + 5 * i] & 0x7F
        assert (first, count) == (address, len(bv.objects))
        address += sum(sc.object_word_count(o) for o in bv.objects)
    assert address == len(words)
    assert [len(bv.objects) for bv in demo.scene.bvs] == [4, 4, 12, 4]


def test_demo_header_matches_the_design_doc_hex_dump(demo):
    # Section 9.4: F, R, U, then bv_count|log2spp and CAM_Z.
    assert demo.words[0:9] == [0x2C44, 0x2C44, 0xF2B2, 0x2D41, 0xD2BF, 0x0000, 0x0969, 0x0969, 0x3E9A]
    assert demo.words[15] == 0x0025                # 4 BVs, 32 samples
    assert demo.words[16] == 0x0600                # camera height 12
    assert sc.unpack_colours(demo.words[9:12]) == ((235, 240, 255), (110, 160, 255))
    assert sc.unpack_colours(demo.words[12:15]) == ((205, 205, 200), (120, 120, 130))
    assert all(0 <= w <= 0xFFFF for w in demo.words)
    assert demo.warnings == []


def test_demo_sphere_is_stored_moved_by_the_camera_position(demo):
    # The sphere at (32, 22, 6) in the file is at (27, 17, 6) in memory (section 9.4).
    first = sc.decode_words(demo.words).objects[0]
    assert (first.kind, first.points, first.radius) == ("sphere", [(27.0, 17.0, 6.0)], 6.0)
    assert (first.material, first.colour, first.bv) == ("matte", (220, 60, 50), 0)


def test_object_words_carry_type_material_colour_and_strength():
    compiled = compile_ok(HEADER + "sphere 10 10 3 3 glass 10 20 30 0 0\n"
                                   "tri 6 5 0  7 5 0  6 5 2 glow 255 170 80 8 0\n"
                                   "sphere 10 10 9 1 mirror 1 2 3 5 0\n")
    glass, glow, mirror = sc.decode_words(compiled.words).objects
    assert (glass.kind, glass.material, glass.colour, glass.strength) == ("sphere", "glass", (10, 20, 30), 0)
    assert (glow.kind, glow.material, glow.colour, glow.strength) == ("tri", "glow", (255, 170, 80), 8)
    assert glow.points == [(1, 0, 0), (2, 0, 0), (1, 0, 2)]
    # Strength only means something for glow, so it is not stored for other materials.
    assert (mirror.material, mirror.strength) == ("mirror", 0)


def test_demo_matches_the_numbers_quoted_in_rendering_flow(demo):
    """Rendering Flow section 5c works through the demo scene's BV 0 and gives these values."""
    w = demo.words
    assert w[17] == 0x1284                         # "first object at 37, 4 objects"
    assert w[18] == 2143                           # "r = 16.74 ... raw 2,143"
    centres_and_radii = [(bv.centre, round(bv.radius, 2)) for bv in sc.decode_words(w).bvs]
    # The page gives centres in file coordinates; memory holds them moved by the camera's (5, 5).
    assert centres_and_radii[:3] == [((23, 18, 6), 16.74), ((51.5, 30, 10), 19.69), ((30, 52, 5), 8.71)]


def test_object_words_follow_the_rendering_flow_layout(demo):
    # The red matte sphere at address 37: blue | BV index | T, red | green, strength | mat,
    # then radius and centre z, y, x.
    assert demo.words[37:44] == [50 << 8 | 0 << 1 | 1, 220 << 8 | 60, 0, 6 * 128, 6 * 128, 17 * 128, 27 * 128]

    compiled = compile_ok(HEADER + "sphere 10 10 3 3 glass 10 20 30 0 4\n"
                                   "tri 6 5 0  7 5 0  6 5 2 glow 255 170 80 8 9\n")
    sphere, tri = compiled.words[27:34], compiled.words[34:46]
    assert sphere[:3] == [30 << 8 | 0 << 1 | 1, 10 << 8 | 20, 2]              # glass = 10, volume 0
    assert tri[:3] == [80 << 8 | 1 << 1 | 0, 255 << 8 | 170, 8192 << 2 | 3]   # glow = 11, strength 8.0
    # edge 2 z y x, edge 1 z y x, corner v0 z y x (camera at 5, 5).
    assert tri[3:] == [2 * 128, 0, 0,  0, 0, 1 * 128,  0, 0, 1 * 128]


def test_bounding_volume_leaves_room_for_the_push_off():
    # A lone sphere: its volume must be bigger by at least the 0.031 a bounce is pushed off it.
    bv = compile_ok(HEADER + "sphere 10 10 3 3 matte 1 2 3\n").scene.bvs[0]
    assert 3 + 0.031 < bv.radius <= 3 + sc.BV_MARGIN + sc.POS_STEP


def test_geometry_that_does_not_fit_the_number_range_is_an_error():
    # Both corners are inside +-256, but the edge between them is 450 long.
    found = rules(HEADER + "tri -200 5 0  250 5 0  5 5 9 matte 1 2 3\n")
    assert found == [(None, "obj tri bv0 e1 x = 450 does not fit the chip's number range (-256 to 255.992)")]


def test_triangle_with_no_area_is_rejected():
    assert rules(HEADER + "tri 10 10 0  12 10 0  14 10 0 matte 1 2 3\n") == [
        (7, "tri has a triangle with no area (its corners are in a line)")]
    assert rules(HEADER + "quad 10 10 0  10 10 0  10 10 0  10 10 0 matte 1 2 3\n")[0][0] == 7


def test_objects_too_far_for_the_chip_to_measure_get_a_warning():
    compiled = compile_ok(HEADER + "sphere 10 10 3 3 matte 1 2 3\nbox 200 200 0  210 210 5 matte 1 2 3\n")
    assert compiled.warnings == [
        "line 8: box reaches 290 from the camera; the chip measures distances up to 256, "
        "so it may be cut off or missing"]


# Decoder and round trip
def random_scene(rng):
    lines = [HEADER.replace("camera 5 5 12 45 -12 90",
                            f"camera {rng.uniform(-50, 50):.3f} {rng.uniform(-50, 50):.3f} {rng.uniform(1, 40):.3f} "
                            f"{rng.uniform(0, 360):.2f} {rng.uniform(-80, 80):.2f} {rng.uniform(10, 90):.2f}")]

    def point():
        return f"{rng.uniform(-100, 100):.3f} {rng.uniform(-100, 100):.3f} {rng.uniform(0, 50):.3f}"

    def tail(spheres_only=False):
        material = rng.choice(sc.MATERIALS if spheres_only else ("matte", "mirror", "glow"))
        colour = " ".join(str(rng.randrange(256)) for _ in range(3))
        bv = rng.choice(["", f" {rng.randrange(16)}"])
        return f"{material} {colour} {rng.uniform(0, 15.99):.3f}{bv}"

    for _ in range(rng.randrange(1, 12)):
        radius = rng.uniform(0.1, 20)
        x, y, z = rng.uniform(-100, 100), rng.uniform(-100, 100), radius + rng.uniform(0, 30)
        lines.append(f"sphere {x:.3f} {y:.3f} {z:.3f} {radius:.3f} {tail(spheres_only=True)}")
    for _ in range(rng.randrange(0, 8)):
        lines.append(f"tri {point()} {point()} {point()} {tail()}")
    if rng.random() < 0.5:
        lines.append(f"quad {point()} {point()} {point()} {point()} {tail()}")
    if rng.random() < 0.5:
        lines.append(f"box {point()} {point()} {tail()}")
    return "\n".join(lines) + "\n"


def test_demo_round_trips_through_the_decoder(demo):
    assert sc.scenes_match(demo.scene, sc.decode_words(demo.words))


@pytest.mark.parametrize("seed", range(25))
def test_random_scenes_round_trip_within_one_step(seed):
    """The main unit test (section 9.5): encode then decode gives the same scene back."""
    compiled = compile_ok(random_scene(random.Random(seed)))
    decoded = sc.decode_words(compiled.words)
    assert sc.scenes_match(compiled.scene, decoded)
    # And every stored volume still contains its objects after rounding.
    for bv in decoded.bvs:
        for obj in bv.objects:
            assert all(math.dist(bv.centre, p) + obj.radius <= bv.radius + 1e-9 for p in obj.points)


def test_scenes_match_notices_real_differences(demo):
    def changed(index, delta):
        words = list(demo.words)
        words[index] = (words[index] + delta) & 0xFFFF
        return sc.decode_words(words)

    last = len(demo.words) - 1                     # x of the last triangle's corner 0
    assert sc.scenes_match(demo.scene, changed(last, 1)), "one step is within tolerance"
    assert not sc.scenes_match(demo.scene, changed(last, 3))
    assert not sc.scenes_match(demo.scene, changed(9, 0x0100)), "a colour must match exactly"
    assert not sc.scenes_match(demo.scene, changed(16, 5)), "camera height"


def test_decoded_scene_written_as_text_compiles_to_the_same_scene(demo):
    decoded = sc.decode_words(demo.words)
    again = compile_ok(sc.scene_to_text(decoded))
    assert len(again.words) == len(demo.words)
    assert sc.scenes_match(decoded, sc.decode_words(again.words))


@pytest.mark.parametrize("words, message", [
    ([0] * 10, "the header alone is 17"),
    ([0] * 15 + [17 << 3, 0], "17 BVs"),
    ([0] * 15 + [0x7, 0], "log2 spp 7"),
    ([0] * 15 + [2 << 3, 0, 0], "ends inside the 2 BV records"),
    ([0] * 15 + [1 << 3, 0] + [22 << 7 | 1, 0, 0, 0, 0] + [0] * 3, "runs past the end"),
    ([0] * 15 + [1 << 3, 0] + [22 << 7 | 1, 0, 0, 0, 0] + [0x8000] + [0] * 8, "runs past the end"),
])
def test_decoder_rejects_images_that_are_not_scenes(words, message):
    with pytest.raises(ValueError, match=message):
        sc.decode_words(words)


# Hex dump and the top-level calls
def test_hex_dump_names_every_word_and_reads_back(demo):
    _, report = sc.compile_text(DEMO.read_text(), "scene_demo.txt")
    dump = report["hex"]
    assert "# words   295 / 512" in dump
    assert "  000   2C44  hdr F.x                    0.6917  DIR" in dump
    assert "  00F   0025  hdr bv_count|log2spp       4 | 5" in dump
    assert "  010   0600  hdr CAM_Z                  12.0000  POS" in dump
    assert dump.rstrip().endswith("# warnings: none")
    assert sc.parse_hex_dump(dump) == demo.words
    # A bare "address word" listing, as dumped from the RTL, reads back too.
    assert sc.parse_hex_dump(sc.hex_dump(demo.words)) == demo.words
    with pytest.raises(sc.SceneError):
        sc.parse_hex_dump("000 XYZ\n")


def test_compile_text_report():
    words, report = sc.compile_text(DEMO.read_text())
    assert len(words) == 295
    assert {k: report[k] for k in ("words", "bvs", "spheres", "triangles", "errors", "warnings")} == {
        "words": 295, "bvs": 4, "spheres": 6, "triangles": 18, "errors": [], "warnings": []}

    words, report = sc.compile_text(HEADER.replace("90", "100"))
    assert words is None and report["hex"] == "" and report["words"] == 0
    assert report["errors"] == [{"line": 3, "rule": "field of view 100 is over the limit of 90"}]


def test_compile_scene_reads_a_file_and_raises_on_errors(tmp_path):
    words, warnings, metadata = sc.compile_scene(DEMO)
    assert (len(words), warnings, metadata["bvs"]) == (295, [], 4)

    bad = tmp_path / "bad.txt"
    bad.write_text(HEADER + "tri 0 0 0 1 0 0 0 1 0 glass 1 2 3\n")
    with pytest.raises(sc.SceneError) as error:
        sc.compile_scene(bad)
    assert (error.value.line, error.value.rule) == (7, "glass is for spheres only")
    assert str(error.value) == "line 7: glass is for spheres only"


def test_load_scene_parses_a_file_and_raises_on_a_broken_line(tmp_path):
    assert len(sc.load_scene(DEMO).objects) == 6 + 18
    broken = tmp_path / "broken.txt"
    broken.write_text("camera 1 2\n")
    with pytest.raises(sc.SceneError, match="line 1: camera needs 6 numbers, got 2"):
        sc.load_scene(broken)


def test_empty_scene_is_just_the_header():
    compiled = compile_ok(HEADER)
    assert len(compiled.words) == 17 and compiled.words[15] == 0x0003   # no BVs, 8 samples
    assert sc.decode_words(compiled.words).objects == []


def test_scene_written_by_the_designer_compiles():
    """The exact text the Rust designer's to_txt test expects (designer/src/scene.rs)."""
    text = ("# TinyTracer scene, written by the scene designer.\n\n"
            "image   512\nspp     32\ncamera  5 5 12   45 -12 90\n"
            "sky     235 240 255  110 160 255\nground  205 205 200\nchecker 120 120 130\n\n"
            "# Red matte\nsphere 32 22 6  6   matte  220 60 50  0\n\n"
            "# Lamp2\nsphere 26 18 1.5  1.5   glow  255 170 80  8\n"
            "tri 12 48 0  22 48 0  17 53 9   mirror  255 255 255  0\n"
            "quad 54 18 0.5  62 24 0.5  62 24 7  54 18 7   glow  255 170 80  8\n"
            "box 30 52 0  40 62 10   matte  204 204 204  0\n")
    compiled = compile_ok(text)
    assert len(compiled.scene.objects) == 2 + 1 + 2 + 12
    assert sc.scenes_match(compiled.scene, sc.decode_words(compiled.words))
