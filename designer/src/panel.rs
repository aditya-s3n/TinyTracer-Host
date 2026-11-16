//! Side panel: camera, sky and ground, the sphere list, and the selected sphere (design doc 6.3).
//!
//! The panel edits the scene it is handed and keeps no state of its own.

use eframe::egui;
use glam::Vec3;

use crate::ipc::{Status, TinyTracer};
use crate::scene::{
    Scene, Sphere, MAX_EMISSIVE, MAX_FOV_DEG, MAX_PITCH_DEG, MAX_SPHERES, MIN_FOV_DEG, POS_LIMIT, VALID_SPP,
};

const WARNING: egui::Color32 = egui::Color32::from_rgb(230, 160, 40);
const ERROR: egui::Color32 = egui::Color32::from_rgb(235, 90, 80);
const GOOD: egui::Color32 = egui::Color32::from_rgb(90, 200, 110);

pub fn show(ui: &mut egui::Ui, scene: &mut Scene, selected: &mut Option<usize>, tinytracer: &mut TinyTracer) {
    tinytracer_section(ui, scene, tinytracer);
    camera_section(ui, scene);
    sky_section(ui, scene);
    objects_section(ui, scene, selected);
    selected_section(ui, scene, *selected);
}

/// Three drag boxes on one row for a position, limited to the chip's +/-256 range.
fn position_row(ui: &mut egui::Ui, pos: &mut Vec3) {
    ui.horizontal(|ui| {
        for (label, value) in [("x", &mut pos.x), ("y", &mut pos.y), ("z", &mut pos.z)] {
            ui.add(
                egui::DragValue::new(value)
                    .speed(0.1)
                    .range(-POS_LIMIT..=POS_LIMIT)
                    .max_decimals(2)
                    .prefix(format!("{label} ")),
            );
        }
    });
}

fn grid(ui: &mut egui::Ui, id: &str, contents: impl FnOnce(&mut egui::Ui)) {
    egui::Grid::new(id).num_columns(2).spacing([12.0, 6.0]).show(ui, contents);
}

/// Compile the scene to a .txt file, start and stop the TinyTracer app, and show what it replied.
fn tinytracer_section(ui: &mut egui::Ui, scene: &mut Scene, tt: &mut TinyTracer) {
    egui::CollapsingHeader::new("TinyTracer").default_open(true).show(ui, |ui| {
        grid(ui, "tinytracer", |ui| {
            ui.label("Samples")
                .on_hover_text("Samples per pixel for the chip render. The preview does not use it.");
            egui::ComboBox::from_id_salt("spp").selected_text(scene.spp.to_string()).show_ui(ui, |ui| {
                for spp in VALID_SPP {
                    ui.selectable_value(&mut scene.spp, spp, spp.to_string());
                }
            });
            ui.end_row();

            ui.label("Scene file");
            ui.add(egui::TextEdit::singleline(&mut tt.output_path).desired_width(f32::INFINITY));
            ui.end_row();
        });

        ui.horizontal(|ui| {
            let compile = ui.button("Compile").on_hover_text(
                "Write the scene file. If the TinyTracer app is connected, it is sent the scene too.",
            );
            if compile.clicked() {
                tt.compile(scene);
            }
            if tt.is_running() {
                if ui.button("Stop TinyTracer").clicked() {
                    tt.stop();
                }
            } else if ui.button("Start TinyTracer").clicked() {
                tt.start(ui.ctx());
            }
        });

        match &tt.compile_result {
            Some(Ok(message)) => ui.weak(message),
            Some(Err(message)) => ui.colored_label(ERROR, message),
            None => ui.weak("Not compiled yet."),
        };

        ui.horizontal_wrapped(|ui| {
            ui.label("TinyTracer app:");
            match &tt.status {
                Status::NotRunning => ui.weak("not running"),
                Status::Starting => ui.colored_label(WARNING, "starting…"),
                Status::Connected { tthost } => ui.colored_label(GOOD, format!("connected (tthost {tthost})")),
                Status::Failed(reason) => ui.colored_label(ERROR, reason),
            };
        });

        if let Some(checked) = &tt.checked {
            if checked.errors.is_empty() {
                ui.colored_label(GOOD, format!("Scene accepted: {} of 512 words, {} BVs", checked.words, checked.bvs));
            }
            for error in &checked.errors {
                let place = error.line.map_or(String::new(), |line| format!("line {line}: "));
                ui.colored_label(ERROR, format!("Scene rejected: {place}{}", error.rule));
            }
            for warning in &checked.warnings {
                ui.colored_label(WARNING, format!("Warning: {warning}"));
            }
        }
        if let Some(message) = &tt.app_error {
            ui.colored_label(ERROR, format!("App error: {message}"));
        }
    });
}

fn camera_section(ui: &mut egui::Ui, scene: &mut Scene) {
    let camera = &mut scene.camera;
    egui::CollapsingHeader::new("Camera").default_open(true).show(ui, |ui| {
        grid(ui, "camera", |ui| {
            ui.label("Location");
            position_row(ui, &mut camera.pos);
            ui.end_row();

            ui.label("FOV");
            ui.add(egui::Slider::new(&mut camera.fov_deg, MIN_FOV_DEG..=MAX_FOV_DEG).suffix("°"));
            ui.end_row();

            ui.label("Yaw");
            ui.add(egui::DragValue::new(&mut camera.yaw_deg).speed(0.5).max_decimals(1).suffix("°"));
            ui.end_row();

            ui.label("Pitch");
            ui.add(
                egui::DragValue::new(&mut camera.pitch_deg)
                    .speed(0.5)
                    .range(-MAX_PITCH_DEG..=MAX_PITCH_DEG)
                    .max_decimals(1)
                    .suffix("°"),
            );
            ui.end_row();
        });
    });
    camera.clamp();
}

fn sky_section(ui: &mut egui::Ui, scene: &mut Scene) {
    egui::CollapsingHeader::new("Sky and ground").default_open(true).show(ui, |ui| {
        grid(ui, "sky", |ui| {
            ui.label("Sky top");
            ui.color_edit_button_rgb(&mut scene.sky.top);
            ui.end_row();

            ui.label("Sky horizon");
            ui.color_edit_button_rgb(&mut scene.sky.horizon);
            ui.end_row();

            ui.label("Sky strength");
            ui.add(egui::Slider::new(&mut scene.sky.strength, 0.0..=4.0));
            ui.end_row();

            ui.label("Ground checker");
            ui.horizontal(|ui| {
                let [a, b] = &mut scene.ground;
                ui.color_edit_button_rgb(a);
                ui.color_edit_button_rgb(b);
            });
            ui.end_row();
        });
    });
}

fn objects_section(ui: &mut egui::Ui, scene: &mut Scene, selected: &mut Option<usize>) {
    // A stale index can be left behind by a delete; drop it before anything uses it.
    if selected.is_some_and(|i| i >= scene.spheres.len()) {
        *selected = None;
    }

    egui::CollapsingHeader::new("Objects").default_open(true).show(ui, |ui| {
        ui.horizontal(|ui| {
            let room = scene.spheres.len() < MAX_SPHERES;
            if ui.add_enabled(room, egui::Button::new("Add sphere")).clicked() {
                *selected = scene.add_sphere();
            }
            if ui.add_enabled(room && selected.is_some(), egui::Button::new("Duplicate")).clicked() {
                *selected = selected.and_then(|i| scene.duplicate(i));
            }
            if ui.add_enabled(selected.is_some(), egui::Button::new("Delete")).clicked() {
                if let Some(i) = selected.take() {
                    scene.spheres.remove(i);
                }
            }
        });
        ui.weak(format!("{} of {MAX_SPHERES} spheres", scene.spheres.len()));

        egui::ScrollArea::vertical().id_salt("object list").max_height(160.0).show(ui, |ui| {
            for (i, sphere) in scene.spheres.iter().enumerate() {
                let label = if sphere.name.is_empty() { "(unnamed)" } else { sphere.name.as_str() };
                // The index keeps rows with the same name apart.
                let row = ui.push_id(i, |ui| ui.selectable_label(*selected == Some(i), label));
                if row.inner.clicked() {
                    *selected = Some(i);
                }
            }
        });
    });
}

fn selected_section(ui: &mut egui::Ui, scene: &mut Scene, selected: Option<usize>) {
    egui::CollapsingHeader::new("Selected").default_open(true).show(ui, |ui| {
        let Some(sphere) = selected.and_then(|i| scene.spheres.get_mut(i)) else {
            ui.weak("Click a sphere in the view or in the list.");
            return;
        };
        sphere_editor(ui, sphere);
    });
}

fn sphere_editor(ui: &mut egui::Ui, sphere: &mut Sphere) {
    grid(ui, "selected", |ui| {
        ui.label("Name");
        ui.text_edit_singleline(&mut sphere.name);
        ui.end_row();

        ui.label("Position");
        position_row(ui, &mut sphere.center);
        ui.end_row();

        ui.label("Radius");
        ui.add(egui::DragValue::new(&mut sphere.radius).speed(0.05).range(0.05..=POS_LIMIT).max_decimals(2));
        ui.end_row();

        let material = &mut sphere.material;
        ui.label("Colour");
        ui.color_edit_button_rgb(&mut material.color);
        ui.end_row();

        ui.label("Metallic");
        ui.add(egui::Slider::new(&mut material.metallic, 0.0..=1.0));
        ui.end_row();

        ui.label("Roughness");
        ui.add(egui::Slider::new(&mut material.roughness, 0.0..=1.0));
        ui.end_row();

        ui.label("Emissive");
        ui.add(egui::Slider::new(&mut material.emissive, 0.0..=MAX_EMISSIVE));
        ui.end_row();
    });

    let (chip_material, _) = sphere.material.chip_material();
    ui.weak(format!("Written to the scene file as: {chip_material}"))
        .on_hover_text("The chip has matte, mirror, glass and glow. Emissive above 0 becomes glow; otherwise metallic 0.5 or more becomes mirror; otherwise matte.");

    let bottom = sphere.center.z - sphere.radius;
    if bottom < 0.0 {
        ui.colored_label(WARNING, format!("Bottom is at z = {bottom:.2}: below the ground (z = 0)."));
    }
}
