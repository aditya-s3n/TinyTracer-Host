//! Side panel: camera, sky and ground, the object list, and the selected object (design doc 6.3).
//!
//! The panel edits the scene it is handed and keeps no state of its own.

use eframe::egui;
use glam::Vec3;

use crate::ipc::{Status, TinyTracer, TOTAL_PIXELS};
use crate::scene::{
    Object, Scene, Shape, ShapeKind, MAX_EMISSIVE, MAX_FOV_DEG, MAX_PITCH_DEG, MIN_FOV_DEG, POS_LIMIT, SRAM_WORDS,
    VALID_SPP,
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
/// Returns true when a value changed.
fn position_row(ui: &mut egui::Ui, pos: &mut Vec3) -> bool {
    xyz_row(ui, pos, -POS_LIMIT..=POS_LIMIT)
}

fn xyz_row(ui: &mut egui::Ui, v: &mut Vec3, range: std::ops::RangeInclusive<f32>) -> bool {
    let mut changed = false;
    ui.horizontal(|ui| {
        for (label, value) in [("x", &mut v.x), ("y", &mut v.y), ("z", &mut v.z)] {
            let drag = egui::DragValue::new(value)
                .speed(0.1)
                .range(range.clone())
                .max_decimals(2)
                .prefix(format!("{label} "));
            changed |= ui.add(drag).changed();
        }
    });
    changed
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
        render_rows(ui, scene, tt);
        if let Some(message) = &tt.app_error {
            ui.colored_label(ERROR, format!("App error: {message}"));
        }
    });
}

/// Render and Cancel, the progress of the render, and where the image went.
/// Shown only while the TinyTracer app is connected.
fn render_rows(ui: &mut egui::Ui, scene: &Scene, tt: &mut TinyTracer) {
    if !tt.is_connected() {
        return;
    }
    ui.checkbox(&mut tt.use_stand_in_chip, "Use the stand-in chip (no hardware)").on_hover_text(
        "Off: render on the backend and port chosen in the TinyTracer window.\n\
         On: render on the fake chip that ships with the app, to test the whole path.",
    );
    ui.horizontal(|ui| {
        let render = ui
            .add_enabled(tt.can_render(), egui::Button::new("Render"))
            .on_disabled_hover_text("Compile first: the app has to accept the scene.");
        if render.clicked() {
            tt.render(scene);
        }
        if ui.add_enabled(tt.is_rendering(), egui::Button::new("Cancel")).clicked() {
            tt.cancel();
        }
        if !tt.run_state.is_empty() {
            ui.weak(&tt.run_state);
        }
    });

    if tt.is_rendering() {
        if let Some(progress) = &tt.progress {
            let eta = progress.eta_s.map_or(String::new(), |s| format!(", {s:.0} s left"));
            let text = format!("row {} of 512{eta}", progress.row + 1);
            ui.add(egui::ProgressBar::new(progress.pixels as f32 / TOTAL_PIXELS as f32).text(text));
        }
    }
    if let Some((png, bad)) = &tt.done {
        ui.colored_label(GOOD, format!("Rendered ({bad} bad pixels):"));
        ui.add(egui::Label::new(egui::RichText::new(png).weak()).wrap());
    }
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
    if selected.is_some_and(|i| i >= scene.objects.len()) {
        *selected = None;
    }

    egui::CollapsingHeader::new("Objects").default_open(true).show(ui, |ui| {
        ui.horizontal_wrapped(|ui| {
            ui.label("Add");
            for kind in ShapeKind::ALL {
                let button = ui
                    .add_enabled(scene.can_add(kind), egui::Button::new(kind.label()))
                    .on_disabled_hover_text("The preview is full.");
                if button.clicked() {
                    *selected = scene.add(kind);
                }
            }
        });
        ui.horizontal(|ui| {
            let can_duplicate = selected.is_some_and(|i| scene.can_add(scene.objects[i].shape.kind()));
            if ui.add_enabled(can_duplicate, egui::Button::new("Duplicate")).clicked() {
                *selected = selected.and_then(|i| scene.duplicate(i));
            }
            if ui.add_enabled(selected.is_some(), egui::Button::new("Delete")).clicked() {
                if let Some(i) = selected.take() {
                    scene.objects.remove(i);
                }
            }
        });

        memory_meter(ui, scene);

        egui::ScrollArea::vertical().id_salt("object list").max_height(160.0).show(ui, |ui| {
            for (i, object) in scene.objects.iter().enumerate() {
                let name = if object.name.is_empty() { "(unnamed)" } else { object.name.as_str() };
                let label = format!("{name}  ·  {}", object.shape.kind().keyword());
                // The index keeps rows with the same name apart.
                let row = ui.push_id(i, |ui| ui.selectable_label(*selected == Some(i), label));
                if row.inner.clicked() {
                    *selected = Some(i);
                }
            }
        });
    });
}

/// How much of the chip's 512 words the scene needs. Triangles are expensive:
/// 12 words each, so one box costs 144.
fn memory_meter(ui: &mut egui::Ui, scene: &Scene) {
    let words = scene.word_estimate();
    let text = format!(
        "about {words} of {SRAM_WORDS} words  ·  {} spheres, {} triangles",
        scene.sphere_count(),
        scene.triangle_count()
    );
    let label = if words > SRAM_WORDS {
        ui.colored_label(ERROR, format!("{text}: too big for the chip"))
    } else if words > SRAM_WORDS * 9 / 10 {
        ui.colored_label(WARNING, text)
    } else {
        ui.weak(text)
    };
    label.on_hover_text(
        "Estimate: 22 words of header and one bounding volume, 7 per sphere, 12 per triangle.\n\
         A quad is 2 triangles and a box is 12. The TinyTracer app gives the exact number.",
    );
}

fn selected_section(ui: &mut egui::Ui, scene: &mut Scene, selected: Option<usize>) {
    egui::CollapsingHeader::new("Selected").default_open(true).show(ui, |ui| {
        let Some(object) = selected.and_then(|i| scene.objects.get_mut(i)) else {
            ui.weak("Click an object in the view or in the list.");
            return;
        };
        object_editor(ui, object);
    });
}

fn object_editor(ui: &mut egui::Ui, object: &mut Object) {
    grid(ui, "selected", |ui| {
        ui.label("Name");
        ui.text_edit_singleline(&mut object.name);
        ui.end_row();

        shape_rows(ui, &mut object.shape);

        let material = &mut object.material;
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

    let (chip_material, _) = object.material.chip_material();
    ui.weak(format!("Written to the scene file as: {} {chip_material}", object.shape.kind().keyword()))
        .on_hover_text("The chip has matte, mirror, glass and glow. Emissive above 0 becomes glow; otherwise metallic 0.5 or more becomes mirror; otherwise matte.");

    let (lowest, highest) = object.shape.bounds();
    if lowest.z < 0.0 {
        ui.colored_label(WARNING, format!("Lowest point is at z = {:.2}: below the ground (z = 0).", lowest.z));
    }
    if lowest.min_element() < -POS_LIMIT || highest.max_element() > POS_LIMIT {
        ui.colored_label(WARNING, format!("Part of it is outside the chip's range of ±{POS_LIMIT}."));
    }
}

/// The rows that place and size a shape. Every shape has a Position that moves
/// it as a whole; below that come the numbers that are particular to the shape.
fn shape_rows(ui: &mut egui::Ui, shape: &mut Shape) {
    ui.label("Position");
    let before = shape.center();
    let mut center = before;
    if position_row(ui, &mut center) {
        shape.translate(center - before);
    }
    ui.end_row();

    match shape {
        Shape::Sphere { radius, .. } => {
            ui.label("Radius");
            ui.add(egui::DragValue::new(radius).speed(0.05).range(0.05..=POS_LIMIT).max_decimals(2));
            ui.end_row();
        }
        Shape::Box { a, b } => {
            // The file stores two opposite corners; a size is easier to think in.
            ui.label("Size");
            let center = (*a + *b) * 0.5;
            let mut size = (*b - *a).abs();
            if xyz_row(ui, &mut size, 0.05..=2.0 * POS_LIMIT) {
                *a = center - size * 0.5;
                *b = center + size * 0.5;
            }
            ui.end_row();
        }
        Shape::Tri(corners) => corner_rows(ui, corners),
        Shape::Quad(corners) => {
            corner_rows(ui, corners);
            ui.label("");
            ui.weak("Corners go in order around the edge.");
            ui.end_row();
        }
    }
}

fn corner_rows(ui: &mut egui::Ui, corners: &mut [Vec3]) {
    for (i, corner) in corners.iter_mut().enumerate() {
        ui.label(format!("Corner {}", i + 1));
        position_row(ui, corner);
        ui.end_row();
    }
}
