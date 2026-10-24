#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;
use tauri::{Manager, WindowEvent, menu::{Menu, MenuItem}, tray::{TrayIconBuilder, TrayIconEvent, MouseButton, MouseButtonState}};
use tauri_plugin_shell::{ShellExt, process::{CommandChild, CommandEvent}};
use tauri_plugin_dialog::DialogExt;
mod notifications;

#[derive(Default)]
struct Backend {
    connection: Mutex<Option<serde_json::Value>>,
    child: Mutex<Option<CommandChild>>,
    notification_status: Mutex<Option<serde_json::Value>>,
    opening_pending: Mutex<bool>,
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

#[tauri::command]
async fn save_export(app: tauri::AppHandle, name: String, content: String, format: String) -> Result<Option<String>, String> {
    if !["json", "csv"].contains(&format.as_str()) || content.len() > 30_000_000 {
        return Err("无效的导出文件".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let picked = app.dialog().file().set_title("导出 AssetGrove 数据").set_file_name(&name)
            .add_filter(format.to_uppercase(), &[format.as_str()]).blocking_save_file();
        let Some(picked) = picked else { return Ok(None); };
        let mut path = picked.into_path().map_err(|e|e.to_string())?;
        if path.extension().is_none() {
            path.set_extension(&format);
        }
        if path.extension().and_then(|s|s.to_str()).map(|s|s.eq_ignore_ascii_case(&format)) != Some(true) {
            return Err(format!("请选择 .{format} 文件，以免覆盖其他类型的文件"));
        }
        // 文件名由系统对话框选择；原子替换，写入失败时保留既有文件。
        let directory = path.parent().ok_or("导出路径无效")?;
        let temporary = directory.join(format!(".assetgrove-export-{}-{}.tmp", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|e|e.to_string())?.as_nanos()));
        use std::io::Write;
        let result = (|| -> std::io::Result<()> {
            let mut file = std::fs::OpenOptions::new().create_new(true).write(true).open(&temporary)?;
            file.write_all(content.as_bytes())?;
            file.sync_all()?;
            drop(file);
            std::fs::rename(&temporary, &path)
        })();
        if let Err(error) = result {
            let _ = std::fs::remove_file(&temporary);
            return Err(format!("导出失败，请检查文件是否被占用及文件夹权限：{error}"));
        }
        Ok(Some(path.to_string_lossy().into_owned()))
    }).await.map_err(|e|e.to_string())?
}

#[tauri::command]
async fn notification_status(app: tauri::AppHandle, send_test: bool) -> serde_json::Value {
    let handle = app.clone();
    let result = tauri::async_runtime::spawn_blocking(move || notifications::deliver(&handle,"AssetGrove 通知测试","关注条件满足时，将在此显示股价与股息提醒。", !send_test)).await;
    let status = match result {
        Ok(Ok(message)) => serde_json::json!({"ok":true,"message":message}),
        Ok(Err(error)) => serde_json::json!({"ok":false,"message":error}),
        Err(error) => serde_json::json!({"ok":false,"message":error.to_string()}),
    };
    *app.state::<Backend>().notification_status.lock().unwrap() = Some(status.clone());
    status
}

#[tauri::command]
fn last_notification_status(state: tauri::State<Backend>) -> Option<serde_json::Value> {
    state.notification_status.lock().ok()?.clone()
}

fn flush_opening(app: &tauri::AppHandle) {
    let state = app.state::<Backend>();
    let mut pending = state.opening_pending.lock().unwrap();
    if *pending {
        if let Some(child) = state.child.lock().unwrap().as_mut() {
            if child.write(b"opened\n").is_ok() { *pending = false; }
        }
    }
}

fn show_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        *app.state::<Backend>().opening_pending.lock().unwrap() = true;
        flush_opening(app);
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
        .invoke_handler(tauri::generate_handler![backend_connection, quit_application, choose_data_directory, save_export, notification_status, last_notification_status])
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
            flush_opening(app.handle());
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let mut pending = Vec::new();
                while let Some(event) = receiver.recv().await {
                    if let CommandEvent::Stdout(bytes) = event {
                        pending.extend_from_slice(&bytes);
                        while let Some(end) = pending.iter().position(|&b|b == b'\n') {
                          let line: Vec<_> = pending.drain(..=end).collect();
                          if let Ok(message) = serde_json::from_slice::<serde_json::Value>(&line) {
                            match message["event"].as_str() {
                                Some("connection") => {
                                    *handle.state::<Backend>().connection.lock().unwrap() = Some(message);
                                },
                                Some("notification") => {
                                    let app = handle.clone();
                                    let delivered = tauri::async_runtime::spawn_blocking(move || notifications::deliver(&app,
                                        message["title"].as_str().unwrap_or("AssetGrove"), message["body"].as_str().unwrap_or(""), false)).await;
                                    let result = match delivered {
                                        Ok(Ok(message)) => serde_json::json!({"ok":true,"message":message}),
                                        Ok(Err(error)) => serde_json::json!({"ok":false,"message":error}),
                                        Err(error) => serde_json::json!({"ok":false,"message":error.to_string()}),
                                    };
                                    *handle.state::<Backend>().notification_status.lock().unwrap() = Some(result);
                                },
                                _ => (),
                            }
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
