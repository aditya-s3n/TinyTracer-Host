// Raycast preview: one ray per pixel sample, first hit wins (design doc 4.1 and 6.4).
//
// This is a framing and layout preview, not the chip's path tracer. Each hit is
// shaded with a fixed sun, sky light and a single mirror bounce, which is enough
// to tell matte from metal and rough from polished.

const MAX_SPHERES: i32 = 64;
const MAX_TRIANGLES: i32 = 64;
// Hit ids from here up are triangles: id - TRIANGLE_BASE is the triangle's index.
const TRIANGLE_BASE: i32 = MAX_SPHERES;
const EPS: f32 = 1e-3;
const GROUND_MAX_T: f32 = 256.0;
const CHECKER_SIZE: f32 = 4.0;
const NO_HIT: i32 = -1;
const GROUND: i32 = -2;

const SUN_DIR: vec3f = vec3f(0.3713907, -0.5570860, 0.7427814); // normalize(0.4, -0.6, 0.8)
const SUN_COLOR: vec3f = vec3f(1.0, 0.96, 0.88);

struct Sphere {
    center_radius: vec4f,   // xyz centre, w radius
    color_metallic: vec4f,  // rgb linear colour, w metallic
    rough_emissive: vec4f,  // x roughness, y emissive strength, z index of its object
}

// Quads and boxes arrive already split into triangles.
struct Triangle {
    v0: vec4f,
    v1: vec4f,
    v2: vec4f,
    color_metallic: vec4f,  // as Sphere
    rough_emissive: vec4f,  // as Sphere
}

struct Globals {
    origin: vec4f,       // xyz camera position, w sphere count
    forward: vec4f,      // xyz F, w selected object index (-1 for none)
    right: vec4f,        // xyz R, w 1 when the shader must sRGB-encode its output
    up: vec4f,           // xyz U, w size of one pixel in image coordinates
    sky_horizon: vec4f,  // rgb linear, w sky strength
    sky_top: vec4f,      // rgb linear, w triangle count
    ground_a: vec4f,     // rgb linear
    ground_b: vec4f,     // rgb linear
    spheres: array<Sphere, MAX_SPHERES>,
    triangles: array<Triangle, MAX_TRIANGLES>,
}

@group(0) @binding(0) var<uniform> g: Globals;

struct VsOut {
    @builtin(position) position: vec4f,
    // Image coordinates: x is -1 (left) to +1 (right), y is +1 (top) to -1 (bottom).
    @location(0) image: vec2f,
}

// One triangle that covers the whole viewport.
@vertex
fn vs_main(@builtin(vertex_index) i: u32) -> VsOut {
    let p = vec2f(f32((i << 1u) & 2u), f32(i & 2u)) * 2.0 - 1.0;
    var out: VsOut;
    out.position = vec4f(p, 0.0, 1.0);
    out.image = p;
    return out;
}

struct Hit {
    t: f32,
    id: i32,     // sphere index, TRIANGLE_BASE + triangle index, GROUND or NO_HIT
    n: vec3f,
}

struct Surface {
    albedo: vec3f,
    metallic: f32,
    roughness: f32,
    emissive: f32,
    object: i32,   // index of the object in the scene, -2 for the ground
}

// The chip's sky (Rendering Flow 5h): horizon + (top - horizon) x D_z, with D_z below 0 counted as 0.
fn sky(d: vec3f) -> vec3f {
    let t = clamp(d.z, 0.0, 1.0);
    return mix(g.sky_horizon.rgb, g.sky_top.rgb, t) * g.sky_horizon.w;
}

fn hit_sphere(o: vec3f, d: vec3f, s: vec4f) -> f32 {
    let oc = s.xyz - o;
    let along = dot(oc, d);
    let offset2 = dot(oc, oc) - along * along;
    let r2 = s.w * s.w;
    if offset2 > r2 {
        return -1.0;
    }
    let half_chord = sqrt(r2 - offset2);
    let near = along - half_chord;
    if near > EPS {
        return near;
    }
    let far = along + half_chord;
    if far > EPS {
        return far;
    }
    return -1.0;
}

// Moller-Trumbore: distance to the triangle, hit from either side, or -1.
fn hit_triangle(o: vec3f, d: vec3f, v0: vec3f, v1: vec3f, v2: vec3f) -> f32 {
    let e1 = v1 - v0;
    let e2 = v2 - v0;
    let p = cross(d, e2);
    let det = dot(e1, p);
    if abs(det) < 1e-8 {
        return -1.0;
    }
    let inv = 1.0 / det;
    let s = o - v0;
    let u = dot(s, p) * inv;
    let q = cross(s, e1);
    let v = dot(d, q) * inv;
    if u < 0.0 || v < 0.0 || u + v > 1.0 {
        return -1.0;
    }
    let t = dot(e2, q) * inv;
    return select(-1.0, t, t > EPS);
}

fn trace(o: vec3f, d: vec3f) -> Hit {
    var hit = Hit(1e30, NO_HIT, vec3f(0.0, 0.0, 1.0));
    let count = i32(g.origin.w);
    for (var i = 0; i < count; i++) {
        let t = hit_sphere(o, d, g.spheres[i].center_radius);
        if t > 0.0 && t < hit.t {
            hit.t = t;
            hit.id = i;
        }
    }
    let triangle_count = i32(g.sky_top.w);
    for (var i = 0; i < triangle_count; i++) {
        let t = hit_triangle(o, d, g.triangles[i].v0.xyz, g.triangles[i].v1.xyz, g.triangles[i].v2.xyz);
        if t > 0.0 && t < hit.t {
            hit.t = t;
            hit.id = TRIANGLE_BASE + i;
        }
    }
    if hit.id >= TRIANGLE_BASE {
        let tri = g.triangles[hit.id - TRIANGLE_BASE];
        hit.n = normalize(cross(tri.v1.xyz - tri.v0.xyz, tri.v2.xyz - tri.v0.xyz));
    } else if hit.id >= 0 {
        let s = g.spheres[hit.id].center_radius;
        hit.n = (o + d * hit.t - s.xyz) / s.w;
    }
    // Seen from inside a sphere or from the back of a triangle: flip the normal to face the ray.
    if dot(hit.n, d) > 0.0 {
        hit.n = -hit.n;
    }
    if d.z < 0.0 && o.z > 0.0 {
        let t = o.z / -d.z;
        if t < hit.t && t <= GROUND_MAX_T {
            hit = Hit(t, GROUND, vec3f(0.0, 0.0, 1.0));
        }
    }
    return hit;
}

fn surface(hit: Hit, p: vec3f) -> Surface {
    if hit.id == GROUND {
        // The chip's checker (Rendering Flow 5f). The host moves the scene so the camera
        // is at x = 0, y = 0, so the squares are counted from the camera, not the world
        // origin. Odd squares get colour A, even squares colour B.
        let cell = floor((p.xy - g.origin.xy) / CHECKER_SIZE);
        let odd = abs(cell.x + cell.y) % 2.0;
        return Surface(mix(g.ground_b.rgb, g.ground_a.rgb, odd), 0.0, 1.0, 0.0, -2);
    }
    var color_metallic = g.spheres[0].color_metallic;
    var rough_emissive = g.spheres[0].rough_emissive;
    if hit.id >= TRIANGLE_BASE {
        color_metallic = g.triangles[hit.id - TRIANGLE_BASE].color_metallic;
        rough_emissive = g.triangles[hit.id - TRIANGLE_BASE].rough_emissive;
    } else {
        color_metallic = g.spheres[hit.id].color_metallic;
        rough_emissive = g.spheres[hit.id].rough_emissive;
    }
    return Surface(color_metallic.rgb, color_metallic.w, rough_emissive.x, rough_emissive.y, i32(rough_emissive.z));
}

// 1 when the sun reaches p, 0 when something is in the way.
fn sun_visible(p: vec3f, n: vec3f) -> f32 {
    return select(0.0, 1.0, trace(p + n * EPS, SUN_DIR).id == NO_HIT);
}

// Light arriving at a matte surface: sky from the direction it faces, plus the sun.
fn diffuse_light(n: vec3f, sun: f32) -> vec3f {
    return sky(n) * 0.45 + SUN_COLOR * max(dot(n, SUN_DIR), 0.0) * sun;
}

// What a mirror bounce sees. Shaded without a further bounce.
fn shade_reflected(o: vec3f, d: vec3f) -> vec3f {
    let hit = trace(o, d);
    if hit.id == NO_HIT {
        return sky(d);
    }
    let p = o + d * hit.t;
    let s = surface(hit, p);
    return s.albedo * diffuse_light(hit.n, sun_visible(p, hit.n)) + s.albedo * s.emissive;
}

fn shade(o: vec3f, d: vec3f) -> vec3f {
    let hit = trace(o, d);
    if hit.id == NO_HIT {
        return sky(d);
    }
    let p = o + d * hit.t;
    let n = hit.n;
    let s = surface(hit, p);
    let sun = sun_visible(p, n);
    let n_dot_v = max(dot(n, -d), 0.0);

    // Matte part: metals have none.
    var color = s.albedo * (1.0 - s.metallic) * diffuse_light(n, sun);

    // Mirror part. Non-metals reflect 4% head-on, metals reflect their own colour.
    // Roughness fades the sharp reflection into the plain sky colour.
    let f0 = mix(vec3f(0.04), s.albedo, s.metallic);
    let fresnel = f0 + (max(vec3f(1.0 - s.roughness), f0) - f0) * pow(1.0 - n_dot_v, 5.0);
    if hit.id != GROUND {
        let r = reflect(d, n);
        let sharp = shade_reflected(p + n * EPS, r);
        color += fresnel * mix(sharp, sky(n), s.roughness);

        // Sun highlight: tight on polished spheres, wide and dim on rough ones.
        let a = max(s.roughness, 0.04);
        let shininess = 2.0 / (a * a * a * a) - 2.0;
        let h = normalize(SUN_DIR - d);
        let highlight = pow(max(dot(n, h), 0.0), shininess) * min((shininess + 2.0) / 8.0, 60.0);
        color += fresnel * SUN_COLOR * highlight * max(dot(n, SUN_DIR), 0.0) * sun;
    }

    color += s.albedo * s.emissive;

    // Selected object: an orange rim on a sphere. Flat faces have no rim, so they get an even tint.
    if s.object == i32(g.forward.w) {
        var amount = 0.4;
        if hit.id < TRIANGLE_BASE {
            amount = clamp(pow(1.0 - n_dot_v, 2.5) * 1.6, 0.0, 0.9);
        }
        color = mix(color, vec3f(1.0, 0.45, 0.05), amount);
    }
    return color;
}

fn linear_to_srgb(c: vec3f) -> vec3f {
    let lo = c * 12.92;
    let hi = 1.055 * pow(c, vec3f(1.0 / 2.4)) - 0.055;
    return select(hi, lo, c <= vec3f(0.0031308));
}

@fragment
fn fs_main(in: VsOut) -> @location(0) vec4f {
    // 2 x 2 samples per pixel to smooth sphere edges and the checker.
    var sum = vec3f(0.0);
    for (var i = 0; i < 4; i++) {
        let offset = (vec2f(f32(i & 1), f32(i >> 1u)) - 0.5) * 0.5 * g.up.w;
        let image = in.image + offset;
        let d = normalize(g.forward.xyz + image.x * g.right.xyz + image.y * g.up.xyz);
        // Clamp each sample like the chip clamps a pixel at 255.
        sum += min(shade(g.origin.xyz, d), vec3f(1.0));
    }
    var color = sum * 0.25;
    if g.right.w > 0.5 {
        color = linear_to_srgb(color);
    }
    return vec4f(color, 1.0);
}
