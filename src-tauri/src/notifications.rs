use tauri::AppHandle;

#[cfg(windows)]
pub fn deliver(app: &AppHandle, title: &str, body: &str, test_only: bool) -> Result<String, String> {
    use std::{io::Write, os::windows::process::CommandExt, process::{Command, Stdio}, time::{Duration, Instant}};
    // 直接取得 Windows 通知设置与投递结果，避免插件后台线程吞掉失败信息。
    let script = r#"
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
  $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
  $identity = 'HKCU:\Software\Classes\AppUserModelId\' + $request.app_id
  if (-not (Test-Path -LiteralPath $identity)) { New-Item -Path $identity -Force > $null }
  $registered = Get-ItemProperty -LiteralPath $identity
  if (-not $registered.DisplayName) { New-ItemProperty -LiteralPath $identity -Name DisplayName -Value 'AssetGrove' -PropertyType String -Force > $null }
  if (-not $registered.IconUri) { New-ItemProperty -LiteralPath $identity -Name IconUri -Value $request.executable -PropertyType String -Force > $null }
  $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($request.app_id)
  $setting = $notifier.Setting.ToString()
  if ($setting -ne 'Enabled') { throw ('Windows notification setting: ' + $setting) }
  if (-not $request.test_only) {
    $title = [System.Security.SecurityElement]::Escape([string]$request.title)
    $body = [System.Security.SecurityElement]::Escape([string]$request.body)
    $xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
    $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' + $title + '</text><text>' + $body + '</text></binding></visual><audio src="ms-winsoundevent:Notification.Default"/></toast>')
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $toast.Tag = [Guid]::NewGuid().ToString('N').Substring(0,16)
    $toast.Group = 'AssetGrove'
    $toast.ExpirationTime = [DateTimeOffset]::Now.AddMinutes(10)
    $notifier.Show($toast)
    $found = $false
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
      $history = [Windows.UI.Notifications.ToastNotificationManager]::History.GetHistory($request.app_id)
      if ($history | Where-Object { $_.Tag -eq $toast.Tag }) { $found = $true; break }
      Start-Sleep -Milliseconds 100
    }
    if (-not $found) { throw 'Windows did not retain the toast. Check notification banners and Do Not Disturb settings.' }
  }
  [Console]::Out.Write('Enabled')
} catch {
  [Console]::Error.Write($_.Exception.Message)
  exit 1
}
"#;
    let mut child = Command::new("powershell.exe")
        .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script])
        .creation_flags(0x08000000)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| e.to_string())?;
    let request = serde_json::json!({"app_id":app.config().identifier,"title":title,"body":body,"test_only":test_only,"executable":std::env::current_exe().map_err(|e|e.to_string())?.to_string_lossy()});
    child.stdin.take().ok_or("无法连接系统通知服务")?.write_all(request.to_string().as_bytes()).map_err(|e| e.to_string())?;
    let deadline = Instant::now() + Duration::from_secs(10);
    while child.try_wait().map_err(|e| e.to_string())?.is_none() {
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Windows 通知服务响应超时".into());
        }
        std::thread::sleep(Duration::from_millis(30));
    }
    let result = child.wait_with_output().map_err(|e| e.to_string())?;
    if !result.status.success() {
        return Err(String::from_utf8_lossy(&result.stderr).trim().to_string());
    }
    Ok(if test_only { "Windows 允许通知" } else { "Windows 通知中心已收到消息" }.into())
}

#[cfg(not(windows))]
pub fn deliver(app: &AppHandle, title: &str, body: &str, test_only: bool) -> Result<String, String> {
    use tauri_plugin_notification::NotificationExt;
    if test_only {
        return Ok(format!("{:?}", app.notification().permission_state().map_err(|e|e.to_string())?));
    }
    app.notification().builder().title(title).body(body).show().map_err(|e|e.to_string())?;
    Ok("已提交到系统通知中心".into())
}
