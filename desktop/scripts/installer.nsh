; Per-user Windows Explorer context menu integration ("Open with codelet")
; Uses HKCU so installation and uninstallation require zero administrator privileges.

!macro customInstall
  ; Right-click on any folder
  WriteRegStr HKCU "Software\Classes\Directory\shell\codelet" "" "使用 codelet 打开"
  WriteRegStr HKCU "Software\Classes\Directory\shell\codelet" "Icon" "$INSTDIR\codelet.exe"
  WriteRegStr HKCU "Software\Classes\Directory\shell\codelet\command" "" '"$INSTDIR\codelet.exe" "%1"'

  ; Right-click on background inside any folder
  WriteRegStr HKCU "Software\Classes\Directory\Background\shell\codelet" "" "使用 codelet 打开"
  WriteRegStr HKCU "Software\Classes\Directory\Background\shell\codelet" "Icon" "$INSTDIR\codelet.exe"
  WriteRegStr HKCU "Software\Classes\Directory\Background\shell\codelet\command" "" '"$INSTDIR\codelet.exe" "%V"'
!macroend

!macro customUnInstall
  DeleteRegKey HKCU "Software\Classes\Directory\shell\codelet"
  DeleteRegKey HKCU "Software\Classes\Directory\Background\shell\codelet"
!macroend
