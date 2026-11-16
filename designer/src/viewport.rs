//! Viewport: the square raycast preview in the central panel (design doc 6.4).
//!
//! The picture is drawn by a fragment shader (`raycast.wgsl`) through an egui
//! paint callback. Every frame the whole scene is packed into one uniform
//! buffer; nothing else lives on the GPU.

use bytemuck::{Pod, Zeroable};
use eframe::egui;
use eframe::egui_wgpu::{self, wgpu};
use glam::Vec3;

use crate::scene::{srgb_to_linear3, Scene, MAX_SPHERES};

const LOOK_DEG_PER_POINT: f32 = 0.25;
const MOVE_SPEED: f32 = 12.0;
const FAST_MULTIPLIER: f32 = 4.0;
const FOV_DEG_PER_SCROLL_POINT: f32 = 0.05;

#[repr(C)]
#[derive(Clone, Copy, Pod, Zeroable)]
struct GpuSphere {
    center_radius: [f32; 4],
    color_metallic: [f32; 4],
    rough_emissive: [f32; 4],
}

/// Must match `Globals` in raycast.wgsl field for field.
#[repr(C)]
#[derive(Clone, Copy, Pod, Zeroable)]
struct Globals {
    origin: [f32; 4],
    forward: [f32; 4],
    right: [f32; 4],
    up: [f32; 4],
    sky_horizon: [f32; 4],
    sky_top: [f32; 4],
    ground_a: [f32; 4],
    ground_b: [f32; 4],
    spheres: [GpuSphere; MAX_SPHERES],
}

fn vec4(v: impl Into<[f32; 3]>, w: f32) -> [f32; 4] {
    let [x, y, z] = v.into();
    [x, y, z, w]
}

impl Globals {
    fn new(scene: &Scene, selected: Option<usize>, encode_srgb: bool, side_pixels: f32) -> Box<Self> {
        let (f, r, u) = scene.camera.basis();
        let count = scene.spheres.len().min(MAX_SPHERES);
        let mut globals = Box::new(Self {
            origin: vec4(scene.camera.pos, count as f32),
            forward: vec4(f, selected.map_or(-1.0, |i| i as f32)),
            right: vec4(r, if encode_srgb { 1.0 } else { 0.0 }),
            // The image spans -1..1, so one pixel is 2 / side wide.
            up: vec4(u, 2.0 / side_pixels.max(1.0)),
            sky_horizon: vec4(srgb_to_linear3(scene.sky.horizon), scene.sky.strength),
            sky_top: vec4(srgb_to_linear3(scene.sky.top), 0.0),
            ground_a: vec4(srgb_to_linear3(scene.ground[0]), 0.0),
            ground_b: vec4(srgb_to_linear3(scene.ground[1]), 0.0),
            spheres: [GpuSphere::zeroed(); MAX_SPHERES],
        });
        for (gpu, s) in globals.spheres.iter_mut().zip(&scene.spheres) {
            *gpu = GpuSphere {
                // A zero radius would divide by zero when the shader builds the normal.
                center_radius: vec4(s.center, s.radius.max(1e-3)),
                color_metallic: vec4(srgb_to_linear3(s.material.color), s.material.metallic),
                rough_emissive: [s.material.roughness, s.material.emissive, 0.0, 0.0],
            };
        }
        globals
    }
}

/// GPU objects, created once and kept in egui's callback resources.
struct Resources {
    pipeline: wgpu::RenderPipeline,
    bind_group: wgpu::BindGroup,
    uniforms: wgpu::Buffer,
}

struct RaycastCallback {
    globals: Box<Globals>,
}

impl egui_wgpu::CallbackTrait for RaycastCallback {
    fn prepare(
        &self,
        _device: &wgpu::Device,
        queue: &wgpu::Queue,
        _screen_descriptor: &egui_wgpu::ScreenDescriptor,
        _egui_encoder: &mut wgpu::CommandEncoder,
        resources: &mut egui_wgpu::CallbackResources,
    ) -> Vec<wgpu::CommandBuffer> {
        if let Some(res) = resources.get::<Resources>() {
            queue.write_buffer(&res.uniforms, 0, bytemuck::bytes_of(&*self.globals));
        }
        Vec::new()
    }

    fn paint(
        &self,
        _info: egui::PaintCallbackInfo,
        render_pass: &mut wgpu::RenderPass<'static>,
        resources: &egui_wgpu::CallbackResources,
    ) {
        // egui has already set the GPU viewport to our rectangle.
        if let Some(res) = resources.get::<Resources>() {
            render_pass.set_pipeline(&res.pipeline);
            render_pass.set_bind_group(0, &res.bind_group, &[]);
            render_pass.draw(0..3, 0..1);
        }
    }
}

pub struct Viewport {
    /// True when the window surface stores plain bytes, so the shader has to apply sRGB gamma itself.
    encode_srgb: bool,
}

impl Viewport {
    /// Build the shader pipeline. Returns `None` if eframe is not running on wgpu.
    pub fn new(cc: &eframe::CreationContext<'_>) -> Option<Self> {
        let render_state = cc.wgpu_render_state.as_ref()?;
        let device = &render_state.device;

        let shader = device.create_shader_module(wgpu::include_wgsl!("raycast.wgsl"));
        let pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("raycast"),
            // Derive the layout from the shader: one uniform buffer at group 0, binding 0.
            layout: None,
            vertex: wgpu::VertexState {
                module: &shader,
                entry_point: Some("vs_main"),
                compilation_options: Default::default(),
                buffers: &[],
            },
            fragment: Some(wgpu::FragmentState {
                module: &shader,
                entry_point: Some("fs_main"),
                compilation_options: Default::default(),
                targets: &[Some(render_state.target_format.into())],
            }),
            primitive: wgpu::PrimitiveState::default(),
            depth_stencil: None,
            multisample: wgpu::MultisampleState::default(),
            multiview_mask: None,
            cache: None,
        });

        let uniforms = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("raycast globals"),
            size: size_of::<Globals>() as u64,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("raycast globals"),
            layout: &pipeline.get_bind_group_layout(0),
            entries: &[wgpu::BindGroupEntry { binding: 0, resource: uniforms.as_entire_binding() }],
        });

        render_state
            .renderer
            .write()
            .callback_resources
            .insert(Resources { pipeline, bind_group, uniforms });

        Some(Self { encode_srgb: !render_state.target_format.is_srgb() })
    }

    /// Draw the preview and handle camera controls and click-to-select.
    pub fn show(&self, ui: &mut egui::Ui, scene: &mut Scene, selected: &mut Option<usize>) {
        // The chip only renders square images, so the preview is the largest centred square.
        let available = ui.available_rect_before_wrap();
        let side = available.width().min(available.height()).max(16.0);
        let rect = egui::Rect::from_center_size(available.center(), egui::Vec2::splat(side));
        let response = ui.allocate_rect(rect, egui::Sense::click_and_drag());

        self.camera_controls(ui, &response, scene);

        if response.clicked() {
            if let Some(pos) = response.interact_pointer_pos() {
                let a = (pos.x - rect.center().x) / (side * 0.5);
                let b = (rect.center().y - pos.y) / (side * 0.5);
                *selected = scene.pick(scene.camera.pos, scene.camera.ray_dir(a, b));
            }
        }

        let side_pixels = side * ui.ctx().pixels_per_point();
        let globals = Globals::new(scene, *selected, self.encode_srgb, side_pixels);
        ui.painter()
            .add(egui_wgpu::Callback::new_paint_callback(rect, RaycastCallback { globals }));

        // Controls hint on a dark strip so it stays readable over a bright ground.
        let hint = "Right-drag: look  ·  hold right + WASD/QE: move (Shift = fast)  ·  scroll: FOV  ·  click: select";
        let painter = ui.painter();
        let galley = painter.layout_no_wrap(hint.to_owned(), egui::FontId::proportional(12.0), egui::Color32::WHITE);
        let text_pos = rect.left_bottom() + egui::vec2(10.0, -8.0 - galley.size().y);
        let strip = egui::Rect::from_min_size(text_pos, galley.size()).expand2(egui::vec2(6.0, 3.0));
        painter.rect_filled(strip, 4.0, egui::Color32::from_black_alpha(150));
        painter.galley(text_pos, galley, egui::Color32::WHITE);
    }

    fn camera_controls(&self, ui: &egui::Ui, response: &egui::Response, scene: &mut Scene) {
        let camera = &mut scene.camera;

        if response.dragged_by(egui::PointerButton::Secondary) {
            // Look around: the scene follows the mouse like grabbing a panorama.
            let delta = response.drag_delta();
            camera.yaw_deg += delta.x * LOOK_DEG_PER_POINT;
            camera.pitch_deg += delta.y * LOOK_DEG_PER_POINT;

            // Fly while the right button is held, so typing in the panel never moves the camera.
            let (step, dt) = ui.input(|i| {
                let axis = |plus, minus| i.key_down(plus) as i32 as f32 - i.key_down(minus) as i32 as f32;
                let speed = MOVE_SPEED * if i.modifiers.shift { FAST_MULTIPLIER } else { 1.0 };
                let step = camera.forward() * axis(egui::Key::W, egui::Key::S)
                    + camera.right_unit() * axis(egui::Key::D, egui::Key::A)
                    + Vec3::Z * axis(egui::Key::E, egui::Key::Q);
                (step * speed, i.stable_dt.min(0.1))
            });
            camera.pos += step * dt;
            // Keys held with a still mouse produce no events, so keep the frames coming.
            ui.ctx().request_repaint();
        }

        if response.hovered() {
            let scroll = ui.input(|i| i.smooth_scroll_delta.y);
            camera.fov_deg -= scroll * FOV_DEG_PER_SCROLL_POINT;
        }

        camera.clamp();
    }
}
