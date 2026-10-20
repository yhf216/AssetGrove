#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;
use tauri::{Manager, WindowEvent, menu::{Menu, MenuItem}, tray::{TrayIconBuilder, TrayIconEvent, MouseButton, MouseButtonState}};
use tauri_plugin_shell::{ShellExt, process::{CommandChild, CommandEvent}};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_dialog::DialogExt;

#[derive(Default)]
struct Backend {
    connection: Mutex<Option<serde_json::Value>>,
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
fn backend_connection(state: tauri::State<Backend>) -> Option<serde_json::Value> {
    state.connection.lock().ok()?.clone()
}

#[tauri::command]
fn quit_application(app: tauri::AppHandle) { app.exit(0); }

#[tauri::command]
async fn choose_data_directory(app: tauri::AppHandle) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file().set_title("选择数据文件夹").blocking_pick_folder()
            .map(|path| path.into_path().map(|p| p.to_string_lossy().into_owned()).map_err(|e| e.to_string()))
            .transpose()
    }).await.map_err(|e| e.to_string())?
}

fn show_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn main() {
    // 仅清理本应用的进程环境；保持用户的系统代理设置不变。
    let proxy_keys: Vec<_> = std::env::vars_os()
        .map(|(key, _)| key)
        .filter(|key| key.to_string_lossy().to_lowercase().ends_with("_proxy"))
        .collect();
    for key in proxy_keys { std::env::remove_var(key); }
    std::env::set_var("NO_PROXY", "*");
    let browser_args = std::env::var("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS").unwrap_or_default();
    std::env::set_var("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", format!("{} --no-proxy-server", browser_args));
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_window(app)))
        .manage(Backend::default())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, Some(vec!["--background"])))
        .invoke_handler(tauri::generate_handler![backend_connection, quit_application, choose_data_directory])
        .setup(|app| {
            let show = MenuItem::with_id(app, "show", "打开 AssetGrove", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "完全退出（停止监控）", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            let mut tray = TrayIconBuilder::new()
                .tooltip("AssetGrove · 正在本地运行")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => show_window(app),
                    "quit" => app.exit(0),
                    _ => (),
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        show_window(tray.app_handle());
                    }
                });
            if let Some(icon) = app.default_window_icon() { tray = tray.icon(icon.clone()); }
            tray.build(app)?;
            if !std::env::args().any(|arg| arg == "--background") {
                show_window(app.handle());
            }
            let mut command = app.shell().sidecar("assetgrove-backend")?.args(["--desktop"]);
            if let Ok(executable) = std::env::current_exe() {
                if let Some(directory) = executable.parent() {
                    command = command.env("ASSETGROVE_APP_DIR", directory.to_string_lossy().as_ref());
                }
            }
            let (mut receiver, child) = command.spawn()?;
            *app.state::<Backend>().child.lock().unwrap() = Some(child);
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = receiver.recv().await {
                    if let CommandEvent::Stdout(bytes) = event {
                        if let Ok(message) = serde_json::from_slice::<serde_json::Value>(&bytes) {
                            match message["event"].as_str() {
                                Some("connection") => {
                                    *handle.state::<Backend>().connection.lock().unwrap() = Some(message);
                                },
                                Some("notification") => {
                                    let _ = handle.notification().builder()
                                        .title(message["title"].as_str().unwrap_or("AssetGrove"))
                                        .body(message["body"].as_str().unwrap_or(""))
                                        .show();
                                },
                                _ => (),
                            }
                        }
                    }
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let window = window.clone();
                // 退出关闭回调后再提交隐藏操作，避免窗口消息处理中的重入。
                tauri::async_runtime::spawn_blocking(move || {
                    if let Err(error) = window.hide() {
                        eprintln!("Unable to hide AssetGrove window: {error}");
                    }
                });
            }
        })
        .build(tauri::generate_context!())
        .expect("AssetGrove desktop failed to initialize");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(mut child) = handle.state::<Backend>().child.lock().unwrap().take() {
                let _ = child.write(b"shutdown\n");
                std::thread::sleep(std::time::Duration::from_millis(350));
                let _ = child.kill();
            }
        }
    });
}
