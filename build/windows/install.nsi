; The Telescope Net Node Agent — Windows NSIS Installer
;
; Build prerequisites:
;   NSIS 3.x  (https://nsis.sourceforge.io/)
;   NSSM      (https://nssm.cc/) — placed at build\windows\nssm\nssm.exe
;   Bundled exe at dist\TelescopeNetNode.exe (built with PyInstaller)
;
; Build command (from repo root):
;   makensis build\windows\install.nsi

!define PRODUCT_NAME      "The Telescope Net Node Agent"
!define PRODUCT_VERSION   "1.0.0"
!define PRODUCT_PUBLISHER "The Telescope Net"
!define PRODUCT_URL       "https://telescopenet.org"
!define SERVICE_NAME      "TelescopeNetNode"
!define INSTALL_DIR       "$PROGRAMFILES64\TelescopeNet\NodeAgent"
!define DATA_DIR          "$APPDATA\TelescopeNet\NodeAgent"
!define UNINSTALL_KEY     "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "..\..\dist\TelescopeNetNode-Setup.exe"
InstallDir "${INSTALL_DIR}"
InstallDirRegKey HKLM "${UNINSTALL_KEY}" "InstallLocation"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUnInstDetails show

;-----------------------------------------------------------------------------
; MUI2 pages
;-----------------------------------------------------------------------------
!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "LogicLib.nsh"

!define MUI_ABORTWARNING
!define MUI_ICON   "..\..\build\icon.ico"
!define MUI_UNICON "..\..\build\icon.ico"

; Installer pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

; Uninstaller pages
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

;-----------------------------------------------------------------------------
; No activation-code page: there are no activation codes. The member signs in
; to the desktop app after install and uses "Connect telescope", which links
; the node and installs its credentials on the local agent.
;-----------------------------------------------------------------------------
; Installer sections
;-----------------------------------------------------------------------------
Section "Node Agent (required)" SecMain
  SectionIn RO

  ; Create directories
  CreateDirectory "${INSTALL_DIR}"
  CreateDirectory "${DATA_DIR}"
  CreateDirectory "${DATA_DIR}\logs"
  CreateDirectory "${DATA_DIR}\data"
  CreateDirectory "${DATA_DIR}\fits_export"
  CreateDirectory "${DATA_DIR}\aavso_submissions"

  ; Copy main executable
  SetOutPath "${INSTALL_DIR}"
  File "..\..\dist\TelescopeNetNode.exe"

  ; Copy NSSM (Windows Service wrapper)
  File "nssm\nssm.exe"

  ; Write config.yaml from the template as-is — nothing to substitute.
  SetOutPath "${DATA_DIR}"
  File "..\..\build\config.template.yaml"
  CopyFiles "${DATA_DIR}\config.template.yaml" "${DATA_DIR}\config.yaml"
  Delete "${DATA_DIR}\config.template.yaml"

  ; Prevent system sleep during overnight operation
  nsExec::ExecToLog 'powercfg /change standby-timeout-ac 0'
  nsExec::ExecToLog 'powercfg /change hibernate-timeout-ac 0'
  nsExec::ExecToLog 'powercfg /change disk-timeout-ac 0'

  ; Register the MCP server with Claude Desktop, so members never hand-edit
  ; claude_desktop_config.json. That step is invisible in an installer, easy to
  ; get subtly wrong, and fails silently -- the tools simply never appear.
  ;
  ; SetShellVarContext current so $APPDATA resolves to the installing user's
  ; roaming profile, not All Users: the config is per-user and Claude will not
  ; look anywhere else. Restored immediately afterwards, since the rest of this
  ; section is machine-wide.
  ;
  ; The agent does the work itself rather than a Python script -- Windows
  ; members have no interpreter. It merges one key and refuses a config it
  ; cannot parse, so other MCP servers are never disturbed. Never fatal:
  ; Claude Desktop not being installed is a normal outcome.
  SetShellVarContext current
  nsExec::ExecToLog '"${INSTALL_DIR}\TelescopeNetNode.exe" --register-mcp \
    --data-dir "${DATA_DIR}" \
    --mcp-config "$APPDATA\Claude\claude_desktop_config.json"'
  SetShellVarContext all

  ; Install as a Windows Service via NSSM
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" install "${SERVICE_NAME}" \
    "${INSTALL_DIR}\TelescopeNetNode.exe"'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" set "${SERVICE_NAME}" \
    AppParameters "--no-browser --data-dir \"${DATA_DIR}\""'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" set "${SERVICE_NAME}" \
    AppDirectory "${DATA_DIR}"'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" set "${SERVICE_NAME}" \
    DisplayName "${PRODUCT_NAME}"'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" set "${SERVICE_NAME}" \
    Description "The Telescope Net automated telescope node agent"'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" set "${SERVICE_NAME}" \
    Start SERVICE_AUTO_START'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" set "${SERVICE_NAME}" \
    AppStdout "${DATA_DIR}\logs\node_agent.log"'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" set "${SERVICE_NAME}" \
    AppStderr "${DATA_DIR}\logs\node_agent_error.log"'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" set "${SERVICE_NAME}" \
    AppRotateFiles 1'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" set "${SERVICE_NAME}" \
    AppRotateBytes 5242880'

  ; Start the service
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" start "${SERVICE_NAME}"'

  ; Create Start Menu shortcut to the dashboard
  CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
  CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}\Dashboard.lnk" \
    "http://localhost:5173" "" "" 0
  CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}\Data Folder.lnk" \
    "${DATA_DIR}" "" "" 0
  CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk" \
    "$INSTDIR\Uninstall.exe" "" "" 0

  ; Write uninstaller
  WriteUninstaller "${INSTALL_DIR}\Uninstall.exe"

  ; Registry keys for Add/Remove Programs
  WriteRegStr   HKLM "${UNINSTALL_KEY}" "DisplayName"      "${PRODUCT_NAME}"
  WriteRegStr   HKLM "${UNINSTALL_KEY}" "UninstallString"  "${INSTALL_DIR}\Uninstall.exe"
  WriteRegStr   HKLM "${UNINSTALL_KEY}" "InstallLocation"  "${INSTALL_DIR}"
  WriteRegStr   HKLM "${UNINSTALL_KEY}" "Publisher"        "${PRODUCT_PUBLISHER}"
  WriteRegStr   HKLM "${UNINSTALL_KEY}" "URLInfoAbout"     "${PRODUCT_URL}"
  WriteRegStr   HKLM "${UNINSTALL_KEY}" "DisplayVersion"   "${PRODUCT_VERSION}"
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoModify"         1
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoRepair"         1
SectionEnd

;-----------------------------------------------------------------------------
; Uninstaller
;-----------------------------------------------------------------------------
Section "Uninstall"
  ; Deregister from Claude Desktop first, while the agent still exists to do
  ; it -- otherwise the member is left with a tool entry pointing at a binary
  ; that has been deleted. Removes only our own key.
  SetShellVarContext current
  nsExec::ExecToLog '"${INSTALL_DIR}\TelescopeNetNode.exe" --deregister-mcp \
    --mcp-config "$APPDATA\Claude\claude_desktop_config.json"'
  SetShellVarContext all

  ; Stop and remove the service
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" stop "${SERVICE_NAME}"'
  nsExec::ExecToLog '"${INSTALL_DIR}\nssm.exe" remove "${SERVICE_NAME}" confirm'

  ; Restore default power settings
  nsExec::ExecToLog 'powercfg /change standby-timeout-ac 30'
  nsExec::ExecToLog 'powercfg /change hibernate-timeout-ac 60'

  ; Remove files (but keep the user's data directory)
  Delete "${INSTALL_DIR}\TelescopeNetNode.exe"
  Delete "${INSTALL_DIR}\nssm.exe"
  Delete "${INSTALL_DIR}\Uninstall.exe"
  RMDir  "${INSTALL_DIR}"
  RMDir  "$PROGRAMFILES64\TelescopeNet"

  ; Remove Start Menu
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\Dashboard.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\Data Folder.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk"
  RMDir  "$SMPROGRAMS\${PRODUCT_NAME}"

  ; Remove registry keys
  DeleteRegKey HKLM "${UNINSTALL_KEY}"
SectionEnd

;-----------------------------------------------------------------------------
; NSIS helper: StrRep
;-----------------------------------------------------------------------------
!include "StrFunc.nsh"
${StrRep}
