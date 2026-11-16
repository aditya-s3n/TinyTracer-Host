//! Scene model: the source of truth. No UI or GPU code, so it can be unit-tested on its own.
//!
//! Conventions follow the design doc: z is up, the ground is the plane z = 0,
//! positions stay within +/-256 and the field of view is at most 90 degrees.
//! Colours are stored the way the colour pickers show them (sRGB, 0..1) and
//! converted to linear light only when they are sent to the shader.

use glam::Vec3;

pub const POS_LIMIT: f32 = 256.0;
pub const MAX_FOV_DEG: f32 = 90.0;
pub const MIN_FOV_DEG: f32 = 10.0;
pub const MAX_PITCH_DEG: f32 = 89.0;
pub const MAX_EMISSIVE: f32 = 15.99;
/// Size of the sphere array in the shader's uniform buffer.
pub const MAX_SPHERES: usize = 64;
/// Size of the triangle array in the shader's uniform buffer. A quad uses 2, a box 12.
/// The chip runs out of memory well before this (about 40 triangles fit in 512 words).
pub const MAX_TRIANGLES: usize = 64;
/// The chip's scene memory, in 16-bit words.
pub const SRAM_WORDS: usize = 512;
/// Samples per pixel the chip can average.
pub const VALID_SPP: [u8; 6] = [1, 2, 4, 8, 16, 32];
/// A sphere at least this metallic is written to the scene file as a mirror.
pub const MIRROR_THRESHOLD: f32 = 0.5;

#[derive(Clone, Debug, PartialEq)]
pub struct Material {
    /// sRGB, 0..1.
    pub color: [f32; 3],
    /// 0 = dielectric (matte/plastic), 1 = metal.
    pub metallic: f32,
    /// 0 = mirror-sharp reflections, 1 = fully diffuse.
    pub roughness: f32,
    /// Glow strength: the sphere emits `color * emissive`. 0 = not a light.
    pub emissive: f32,
}

impl Material {
    /// The chip material this becomes in the scene file, and its glow strength.
    ///
    /// The chip only knows matte, mirror, glass and glow, so the sliders are
    /// reduced to the nearest one: anything emissive is glow, otherwise mostly
    /// metallic is a (perfect) mirror, otherwise matte. Roughness has no chip
    /// equivalent and glass cannot be chosen from the designer yet.
    pub fn chip_material(&self) -> (&'static str, f32) {
        if self.emissive > 0.0 {
            ("glow", self.emissive.min(MAX_EMISSIVE))
        } else if self.metallic >= MIRROR_THRESHOLD {
            ("mirror", 0.0)
        } else {
            ("matte", 0.0)
        }
    }
}

impl Default for Material {
    fn default() -> Self {
        Self { color: [0.8, 0.8, 0.8], metallic: 0.0, roughness: 0.6, emissive: 0.0 }
    }
}

pub type Triangle = [Vec3; 3];

/// The four shapes of the scene file. Quads and boxes stay whole while editing, so
/// a box is resized as one thing; they are only split into triangles for the
/// preview, for picking and for the memory estimate (design doc 6.2).
#[derive(Clone, Debug, PartialEq)]
pub enum Shape {
    Sphere { center: Vec3, radius: f32 },
    Tri([Vec3; 3]),
    /// Four corners in order around the edge. Drawn as triangles 0-1-2 and 0-2-3.
    Quad([Vec3; 4]),
    /// Two opposite corners of a box whose sides line up with the x, y and z axes.
    Box { a: Vec3, b: Vec3 },
}

/// A shape without its numbers, for "add one of these".
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ShapeKind {
    Sphere,
    Tri,
    Quad,
    Box,
}

impl ShapeKind {
    pub const ALL: [Self; 4] = [Self::Sphere, Self::Tri, Self::Quad, Self::Box];

    /// The keyword that starts this shape's line in the scene file.
    pub fn keyword(self) -> &'static str {
        match self {
            Self::Sphere => "sphere",
            Self::Tri => "tri",
            Self::Quad => "quad",
            Self::Box => "box",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Sphere => "Sphere",
            Self::Tri => "Triangle",
            Self::Quad => "Quad",
            Self::Box => "Box",
        }
    }

    /// Triangles the chip stores for one of these.
    pub fn triangle_count(self) -> usize {
        match self {
            Self::Sphere => 0,
            Self::Tri => 1,
            Self::Quad => 2,
            Self::Box => 12,
        }
    }
}

impl Shape {
    pub fn kind(&self) -> ShapeKind {
        match self {
            Self::Sphere { .. } => ShapeKind::Sphere,
            Self::Tri(_) => ShapeKind::Tri,
            Self::Quad(_) => ShapeKind::Quad,
            Self::Box { .. } => ShapeKind::Box,
        }
    }

    /// The triangles this shape is made of. Empty for a sphere.
    pub fn triangles(&self) -> Vec<Triangle> {
        match *self {
            Self::Sphere { .. } => Vec::new(),
            Self::Tri(t) => vec![t],
            Self::Quad([a, b, c, d]) => vec![[a, b, c], [a, c, d]],
            Self::Box { a, b } => {
                // Corner i takes x from `b` when bit 0 is set, y for bit 1, z for bit 2.
                let corner = |i: usize| {
                    Vec3::new(
                        if i & 1 == 0 { a.x } else { b.x },
                        if i & 2 == 0 { a.y } else { b.y },
                        if i & 4 == 0 { a.z } else { b.z },
                    )
                };
                // Each face as four corners in order around its edge.
                const FACES: [[usize; 4]; 6] =
                    [[0, 1, 3, 2], [4, 5, 7, 6], [0, 1, 5, 4], [2, 3, 7, 6], [0, 2, 6, 4], [1, 3, 7, 5]];
                FACES
                    .iter()
                    .flat_map(|&[p, q, r, s]| [[corner(p), corner(q), corner(r)], [corner(p), corner(r), corner(s)]])
                    .collect()
            }
        }
    }

    /// Lowest and highest corner of the axis-aligned box around the shape.
    pub fn bounds(&self) -> (Vec3, Vec3) {
        let around = |points: &[Vec3]| {
            let lo = points.iter().copied().reduce(Vec3::min).unwrap_or(Vec3::ZERO);
            let hi = points.iter().copied().reduce(Vec3::max).unwrap_or(Vec3::ZERO);
            (lo, hi)
        };
        match self {
            Self::Sphere { center, radius } => (*center - Vec3::splat(*radius), *center + Vec3::splat(*radius)),
            Self::Tri(points) => around(points),
            Self::Quad(points) => around(points),
            Self::Box { a, b } => around(&[*a, *b]),
        }
    }

    /// Middle of the shape's bounds. The side panel's Position row shows and moves this.
    pub fn center(&self) -> Vec3 {
        let (lo, hi) = self.bounds();
        (lo + hi) * 0.5
    }

    /// Move the whole shape.
    pub fn translate(&mut self, delta: Vec3) {
        match self {
            Self::Sphere { center, .. } => *center += delta,
            Self::Tri(points) => points.iter_mut().for_each(|p| *p += delta),
            Self::Quad(points) => points.iter_mut().for_each(|p| *p += delta),
            Self::Box { a, b } => {
                *a += delta;
                *b += delta;
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Object {
    pub name: String,
    pub shape: Shape,
    pub material: Material,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Camera {
    pub pos: Vec3,
    /// Degrees, measured from +x towards +y.
    pub yaw_deg: f32,
    /// Degrees, positive looks up.
    pub pitch_deg: f32,
    /// Full field of view in degrees (the view is square, so it is both horizontal and vertical).
    pub fov_deg: f32,
}

impl Camera {
    /// Unit vector pointing from the camera to the centre of the image.
    pub fn forward(&self) -> Vec3 {
        let (sy, cy) = self.yaw_deg.to_radians().sin_cos();
        let (sp, cp) = self.pitch_deg.to_radians().sin_cos();
        Vec3::new(cp * cy, cp * sy, sp)
    }

    /// Unit vector pointing to the right of the image. Always horizontal.
    pub fn right_unit(&self) -> Vec3 {
        let (sy, cy) = self.yaw_deg.to_radians().sin_cos();
        Vec3::new(sy, -cy, 0.0)
    }

    /// The camera vectors exactly as the scene compiler defines them (design doc 6.4):
    /// F has length 1; R and U reach from the image centre to its right and top
    /// edge, so both have length tan(FOV / 2).
    pub fn basis(&self) -> (Vec3, Vec3, Vec3) {
        let f = self.forward();
        let half = (self.fov_deg.to_radians() * 0.5).tan();
        let r = self.right_unit() * half;
        let u = self.right_unit().cross(f) * half;
        (f, r, u)
    }

    /// Ray through the image point (a, b): a runs -1 (left) to +1 (right), b runs +1 (top) to -1 (bottom).
    pub fn ray_dir(&self, a: f32, b: f32) -> Vec3 {
        let (f, r, u) = self.basis();
        (f + a * r + b * u).normalize()
    }

    pub fn clamp(&mut self) {
        self.pos = self.pos.clamp(Vec3::splat(-POS_LIMIT), Vec3::splat(POS_LIMIT));
        self.pitch_deg = self.pitch_deg.clamp(-MAX_PITCH_DEG, MAX_PITCH_DEG);
        self.fov_deg = self.fov_deg.clamp(MIN_FOV_DEG, MAX_FOV_DEG);
        self.yaw_deg = self.yaw_deg.rem_euclid(360.0);
    }
}

/// Sky gradient in the style of "Ray Tracing in One Weekend": blend from the
/// horizon colour to the top colour by how far up the ray points.
#[derive(Clone, Debug, PartialEq)]
pub struct Sky {
    /// sRGB, 0..1.
    pub horizon: [f32; 3],
    /// sRGB, 0..1.
    pub top: [f32; 3],
    /// Brightness multiplier applied in linear light; above 1 the sky is brighter than white.
    pub strength: f32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Scene {
    pub camera: Camera,
    /// Samples per pixel for the chip render; one of `VALID_SPP`. The preview ignores it.
    pub spp: u8,
    pub sky: Sky,
    /// The two checker colours of the ground plane (sRGB, 0..1).
    pub ground: [[f32; 3]; 2],
    pub objects: Vec<Object>,
}

impl Default for Scene {
    /// A small starter scene, framed like `sim/scene_demo.txt`.
    fn default() -> Self {
        let object = |name: &str, shape, color, metallic, roughness, emissive| Object {
            name: name.to_owned(),
            shape,
            material: Material { color, metallic, roughness, emissive },
        };
        let sphere = |x, y, z, radius| Shape::Sphere { center: Vec3::new(x, y, z), radius };
        let v = Vec3::new;
        Self {
            camera: Camera { pos: Vec3::new(5.0, 5.0, 12.0), yaw_deg: 45.0, pitch_deg: -12.0, fov_deg: 90.0 },
            spp: 32,
            sky: Sky {
                // (1, 1, 1) and (0.5, 0.7, 1.0) in linear light: the book's sky.
                horizon: [1.0, 1.0, 1.0],
                top: [0.735, 0.854, 1.0],
                strength: 1.0,
            },
            ground: [[0.80, 0.80, 0.78], [0.47, 0.47, 0.51]],
            objects: vec![
                object("Red matte", sphere(32.0, 22.0, 6.0, 6.0), [0.86, 0.24, 0.20], 0.0, 0.7, 0.0),
                object("Chrome", sphere(28.0, 12.0, 4.0, 4.0), [0.95, 0.95, 0.95], 1.0, 0.05, 0.0),
                object("Lamp", sphere(20.0, 26.0, 2.5, 2.5), [1.0, 0.67, 0.31], 0.0, 0.5, 1.5),
                object(
                    "Blue box",
                    Shape::Box { a: v(30.0, 52.0, 0.0), b: v(40.0, 62.0, 10.0) },
                    [0.27, 0.43, 0.90],
                    0.0,
                    0.8,
                    0.0,
                ),
                object(
                    "Light panel",
                    Shape::Quad([v(54.0, 18.0, 0.5), v(62.0, 24.0, 0.5), v(62.0, 24.0, 7.0), v(54.0, 18.0, 7.0)]),
                    [1.0, 0.67, 0.31],
                    0.0,
                    0.5,
                    2.0,
                ),
                object(
                    "Yellow triangle",
                    Shape::Tri([v(12.0, 48.0, 0.0), v(22.0, 48.0, 0.0), v(17.0, 53.0, 9.0)]),
                    [0.90, 0.78, 0.35],
                    0.0,
                    0.8,
                    0.0,
                ),
            ],
        }
    }
}

impl Scene {
    pub fn sphere_count(&self) -> usize {
        self.objects.iter().filter(|o| o.shape.kind() == ShapeKind::Sphere).count()
    }

    /// Triangles after quads and boxes are split up.
    pub fn triangle_count(&self) -> usize {
        self.objects.iter().map(|o| o.shape.kind().triangle_count()).sum()
    }

    /// Whether the preview has room for one more of this shape.
    pub fn can_add(&self, kind: ShapeKind) -> bool {
        match kind {
            ShapeKind::Sphere => self.sphere_count() < MAX_SPHERES,
            _ => self.triangle_count() + kind.triangle_count() <= MAX_TRIANGLES,
        }
    }

    /// Rough size of the scene in the chip's memory (design doc 6.2): a 17-word
    /// header, 5 words per bounding volume, 7 per sphere and 12 per triangle.
    /// It assumes a single bounding volume; the Python compiler decides the real number.
    pub fn word_estimate(&self) -> usize {
        17 + 5 + 7 * self.sphere_count() + 12 * self.triangle_count()
    }

    /// Add a shape standing on the ground in front of the camera, facing it, and
    /// return its index. Returns `None` when the preview is full.
    pub fn add(&mut self, kind: ShapeKind) -> Option<usize> {
        if !self.can_add(kind) {
            return None;
        }
        let f = self.camera.forward();
        let flat = Vec3::new(f.x, f.y, 0.0).normalize_or(Vec3::X);
        let limit = Vec3::splat(POS_LIMIT - 8.0);
        let mut base = (self.camera.pos + flat * 25.0).clamp(-limit, limit);
        base.z = 0.0;
        let side = self.camera.right_unit() * 4.0;
        let up = |height| Vec3::new(0.0, 0.0, height);
        let shape = match kind {
            ShapeKind::Sphere => Shape::Sphere { center: base + up(3.0), radius: 3.0 },
            ShapeKind::Tri => Shape::Tri([base - side, base + side, base + up(7.0)]),
            ShapeKind::Quad => Shape::Quad([base - side, base + side, base + side + up(6.0), base - side + up(6.0)]),
            ShapeKind::Box => Shape::Box { a: base - Vec3::new(3.0, 3.0, 0.0), b: base + Vec3::new(3.0, 3.0, 6.0) },
        };
        self.objects.push(Object {
            name: format!("{} {}", kind.label(), self.objects.len() + 1),
            shape,
            material: Material::default(),
        });
        Some(self.objects.len() - 1)
    }

    /// Copy an object, moved along x so it sits beside the original. Returns the new index.
    pub fn duplicate(&mut self, index: usize) -> Option<usize> {
        let mut copy = self.objects.get(index)?.clone();
        if !self.can_add(copy.shape.kind()) {
            return None;
        }
        let (lo, hi) = copy.shape.bounds();
        copy.shape.translate(Vec3::X * (hi.x - lo.x + 1.0));
        copy.name.push_str(" copy");
        self.objects.push(copy);
        Some(self.objects.len() - 1)
    }

    /// Index of the nearest object hit by the ray, using the same maths as the shader.
    pub fn pick(&self, origin: Vec3, dir: Vec3) -> Option<usize> {
        let ground_t = if dir.z < 0.0 && origin.z > 0.0 { origin.z / -dir.z } else { f32::INFINITY };
        let hit = |shape: &Shape| match shape {
            Shape::Sphere { center, radius } => ray_sphere(origin, dir, *center, *radius),
            _ => shape.triangles().iter().filter_map(|t| ray_triangle(origin, dir, t)).min_by(f32::total_cmp),
        };
        self.objects
            .iter()
            .enumerate()
            .filter_map(|(i, o)| hit(&o.shape).map(|t| (i, t)))
            .filter(|&(_, t)| t < ground_t)
            .min_by(|a, b| a.1.total_cmp(&b.1))
            .map(|(i, _)| i)
    }
}

impl Scene {
    /// The scene as a TinyTracer scene file (design doc 9.1, same layout as `scene_demo.txt`).
    pub fn to_txt(&self) -> String {
        let cam = &self.camera;
        let mut out = String::new();
        out += "# TinyTracer scene, written by the scene designer.\n";
        out += "# z is up. Colours are 0-255. Positions must stay within +-256.\n";
        out += "#   sphere x y z r               material R G B strength\n";
        out += "#   tri    3 corners x y z       material R G B strength\n";
        out += "#   quad   4 corners in order    material R G B strength   (host makes 2 triangles)\n";
        out += "#   box    2 opposite corners    material R G B strength   (host makes 12 triangles)\n\n";
        out += "image   512\n";
        out += &format!("spp     {}\n", self.spp);
        out += &format!(
            "camera  {} {} {}   {} {} {}\n",
            num(cam.pos.x), num(cam.pos.y), num(cam.pos.z), num(cam.yaw_deg), num(cam.pitch_deg), num(cam.fov_deg),
        );
        // The file has no sky strength, so it is folded into the two colours.
        let sky = |c: [f32; 3]| rgb255(c.map(|v| linear_to_srgb(srgb_to_linear(v) * self.sky.strength)));
        out += &format!("sky     {}  {}\n", sky(self.sky.horizon), sky(self.sky.top));
        out += &format!("ground  {}\n", rgb255(self.ground[0]));
        out += &format!("checker {}\n", rgb255(self.ground[1]));

        for object in &self.objects {
            let (material, strength) = object.material.chip_material();
            let name: String = object.name.chars().filter(|c| !c.is_control()).collect();
            let geometry = match &object.shape {
                Shape::Sphere { center, radius } => format!("{}  {}", point(*center), num(*radius)),
                Shape::Tri(corners) => points(corners),
                Shape::Quad(corners) => points(corners),
                Shape::Box { a, b } => points(&[*a, *b]),
            };
            out += &format!("\n# {name}\n");
            out += &format!(
                "{} {geometry}   {material}  {}  {}\n",
                object.shape.kind().keyword(),
                rgb255(object.material.color),
                num(strength),
            );
        }
        out
    }
}

/// A number for the scene file: at most 3 decimals, no trailing zeros ("6", "1.5", "-0.125").
fn num(v: f32) -> String {
    let text = format!("{v:.3}");
    let text = text.trim_end_matches('0').trim_end_matches('.');
    if text == "-0" { "0".to_owned() } else { text.to_owned() }
}

/// A position for the scene file: "x y z".
fn point(p: Vec3) -> String {
    format!("{} {} {}", num(p.x), num(p.y), num(p.z))
}

/// Several positions, two spaces apart so the corners are easy to tell apart.
fn points(corners: &[Vec3]) -> String {
    corners.iter().map(|&p| point(p)).collect::<Vec<_>>().join("  ")
}

/// An sRGB colour (0..1) as three 0-255 integers.
fn rgb255(c: [f32; 3]) -> String {
    let [r, g, b] = c.map(|v| (v.clamp(0.0, 1.0) * 255.0).round() as u8);
    format!("{r} {g} {b}")
}

/// Distance along a unit-length ray to a sphere, or `None` on a miss (design doc 4.1).
pub fn ray_sphere(origin: Vec3, dir: Vec3, center: Vec3, radius: f32) -> Option<f32> {
    let oc = center - origin;
    let along = oc.dot(dir);
    let offset2 = oc.length_squared() - along * along;
    let r2 = radius * radius;
    if offset2 > r2 {
        return None;
    }
    let half_chord = (r2 - offset2).sqrt();
    let near = along - half_chord;
    let far = along + half_chord;
    // `near` is behind us when the ray starts inside the sphere.
    [near, far].into_iter().find(|&t| t > 1e-3)
}

/// Distance along a ray to a triangle, hit from either side, or `None` on a miss
/// (the Möller-Trumbore test, design doc 4.1).
pub fn ray_triangle(origin: Vec3, dir: Vec3, [v0, v1, v2]: &Triangle) -> Option<f32> {
    let e1 = *v1 - *v0;
    let e2 = *v2 - *v0;
    let p = dir.cross(e2);
    let det = e1.dot(p);
    if det.abs() < 1e-8 {
        return None; // ray runs along the triangle's plane, or the triangle has no area
    }
    let inv = 1.0 / det;
    let s = origin - *v0;
    // u and v say where the hit is between the corners; outside 0..1 is a miss.
    let u = s.dot(p) * inv;
    let q = s.cross(e1);
    let v = dir.dot(q) * inv;
    if u < 0.0 || v < 0.0 || u + v > 1.0 {
        return None;
    }
    let t = e2.dot(q) * inv;
    (t > 1e-3).then_some(t)
}

/// sRGB-encoded value (0..1) to linear light.
pub fn srgb_to_linear(c: f32) -> f32 {
    if c <= 0.04045 { c / 12.92 } else { ((c + 0.055) / 1.055).powf(2.4) }
}

/// Linear light to an sRGB-encoded value (design doc 4.5).
pub fn linear_to_srgb(c: f32) -> f32 {
    if c <= 0.0031308 { c * 12.92 } else { 1.055 * c.powf(1.0 / 2.4) - 0.055 }
}

pub fn srgb_to_linear3(c: [f32; 3]) -> [f32; 3] {
    c.map(srgb_to_linear)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: Vec3, b: [f32; 3]) -> bool {
        (a - Vec3::from(b)).abs().max_element() < 1e-3
    }

    #[test]
    fn demo_camera_matches_design_doc() {
        // Design doc 4.1: yaw 45, pitch -12, FOV 90.
        let cam = Camera { pos: Vec3::new(5.0, 5.0, 12.0), yaw_deg: 45.0, pitch_deg: -12.0, fov_deg: 90.0 };
        let (f, r, u) = cam.basis();
        assert!(close(f, [0.692, 0.692, -0.208]), "{f}");
        assert!(close(r, [0.707, -0.707, 0.0]), "{r}");
        assert!(close(u, [0.147, 0.147, 0.978]), "{u}");
    }

    #[test]
    fn right_and_up_have_length_tan_half_fov() {
        let cam = Camera { pos: Vec3::ZERO, yaw_deg: 130.0, pitch_deg: 35.0, fov_deg: 60.0 };
        let (f, r, u) = cam.basis();
        let half = 30f32.to_radians().tan();
        assert!((f.length() - 1.0).abs() < 1e-5);
        assert!((r.length() - half).abs() < 1e-5);
        assert!((u.length() - half).abs() < 1e-5);
        assert!(f.dot(r).abs() < 1e-5 && f.dot(u).abs() < 1e-5 && r.dot(u).abs() < 1e-5);
    }

    #[test]
    fn ray_sphere_hits_front_and_misses() {
        let t = ray_sphere(Vec3::ZERO, Vec3::X, Vec3::new(10.0, 0.0, 0.0), 2.0).unwrap();
        assert!((t - 8.0).abs() < 1e-5);
        assert!(ray_sphere(Vec3::ZERO, Vec3::X, Vec3::new(10.0, 3.0, 0.0), 2.0).is_none());
        // Behind the camera.
        assert!(ray_sphere(Vec3::ZERO, Vec3::X, Vec3::new(-10.0, 0.0, 0.0), 2.0).is_none());
        // From inside: the far wall.
        let t = ray_sphere(Vec3::ZERO, Vec3::X, Vec3::ZERO, 2.0).unwrap();
        assert!((t - 2.0).abs() < 1e-5);
    }

    fn plain(shape: Shape) -> Object {
        Object { name: String::new(), shape, material: Material::default() }
    }

    fn empty_scene() -> Scene {
        Scene { objects: Vec::new(), ..Scene::default() }
    }

    #[test]
    fn pick_returns_nearest_sphere_and_respects_ground() {
        let mut scene = empty_scene();
        let at = |x: f32, z: f32| plain(Shape::Sphere { center: Vec3::new(x, 0.0, z), radius: 1.0 });
        scene.objects.push(at(20.0, 5.0));
        scene.objects.push(at(10.0, 5.0));
        // Buried sphere: the ground is hit first, so it cannot be picked.
        scene.objects.push(at(10.0, -3.0));
        let origin = Vec3::new(0.0, 0.0, 5.0);
        assert_eq!(scene.pick(origin, Vec3::X), Some(1));
        assert_eq!(scene.pick(origin, Vec3::Y), None);
        let down = (Vec3::new(10.0, 0.0, -3.0) - origin).normalize();
        assert_eq!(scene.pick(origin, down), None);
    }

    #[test]
    fn ray_triangle_hits_both_sides_and_misses_outside() {
        let tri = [Vec3::new(5.0, -1.0, -1.0), Vec3::new(5.0, 1.0, -1.0), Vec3::new(5.0, 0.0, 1.0)];
        let t = ray_triangle(Vec3::ZERO, Vec3::X, &tri).unwrap();
        assert!((t - 5.0).abs() < 1e-5);
        // From the other side.
        let t = ray_triangle(Vec3::new(8.0, 0.0, 0.0), -Vec3::X, &tri).unwrap();
        assert!((t - 3.0).abs() < 1e-5);
        // Beside it, behind it, and edge-on.
        assert!(ray_triangle(Vec3::new(0.0, 2.0, 0.0), Vec3::X, &tri).is_none());
        assert!(ray_triangle(Vec3::ZERO, -Vec3::X, &tri).is_none());
        assert!(ray_triangle(Vec3::ZERO, Vec3::Z, &tri).is_none());
    }

    #[test]
    fn quad_and_box_split_into_triangles() {
        let quad = Shape::Quad([Vec3::ZERO, Vec3::X, Vec3::new(1.0, 0.0, 1.0), Vec3::Z]);
        assert_eq!(quad.triangles().len(), 2);

        let boxed = Shape::Box { a: Vec3::new(1.0, 2.0, 0.0), b: Vec3::new(3.0, 6.0, 5.0) };
        let tris = boxed.triangles();
        assert_eq!(tris.len(), 12);
        // The triangles' total area is the box's surface area: 2 * (2*4 + 2*5 + 4*5).
        let area: f32 = tris.iter().map(|[a, b, c]| (*b - *a).cross(*c - *a).length() * 0.5).sum();
        assert!((area - 76.0).abs() < 1e-4, "{area}");
        // A ray from any side reaches the nearest face, wherever it crosses it.
        let center = boxed.center();
        assert_eq!(center, Vec3::new(2.0, 4.0, 2.5));
        for dir in [Vec3::X, -Vec3::X, Vec3::Y, -Vec3::Y, Vec3::Z, -Vec3::Z] {
            // Start 20 back from the centre, nudged sideways so the ray is off the face diagonals.
            let sideways = Vec3::new(0.3, 0.2, 0.1) * (Vec3::ONE - dir.abs());
            let origin = center - dir * 20.0 + sideways;
            let t = tris.iter().filter_map(|t| ray_triangle(origin, dir, t)).min_by(f32::total_cmp);
            let half = (Vec3::new(2.0, 4.0, 5.0) * 0.5).dot(dir.abs());
            assert!((t.unwrap() - (20.0 - half)).abs() < 1e-3, "{dir}: {t:?}");
        }
    }

    #[test]
    fn pick_finds_boxes_and_translate_moves_them() {
        let mut scene = empty_scene();
        scene.objects.push(plain(Shape::Box { a: Vec3::new(10.0, -1.0, 4.0), b: Vec3::new(12.0, 1.0, 6.0) }));
        let origin = Vec3::new(0.0, 0.0, 5.0);
        assert_eq!(scene.pick(origin, Vec3::X), Some(0));
        scene.objects[0].shape.translate(Vec3::new(0.0, 5.0, 0.0));
        assert_eq!(scene.pick(origin, Vec3::X), None);
        assert_eq!(scene.objects[0].shape.bounds(), (Vec3::new(10.0, 4.0, 4.0), Vec3::new(12.0, 6.0, 6.0)));
    }

    #[test]
    fn counts_estimate_and_limits() {
        let mut scene = empty_scene();
        for kind in ShapeKind::ALL {
            assert!(scene.add(kind).is_some());
        }
        assert_eq!((scene.sphere_count(), scene.triangle_count()), (1, 15));
        assert_eq!(scene.word_estimate(), 17 + 5 + 7 + 12 * 15);
        // New shapes stand on the ground.
        assert!(scene.objects.iter().all(|o| o.shape.bounds().0.z.abs() < 1e-5));

        // 15 + 4 * 12 = 63 triangles: room for one more triangle, not for a quad.
        for _ in 0..4 {
            assert!(scene.add(ShapeKind::Box).is_some());
        }
        assert!(scene.add(ShapeKind::Box).is_none());
        assert!(!scene.can_add(ShapeKind::Quad));
        assert!(scene.duplicate(3).is_none());
        assert!(scene.add(ShapeKind::Tri).is_some());
        assert!(!scene.can_add(ShapeKind::Tri));
        assert!(scene.duplicate(0).is_some(), "spheres have their own limit");
    }

    #[test]
    fn demo_scene_fits_the_chip() {
        assert!(Scene::default().word_estimate() <= SRAM_WORDS);
    }

    #[test]
    fn chip_material_picks_the_nearest_chip_material() {
        let m = |metallic, emissive| Material { metallic, emissive, ..Material::default() };
        assert_eq!(m(0.0, 0.0).chip_material(), ("matte", 0.0));
        assert_eq!(m(0.49, 0.0).chip_material(), ("matte", 0.0));
        assert_eq!(m(0.5, 0.0).chip_material(), ("mirror", 0.0));
        // Emissive wins over metallic.
        assert_eq!(m(1.0, 8.0).chip_material(), ("glow", 8.0));
    }

    #[test]
    fn num_trims_zeros() {
        assert_eq!(num(6.0), "6");
        assert_eq!(num(1.5), "1.5");
        assert_eq!(num(-0.125), "-0.125");
        assert_eq!(num(-0.0001), "0");
        assert_eq!(num(-12.0), "-12");
    }

    #[test]
    fn to_txt_matches_the_scene_file_format() {
        let mut scene = Scene::default();
        scene.sky.horizon = [235.0 / 255.0, 240.0 / 255.0, 1.0];
        scene.sky.top = [110.0 / 255.0, 160.0 / 255.0, 1.0];
        scene.ground = [[205.0 / 255.0, 205.0 / 255.0, 200.0 / 255.0], [120.0 / 255.0, 120.0 / 255.0, 130.0 / 255.0]];
        scene.objects.truncate(1);
        scene.objects[0].material.color = [220.0 / 255.0, 60.0 / 255.0, 50.0 / 255.0];
        let v = Vec3::new;
        let glow = Material { color: [1.0, 170.0 / 255.0, 80.0 / 255.0], metallic: 0.0, roughness: 0.5, emissive: 8.0 };
        let mirror = Material { color: [1.0, 1.0, 1.0], metallic: 1.0, roughness: 0.0, emissive: 0.0 };
        scene.objects.push(Object {
            name: "Lamp\n2".to_owned(),
            shape: Shape::Sphere { center: v(26.0, 18.0, 1.5), radius: 1.5 },
            material: glow.clone(),
        });
        scene.objects.push(Object {
            name: "t".to_owned(),
            shape: Shape::Tri([v(12.0, 48.0, 0.0), v(22.0, 48.0, 0.0), v(17.0, 53.0, 9.0)]),
            material: mirror,
        });
        scene.objects.push(Object {
            name: "q".to_owned(),
            shape: Shape::Quad([v(54.0, 18.0, 0.5), v(62.0, 24.0, 0.5), v(62.0, 24.0, 7.0), v(54.0, 18.0, 7.0)]),
            material: glow,
        });
        scene.objects.push(plain(Shape::Box { a: v(30.0, 52.0, 0.0), b: v(40.0, 62.0, 10.0) }));

        // Only the lines the compiler reads: no comments, no blanks.
        let txt = scene.to_txt();
        let lines: Vec<&str> = txt.lines().filter(|l| !l.is_empty() && !l.starts_with('#')).collect();
        assert_eq!(
            lines,
            [
                "image   512",
                "spp     32",
                "camera  5 5 12   45 -12 90",
                "sky     235 240 255  110 160 255",
                "ground  205 205 200",
                "checker 120 120 130",
                "sphere 32 22 6  6   matte  220 60 50  0",
                "sphere 26 18 1.5  1.5   glow  255 170 80  8",
                "tri 12 48 0  22 48 0  17 53 9   mirror  255 255 255  0",
                "quad 54 18 0.5  62 24 0.5  62 24 7  54 18 7   glow  255 170 80  8",
                "box 30 52 0  40 62 10   matte  204 204 204  0",
            ]
        );
        // A newline in a name must not break out of its comment line.
        assert!(txt.contains("# Lamp2\n"));
    }

    #[test]
    fn sky_strength_is_folded_into_the_sky_colours() {
        let mut scene = Scene::default();
        scene.sky.horizon = [0.5, 0.5, 0.5];
        scene.sky.top = [0.5, 0.5, 0.5];
        scene.sky.strength = 0.0;
        assert!(scene.to_txt().contains("sky     0 0 0  0 0 0\n"));
        scene.sky.strength = 100.0;
        assert!(scene.to_txt().contains("sky     255 255 255  255 255 255\n"));
    }

    #[test]
    fn srgb_round_values() {
        assert_eq!(srgb_to_linear(0.0), 0.0);
        assert!((srgb_to_linear(1.0) - 1.0).abs() < 1e-6);
        assert!((srgb_to_linear(0.735) - 0.5).abs() < 2e-3);
    }
}
