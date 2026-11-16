//! Link to the TinyTracer app (design doc 6.5 and 7).
//!
//! The designer starts the Python app as a child process with its stdin and
//! stdout piped, and the two exchange JSON lines: one JSON object per line.
//! A background thread reads the app's replies and hands them to the main
//! thread through a channel, so the UI never waits on the pipe.
//!
//! The designer sends hello, scene, render and cancel. The app answers with
//! ready, checked, state, progress, done and error.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};

use eframe::egui;
use serde::{Deserialize, Serialize};

use crate::scene::Scene;

/// Version of the message protocol. Both sides must agree.
pub const PROTOCOL: u32 = 1;
/// How long the app gets to exit by itself before it is killed.
const EXIT_GRACE: Duration = Duration::from_millis(1500);

/// Messages the designer sends.
#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum ToApp<'a> {
    Hello { protocol: u32 },
    Scene { rev: u64, text: &'a str },
    /// Render the scene sent last. Without a backend the app uses the link
    /// settings chosen in its own window.
    Render {
        spp: u8,
        passes: u32,
        #[serde(skip_serializing_if = "Option::is_none")]
        backend: Option<&'a str>,
        #[serde(skip_serializing_if = "Option::is_none")]
        port: Option<&'a str>,
    },
    Cancel,
}

/// Messages the app sends. Types this build does not know are ignored, so the
/// app can add messages without breaking the designer.
#[derive(Deserialize, Debug, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
enum FromApp {
    Ready {
        protocol: u32,
        #[serde(default)]
        tthost: String,
    },
    Checked(Checked),
    /// idle, compiling, uploading, rendering, processing, resetting, done or error.
    State {
        state: String,
    },
    /// Sent once per finished image row.
    Progress(Progress),
    Done {
        png: String,
        #[serde(default)]
        bad: u32,
    },
    Error {
        message: String,
    },
    #[serde(other)]
    Unknown,
}

/// The Python compiler's verdict on one scene.
#[derive(Deserialize, Debug, PartialEq, Clone)]
pub struct Checked {
    pub rev: u64,
    #[serde(default)]
    pub words: u32,
    #[serde(default)]
    pub bvs: u32,
    #[serde(default)]
    pub errors: Vec<CheckError>,
    #[serde(default)]
    pub warnings: Vec<String>,
}

impl Checked {
    pub fn ok(&self) -> bool {
        self.errors.is_empty()
    }
}

#[derive(Deserialize, Debug, PartialEq, Clone)]
pub struct Progress {
    pub row: u32,
    pub pixels: u32,
    /// Seconds left, once the app can tell.
    pub eta_s: Option<f32>,
    #[serde(default)]
    pub bad: u32,
}

/// Pixels in one chip frame.
pub const TOTAL_PIXELS: u32 = 512 * 512;
/// What `render` asks for when the stand-in chip box is ticked: the app's "sim"
/// backend running the fake chip that ships with it.
const STAND_IN_BACKEND: (&str, &str) = ("sim", "fake");

#[derive(Deserialize, Debug, PartialEq, Clone)]
pub struct CheckError {
    /// Line of the scene file, when the compiler knows it.
    pub line: Option<u32>,
    pub rule: String,
}

/// What the reader thread passes to the main thread.
enum Event {
    Line(String),
    /// The app closed its stdout: it exited or crashed.
    Closed,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Status {
    NotRunning,
    /// Process started, waiting for its `ready`.
    Starting,
    Connected { tthost: String },
    /// Could not start, refused the handshake, or exited by itself.
    Failed(String),
}

struct Running {
    child: Child,
    /// `None` once closed, which is how the app is told to exit.
    stdin: Option<ChildStdin>,
    events: Receiver<Event>,
}

impl Running {
    /// Close the app's stdin so it exits, and kill it if it does not.
    fn shut_down(mut self) {
        self.stdin = None;
        let deadline = Instant::now() + EXIT_GRACE;
        while Instant::now() < deadline {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Everything about the TinyTracer side: the scene file, the child process and its replies.
pub struct TinyTracer {
    /// Folder that holds the Python app's `main.py`.
    root: PathBuf,
    /// Where Compile writes the scene. Edited in the side panel.
    pub output_path: String,
    /// Result of the last Compile: what was written, or why it failed.
    pub compile_result: Option<Result<String, String>>,
    /// Goes up by one with every scene sent, so replies can be matched to it.
    rev: u64,
    running: Option<Running>,
    pub status: Status,
    /// The app's verdict on the scene sent last. Cleared when a newer scene is sent.
    pub checked: Option<Checked>,
    /// The last `error` message from the app.
    pub app_error: Option<String>,
    /// Render on the app's stand-in chip instead of the link chosen in its window.
    pub use_stand_in_chip: bool,
    /// The app's run state: "idle", "rendering", ... Empty until it reports one.
    pub run_state: String,
    pub progress: Option<Progress>,
    /// Where the last finished render was saved, and its bad-pixel count.
    pub done: Option<(String, u32)>,
}

impl TinyTracer {
    pub fn new() -> Self {
        // The Python app sits one folder above this crate. TTHOST_ROOT overrides
        // that for a designer binary that was moved somewhere else.
        let root = std::env::var_os("TTHOST_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join(".."));
        let root = root.canonicalize().unwrap_or(root);
        let output_path = root.join("scenes").join("designer_scene.txt").display().to_string();
        Self {
            root,
            output_path,
            compile_result: None,
            rev: 0,
            running: None,
            status: Status::NotRunning,
            checked: None,
            app_error: None,
            use_stand_in_chip: false,
            run_state: String::new(),
            progress: None,
            done: None,
        }
    }

    pub fn is_connected(&self) -> bool {
        matches!(self.status, Status::Connected { .. })
    }

    /// True while the app is busy with a render.
    pub fn is_rendering(&self) -> bool {
        matches!(self.run_state.as_str(), "compiling" | "uploading" | "rendering" | "processing" | "resetting")
    }

    /// Render is allowed once the app has accepted the scene sent last and is not busy.
    pub fn can_render(&self) -> bool {
        self.is_connected() && !self.is_rendering() && self.checked.as_ref().is_some_and(Checked::ok)
    }

    /// Ask the app to render the scene sent by the last Compile.
    pub fn render(&mut self, scene: &Scene) {
        if !self.can_render() {
            return;
        }
        self.app_error = None;
        self.progress = None;
        self.done = None;
        let (backend, port) = match self.use_stand_in_chip {
            true => (Some(STAND_IN_BACKEND.0), Some(STAND_IN_BACKEND.1)),
            false => (None, None),
        };
        self.send(&ToApp::Render { spp: scene.spp, passes: 1, backend, port });
    }

    /// Ask the app to stop the current render and reset the chip.
    pub fn cancel(&mut self) {
        self.send(&ToApp::Cancel);
    }

    pub fn is_running(&self) -> bool {
        self.running.is_some()
    }

    /// Write the scene to `output_path` as a scene .txt file. If the TinyTracer
    /// app is connected, send it the same text so it can check and render it.
    pub fn compile(&mut self, scene: &Scene) {
        let text = scene.to_txt();
        let path = Path::new(self.output_path.trim());
        let written = path
            .parent()
            .filter(|dir| !dir.as_os_str().is_empty())
            .map_or(Ok(()), std::fs::create_dir_all)
            .and_then(|()| std::fs::write(path, &text));
        self.compile_result = Some(match written {
            Ok(()) => Ok(format!("Wrote {} ({} objects)", path.display(), scene.objects.len())),
            Err(e) => Err(format!("Could not write {}: {e}", path.display())),
        });

        if self.is_connected() && !self.is_rendering() {
            self.rev += 1;
            self.checked = None;
            self.app_error = None;
            self.send(&ToApp::Scene { rev: self.rev, text: &text });
        }
    }

    /// Start the TinyTracer app as a child process and send `hello`.
    pub fn start(&mut self, ctx: &egui::Context) {
        if self.running.is_some() {
            return;
        }
        self.checked = None;
        self.app_error = None;
        self.run_state.clear();
        self.progress = None;
        self.done = None;

        // Prefer the project's virtual environment, where imgui-bundle is installed.
        let venv_python = self.root.join(".venv").join("bin").join("python");
        let python = if venv_python.exists() { venv_python } else { PathBuf::from("python3") };
        let spawned = Command::new(&python)
            .args(["main.py", "--ipc"])
            .current_dir(&self.root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // The app logs to stderr; it shows up in the designer's terminal.
            .stderr(Stdio::inherit())
            .spawn();
        let mut child = match spawned {
            Ok(child) => child,
            Err(e) => {
                self.status = Status::Failed(format!("could not start {}: {e}", python.display()));
                return;
            }
        };

        let stdin = child.stdin.take();
        let stdout = child.stdout.take().expect("stdout was piped");
        let (tx, events) = mpsc::channel();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if tx.send(Event::Line(line)).is_err() {
                    return; // the designer dropped the link
                }
                ctx.request_repaint();
            }
            let _ = tx.send(Event::Closed);
            ctx.request_repaint();
        });

        self.running = Some(Running { child, stdin, events });
        self.status = Status::Starting;
        self.send(&ToApp::Hello { protocol: PROTOCOL });
    }

    /// Stop the TinyTracer app.
    pub fn stop(&mut self) {
        if let Some(running) = self.running.take() {
            running.shut_down();
        }
        self.status = Status::NotRunning;
        self.run_state.clear();
    }

    /// Handle everything the app has sent since the last frame. Call once per frame.
    pub fn poll(&mut self) {
        loop {
            let event = match &self.running {
                Some(running) => running.events.try_recv(),
                None => return,
            };
            match event {
                Ok(Event::Line(line)) => self.handle_line(&line),
                Ok(Event::Closed) | Err(mpsc::TryRecvError::Disconnected) => {
                    self.fail("the TinyTracer app exited".to_owned());
                    return;
                }
                Err(mpsc::TryRecvError::Empty) => return,
            }
        }
    }

    fn handle_line(&mut self, line: &str) {
        match serde_json::from_str::<FromApp>(line) {
            Ok(FromApp::Ready { protocol, tthost }) => {
                if protocol == PROTOCOL {
                    self.status = Status::Connected { tthost };
                } else {
                    self.fail(format!("the app speaks protocol {protocol}, the designer speaks {PROTOCOL}"));
                }
            }
            // A reply to an older scene says nothing about the current one.
            Ok(FromApp::Checked(checked)) if checked.rev == self.rev => self.checked = Some(checked),
            Ok(FromApp::Checked(_)) | Ok(FromApp::Unknown) => {}
            Ok(FromApp::State { state }) => self.run_state = state,
            Ok(FromApp::Progress(progress)) => self.progress = Some(progress),
            Ok(FromApp::Done { png, bad }) => self.done = Some((png, bad)),
            Ok(FromApp::Error { message }) => self.app_error = Some(message),
            Err(e) => eprintln!("designer: ignoring a line from the TinyTracer app ({e}): {line}"),
        }
    }

    fn send(&mut self, message: &ToApp) {
        let Some(stdin) = self.running.as_mut().and_then(|r| r.stdin.as_mut()) else {
            return;
        };
        let line = serde_json::to_string(message).expect("messages are plain data");
        if let Err(e) = writeln!(stdin, "{line}").and_then(|()| stdin.flush()) {
            self.fail(format!("lost the pipe to the TinyTracer app: {e}"));
        }
    }

    fn fail(&mut self, reason: String) {
        if let Some(running) = self.running.take() {
            running.shut_down();
        }
        self.run_state.clear();
        self.status = Status::Failed(reason);
    }
}

impl Drop for TinyTracer {
    /// Take the app down with the designer.
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn messages_match_the_design_doc() {
        let hello = serde_json::to_string(&ToApp::Hello { protocol: 1 }).unwrap();
        assert_eq!(hello, r#"{"type":"hello","protocol":1}"#);
        let scene = serde_json::to_string(&ToApp::Scene { rev: 12, text: "image 512\nspp 32\n" }).unwrap();
        assert_eq!(scene, r#"{"type":"scene","rev":12,"text":"image 512\nspp 32\n"}"#);
    }

    #[test]
    fn replies_from_the_design_doc_parse() {
        let ready: FromApp = serde_json::from_str(r#"{"type":"ready","protocol":1,"tthost":"0.1.0"}"#).unwrap();
        assert_eq!(ready, FromApp::Ready { protocol: 1, tthost: "0.1.0".to_owned() });

        let checked: FromApp = serde_json::from_str(
            r#"{"type":"checked","rev":13,"words":295,"bvs":4,"errors":[{"line":31,"rule":"glass is for spheres only"}],"warnings":[]}"#,
        )
        .unwrap();
        let FromApp::Checked(checked) = checked else { panic!("{checked:?}") };
        assert_eq!((checked.rev, checked.words, checked.bvs), (13, 295, 4));
        assert_eq!(checked.errors, [CheckError { line: Some(31), rule: "glass is for spheres only".to_owned() }]);

        // The compiler does not always know the line.
        let no_line: FromApp =
            serde_json::from_str(r#"{"type":"checked","rev":1,"errors":[{"line":null,"rule":"x"}]}"#).unwrap();
        assert!(matches!(no_line, FromApp::Checked(c) if c.errors[0].line.is_none()));
    }

    #[test]
    fn unknown_message_types_are_ignored() {
        let msg: FromApp = serde_json::from_str(r#"{"type":"from_the_future","x":1}"#).unwrap();
        assert_eq!(msg, FromApp::Unknown);
    }

    #[test]
    fn render_messages_match_the_design_doc() {
        let plain = ToApp::Render { spp: 32, passes: 1, backend: None, port: None };
        assert_eq!(serde_json::to_string(&plain).unwrap(), r#"{"type":"render","spp":32,"passes":1}"#);
        let stand_in = ToApp::Render { spp: 8, passes: 1, backend: Some("sim"), port: Some("fake") };
        assert_eq!(
            serde_json::to_string(&stand_in).unwrap(),
            r#"{"type":"render","spp":8,"passes":1,"backend":"sim","port":"fake"}"#
        );
        assert_eq!(serde_json::to_string(&ToApp::Cancel).unwrap(), r#"{"type":"cancel"}"#);
    }

    #[test]
    fn a_render_is_followed_from_state_to_done() {
        let mut tt = TinyTracer::new();
        tt.status = Status::Connected { tthost: String::new() };
        assert!(!tt.can_render(), "nothing compiled yet");
        tt.rev = 1;
        tt.handle_line(r#"{"type":"checked","rev":1,"words":43,"bvs":1,"errors":[],"warnings":[]}"#);
        assert!(tt.can_render());

        tt.handle_line(r#"{"type":"state","state":"rendering"}"#);
        assert!(tt.is_rendering() && !tt.can_render());
        tt.handle_line(r#"{"type":"progress","row":0,"pixels":512,"eta_s":null,"bad":0}"#);
        tt.handle_line(r#"{"type":"progress","row":1,"pixels":1024,"eta_s":401.5,"bad":2}"#);
        let progress = tt.progress.clone().unwrap();
        assert_eq!((progress.row, progress.pixels, progress.eta_s, progress.bad), (1, 1024, Some(401.5), 2));

        tt.handle_line(r#"{"type":"state","state":"done"}"#);
        tt.handle_line(r#"{"type":"done","png":"/runs/x/render.png","run_dir":"/runs/x","bad":2}"#);
        assert_eq!(tt.done, Some(("/runs/x/render.png".to_owned(), 2)));
        assert!(tt.can_render(), "ready for the next render");

        // A scene the app rejected cannot be rendered.
        tt.handle_line(r#"{"type":"checked","rev":1,"errors":[{"line":3,"rule":"x"}]}"#);
        assert!(!tt.can_render());
    }

    #[test]
    fn only_the_reply_to_the_latest_scene_counts() {
        let mut tt = TinyTracer::new();
        tt.rev = 5;
        tt.handle_line(r#"{"type":"checked","rev":4,"words":10}"#);
        assert!(tt.checked.is_none());
        tt.handle_line(r#"{"type":"checked","rev":5,"words":38}"#);
        assert_eq!(tt.checked.as_ref().map(|c| c.words), Some(38));
        tt.handle_line(r#"{"type":"error","message":"nope"}"#);
        assert_eq!(tt.app_error.as_deref(), Some("nope"));
        tt.handle_line("not json");
    }

    #[test]
    fn compile_writes_the_scene_file() {
        let dir = std::env::temp_dir().join(format!("designer_test_{}", std::process::id()));
        let path = dir.join("nested").join("scene.txt");
        let mut tt = TinyTracer::new();
        tt.output_path = path.display().to_string();
        let scene = Scene::default();
        tt.compile(&scene);
        assert!(matches!(tt.compile_result, Some(Ok(_))), "{:?}", tt.compile_result);
        assert_eq!(std::fs::read_to_string(&path).unwrap(), scene.to_txt());
        std::fs::remove_dir_all(&dir).unwrap();

        tt.output_path = "/proc/definitely/not/writable.txt".to_owned();
        tt.compile(&scene);
        assert!(matches!(tt.compile_result, Some(Err(_))));
    }
}
