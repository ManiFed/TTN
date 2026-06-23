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
Page custom ActivationCodePage ActivationCodePageLeave
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

; Uninstaller pages
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

;-----------------------------------------------------------------------------
; Activation code custom page
;-----------------------------------------------------------------------------
Var ActivationCodeCtrl
Var ActivationCode

Function ActivationCodePage
  !insertmacro MUI_HEADER_TEXT "Activation Code" \
    "Enter the Node Activation Code from your The Telescope Net account."

  nsDialogs::Create 1018
  Pop $0

  ${NSD_CreateLabel} 0 0 100% 40u \
    "Your activation code looks like: BS-2025-XXXXXXXX$\r$\n$\r$\nGet your code at telescopenet.org/account after signing up."
  Pop $0

  ${NSD_CreateText} 0 50u 100% 14u $ActivationCode
  Pop $ActivationCodeCtrl

  nsDialogs::Show
FunctionEnd

Function ActivationCodePageLeave
  ${NSD_GetText} $ActivationCodeCtrl $ActivationCode
  ; Validate format: must start with BS- or be blank (skip for now)
  StrLen $R0 $ActivationCode
  ${If} $R0 > 0
    StrCpy $R1 $ActivationCode 3
    ${If} $R1 != "BS-"
      MessageBox MB_OK|MB_ICONEXCLAMATION \
        "Activation codes start with 'BS-'. Please check your code and try again.$\r$\n$\r$\nYou can skip this and enter the code later in config.yaml."
    ${EndIf}
  ${EndIf}
FunctionEnd

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

  ; Write config.yaml from template, substituting the activation code
  SetOutPath "${DATA_DIR}"
  File "..\..\build\config.template.yaml"
  CopyFiles "${DATA_DIR}\config.template.yaml" "${DATA_DIR}\config.yaml"

  ; Substitute activation code placeholder in config.yaml
  ${If} $ActivationCode != ""
    ; Use a simple sed-like replacement via NSIS string replacement
    FileOpen $0 "${DATA_DIR}\config.yaml" r
    FileOpen $1 "${DATA_DIR}\config.yaml.tmp" w
    loop:
      FileRead $0 $2
      IfErrors done
      ${StrRep} $3 $2 "ACTIVATION_CODE_PLACEHOLDER" $ActivationCode
      FileWrite $1 $3
      Goto loop
    done:
    FileClose $0
    FileClose $1
    Delete "${DATA_DIR}\config.yaml"
    Rename "${DATA_DIR}\config.yaml.tmp" "${DATA_DIR}\config.yaml"
  ${EndIf}
  Delete "${DATA_DIR}\config.template.yaml"

  ; Prevent system sleep during overnight operation
  nsExec::ExecToLog 'powercfg /change standby-timeout-ac 0'
  nsExec::ExecToLog 'powercfg /change hibernate-timeout-ac 0'
  nsExec::ExecToLog 'powercfg /change disk-timeout-ac 0'

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
