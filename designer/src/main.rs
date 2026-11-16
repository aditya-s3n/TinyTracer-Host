//! TinyTracer scene designer: app shell (design doc 6.1).
//!
//! One struct owns all state. egui is immediate mode, so every frame the shell
//! hands the scene to the side panel (which edits it) and to the viewport
//! (which draws it and moves the camera).
//!
//! Not built yet: rendering over IPC, scene file loading and undo.

mod ipc;
mod panel;
mod scene;
mod viewport;

use eframe::egui;

use ipc::TinyTracer;
use scene::Scene;
use viewport::Viewport;

struct DesignerApp {
    scene: Scene,
    /// Index into `scene.spheres`.
    selected: Option<usize>,
    viewport: Viewport,
    /// Scene file output and the link to the TinyTracer app.
    tinytracer: TinyTracer,
}

impl DesignerApp {
    fn new(cc: &eframe::CreationContext<'_>) -> Result<Self, String> {
        let viewport = Viewport::new(cc).ok_or("the scene designer needs the wgpu renderer")?;
        Ok(Self { scene: Scene::default(), selected: None, viewport, tinytracer: TinyTracer::new() })
    }
}

impl eframe::App for DesignerApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.tinytracer.poll();

        egui::Panel::right("side panel").resizable(true).default_size(340.0).show(ui, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                panel::show(ui, &mut self.scene, &mut self.selected, &mut self.tinytracer);
            });
        });

        let backdrop = egui::Frame::NONE.fill(egui::Color32::from_gray(16));
        egui::CentralPanel::default().frame(backdrop).show(ui, |ui| {
            self.viewport.show(ui, &mut self.scene, &mut self.selected);
        });
    }
}

fn main() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("TinyTracer Scene Designer")
            .with_inner_size([1180.0, 820.0])
            .with_min_inner_size([640.0, 400.0]),
        ..Default::default()
    };
    eframe::run_native(
        "TinyTracer Scene Designer",
        options,
        Box::new(|cc| Ok(Box::new(DesignerApp::new(cc)?))),
    )
}
