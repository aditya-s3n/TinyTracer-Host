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

#[derive(Clone, Debug, PartialEq)]
pub struct Sphere {
    pub name: String,
    pub center: Vec3,
    pub radius: f32,
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
    pub spheres: Vec<Sphere>,
}

impl Default for Scene {
    /// A small starter scene, framed like `sim/scene_demo.txt`.
    fn default() -> Self {
        let sphere = |name: &str, center: [f32; 3], radius, color, metallic, roughness, emissive| Sphere {
            name: name.to_owned(),
            center: Vec3::from(center),
            radius,
            material: Material { color, metallic, roughness, emissive },
        };
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
            spheres: vec![
                sphere("Red matte", [32.0, 22.0, 6.0], 6.0, [0.86, 0.24, 0.20], 0.0, 0.7, 0.0),
                sphere("Chrome", [28.0, 12.0, 4.0], 4.0, [0.95, 0.95, 0.95], 1.0, 0.05, 0.0),
                sphere("Lamp", [20.0, 26.0, 2.5], 2.5, [1.0, 0.67, 0.31], 0.0, 0.5, 1.5),
            ],
        }
    }
}

impl Scene {
    /// Add a sphere resting on the ground in front of the camera and return its index.
    /// Returns `None` when the scene is full.
    pub fn add_sphere(&mut self) -> Option<usize> {
        if self.spheres.len() >= MAX_SPHERES {
            return None;
        }
        let radius = 3.0;
        let f = self.camera.forward();
        let flat = Vec3::new(f.x, f.y, 0.0).normalize_or(Vec3::X);
        let mut center = self.camera.pos + flat * 25.0;
        center.z = radius;
        center = center.clamp(Vec3::splat(-POS_LIMIT), Vec3::splat(POS_LIMIT));
        self.spheres.push(Sphere {
            name: format!("Sphere {}", self.spheres.len() + 1),
            center,
            radius,
            material: Material::default(),
        });
        Some(self.spheres.len() - 1)
    }

    /// Copy a sphere, nudged sideways so it does not hide the original. Returns the new index.
    pub fn duplicate(&mut self, index: usize) -> Option<usize> {
        if self.spheres.len() >= MAX_SPHERES {
            return None;
        }
        let mut copy = self.spheres.get(index)?.clone();
        copy.name.push_str(" copy");
        copy.center.x = (copy.center.x + copy.radius * 2.0).min(POS_LIMIT);
        self.spheres.push(copy);
        Some(self.spheres.len() - 1)
    }

    /// Index of the nearest sphere hit by the ray, using the same maths as the shader.
    pub fn pick(&self, origin: Vec3, dir: Vec3) -> Option<usize> {
        let ground_t = if dir.z < 0.0 && origin.z > 0.0 { origin.z / -dir.z } else { f32::INFINITY };
        self.spheres
            .iter()
            .enumerate()
            .filter_map(|(i, s)| ray_sphere(origin, dir, s.center, s.radius).map(|t| (i, t)))
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
        out += "#   sphere x y z r   material R G B strength\n\n";
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

        for s in &self.spheres {
            let (material, strength) = s.material.chip_material();
            let name: String = s.name.chars().filter(|c| !c.is_control()).collect();
            out += &format!("\n# {name}\n");
            out += &format!(
                "sphere {} {} {}  {}   {material}  {}  {}\n",
                num(s.center.x), num(s.center.y), num(s.center.z), num(s.radius),
                rgb255(s.material.color), num(strength),
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

    #[test]
    fn pick_returns_nearest_sphere_and_respects_ground() {
        let mut scene = Scene::default();
        scene.spheres.clear();
        let at = |x: f32, z: f32| Sphere {
            name: String::new(),
            center: Vec3::new(x, 0.0, z),
            radius: 1.0,
            material: Material::default(),
        };
        scene.spheres.push(at(20.0, 5.0));
        scene.spheres.push(at(10.0, 5.0));
        // Buried sphere: the ground is hit first, so it cannot be picked.
        scene.spheres.push(at(10.0, -3.0));
        let origin = Vec3::new(0.0, 0.0, 5.0);
        assert_eq!(scene.pick(origin, Vec3::X), Some(1));
        assert_eq!(scene.pick(origin, Vec3::Y), None);
        let down = (Vec3::new(10.0, 0.0, -3.0) - origin).normalize();
        assert_eq!(scene.pick(origin, down), None);
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
        scene.spheres.truncate(1);
        scene.spheres[0].material.color = [220.0 / 255.0, 60.0 / 255.0, 50.0 / 255.0];
        scene.spheres.push(Sphere {
            name: "Lamp\n2".to_owned(),
            center: Vec3::new(26.0, 18.0, 1.5),
            radius: 1.5,
            material: Material { color: [1.0, 170.0 / 255.0, 80.0 / 255.0], metallic: 0.0, roughness: 0.5, emissive: 8.0 },
        });

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
