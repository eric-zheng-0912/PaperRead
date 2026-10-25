; Custom identity never invokes the legacy PaperRead uninstaller.
; Fixed per-user program location is disjoint from persistent user data.
!ifndef PAPERREAD_INSTALL_DIR_NAME
  !define PAPERREAD_INSTALL_DIR_NAME "PaperRead-Custom"
!endif
!macro customInit
  SetShellVarContext current
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PAPERREAD_INSTALL_DIR_NAME}"
!macroend

!macro customUnInit
  StrCmp $INSTDIR "$LOCALAPPDATA\Programs\${PAPERREAD_INSTALL_DIR_NAME}" +3
    MessageBox MB_ICONSTOP "Unexpected installation path; refusing to remove files."
    Abort
  ; Even explicit delete-app-data is rejected. Do not remove a user's profile.
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "--delete-app-data" $R1
  ${IfNot} ${Errors}
    MessageBox MB_ICONSTOP "User data deletion is not supported by PaperRead Custom."
    Abort
  ${EndIf}
!macroend

!macro customRemoveFiles
  ; Never recursively remove INSTDIR or an arbitrary user-selected directory.
  Delete "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
  Delete "$INSTDIR\${UNINSTALL_FILENAME}"
  ; Exact root-level files present in the audited Electron Windows payload.
  Delete "$INSTDIR\chrome_100_percent.pak"
  Delete "$INSTDIR\chrome_200_percent.pak"
  Delete "$INSTDIR\d3dcompiler_47.dll"
  Delete "$INSTDIR\dxcompiler.dll"
  Delete "$INSTDIR\dxil.dll"
  Delete "$INSTDIR\ffmpeg.dll"
  Delete "$INSTDIR\icudtl.dat"
  Delete "$INSTDIR\resources.pak"
  Delete "$INSTDIR\snapshot_blob.bin"
  Delete "$INSTDIR\v8_context_snapshot.bin"
  Delete "$INSTDIR\vk_swiftshader.dll"
  Delete "$INSTDIR\vulkan-1.dll"
  ; Exact payload and NSIS-generated root files found in the uninstall audit.
  Delete "$INSTDIR\LICENSE.electron.txt"
  Delete "$INSTDIR\LICENSES.chromium.html"
  Delete "$INSTDIR\uninstallerIcon.ico"
  Delete "$INSTDIR\version"
  Delete "$INSTDIR\vk_swiftshader_icd.json"
  ; electron-builder scopes this exact installer copy to this product identity.
  ; Do not remove the cache directory or any other channel's installer.
  Delete "$LOCALAPPDATA\${APP_INSTALLER_STORE_FILE}"
  ; No user data is supported within these application-owned resource trees.
  RMDir /r "$INSTDIR\resources"
  RMDir /r "$INSTDIR\locales"
  RMDir "$INSTDIR"
!macroend
