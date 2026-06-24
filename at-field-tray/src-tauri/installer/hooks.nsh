; AT-Field -- NSIS installer hooks
;
; installMode is "perMachine", so the installer runs elevated. That lets us
; register (and, on uninstall, remove) the AT-Field watchdog Windows service
; as PART OF SETUP. The single UAC consent the user approves when launching
; the installer covers the whole install -- app + watchdog -- so there's no
; separate in-app "Install watchdog" + second UAC step. The in-app
; Install/Uninstall buttons remain available as a repair / fallback path.
;
; The install/uninstall PowerShell scripts ship as PyInstaller `datas`, so in
; the staged bundle they live under resources\atfield\_internal\scripts\.
; install_service.ps1 auto-detects the vendored LibreHardwareMonitor.exe +
; atfield-sensors.exe sitting one level up in resources\atfield\.

!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "Registering the AT-Field watchdog service..."
  Push $0
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\atfield\_internal\scripts\install_service.ps1" -BundledExe "$INSTDIR\resources\atfield\atfield-service.exe"'
  Pop $0
  DetailPrint "Watchdog service installer finished (exit code $0)."
  Pop $0
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "Removing the AT-Field watchdog service..."
  Push $0
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\atfield\_internal\scripts\uninstall_service.ps1"'
  Pop $0
  Pop $0
!macroend
