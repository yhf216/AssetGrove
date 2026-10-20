; UTF-8. Only remove ledger data when explicitly selected during a real uninstall.
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    SetShellVarContext current
    ; This directory can also contain installed binaries or unrelated files.
    ; Delete only the application's known database files; never recurse here.
    ClearErrors
    Delete "$INSTDIR\data\assetgrove.sqlite3"
    Delete "$INSTDIR\data\assetgrove.sqlite3-journal"
    Delete "$INSTDIR\data\assetgrove.sqlite3-wal"
    Delete "$INSTDIR\data\assetgrove.sqlite3-shm"
    Delete "$INSTDIR\settings.json"
    RMDir "$INSTDIR\data"
    IfFileExists "$INSTDIR\data\assetgrove.sqlite3" 0 +2
      MessageBox MB_OK|MB_ICONEXCLAMATION "账本文件仍被占用，未能删除。请完全退出 AssetGrove 后重试。"
    Delete "$LOCALAPPDATA\AssetGrove\assetgrove.sqlite3"
    Delete "$LOCALAPPDATA\AssetGrove\assetgrove.sqlite3-journal"
    Delete "$LOCALAPPDATA\AssetGrove\assetgrove.sqlite3-wal"
    Delete "$LOCALAPPDATA\AssetGrove\assetgrove.sqlite3-shm"
    IfFileExists "$LOCALAPPDATA\AssetGrove\assetgrove.sqlite3" 0 +2
      MessageBox MB_OK|MB_ICONEXCLAMATION "账本文件仍被占用，未能删除。请完全退出 AssetGrove 后手动删除 %LOCALAPPDATA%\AssetGrove\assetgrove.sqlite3。"
    RMDir "$LOCALAPPDATA\AssetGrove"
    RMDir "$INSTDIR"
  ${EndIf}
!macroend
