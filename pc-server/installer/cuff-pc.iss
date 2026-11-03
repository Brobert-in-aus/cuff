; Cuff for PC's Windows installer (Inno Setup 6). Build it with build.ps1, not by hand: that
; stages the public copy of Cuff with uv.exe and the icon, then runs ISCC with these defines.
;   Version  the version (git describe), shown in Apps and used in the file name
;   Stage    the staged folder
;
; Per user, into %LOCALAPPDATA%\Programs\Cuff for PC: no admin, except one prompt for the
; firewall rules (firewall.ps1) when they aren't there yet. It registers the logon task
; (install-autostart.ps1) and a Start menu shortcut, then starts the app; its first run
; downloads the Python packages (bootstrap.py). The app updates itself by downloading a newer
; build of this installer and running it with /VERYSILENT (updates.py), started outside the
; logon task's job (or by the one-shot "Cuff for PC update" task).

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Stage
  #define Stage "stage"
#endif

[Setup]
AppId={{6B0B6E5C-3C1E-4C59-9D7E-6E0C2F1C7A11}
AppName=Cuff for PC
AppVersion={#Version}
AppPublisher=Cuff
AppPublisherURL=https://github.com/Brobert-in-aus/cuff
DefaultDirName={localappdata}\Programs\Cuff for PC
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=CuffForPC-Setup-{#Version}
SetupIconFile={#Stage}\cuff.ico
UninstallDisplayIcon={app}\cuff.ico
UninstallDisplayName=Cuff for PC
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
LicenseFile={#Stage}\LICENSE

[InstallDelete]
; An update replaces the code, so files the new version dropped don't linger. The downloaded
; packages (pc-server\.venv) stay, so an update doesn't download 2.5 GB again.
Type: filesandordirs; Name: "{app}\cuff"
Type: filesandordirs; Name: "{app}\charms"
Type: filesandordirs; Name: "{app}\recorder"
Type: filesandordirs; Name: "{app}\docs"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\pc-server\installer"
Type: files; Name: "{app}\pc-server\*.py"
Type: files; Name: "{app}\pc-server\*.html"
Type: files; Name: "{app}\pc-server\*.ps1"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs

[Icons]
Name: "{userprograms}\Cuff for PC"; Filename: "{sys}\conhost.exe"; \
  Parameters: "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""{app}\pc-server\run-server.ps1"""; \
  WorkingDir: "{app}\pc-server"; IconFilename: "{app}\cuff.ico"; Comment: "Open Cuff for PC"

[Run]
; The firewall rules need admin: Windows asks once, only when they're missing.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\pc-server\firewall.ps1"""; \
  Flags: shellexec runhidden waituntilterminated; Verb: runas; Check: NeedsFirewall; \
  StatusMsg: "Adding the firewall rules (Windows asks for permission)..."
; The logon task, then the app started through it, exactly as at logon: launched straight from
; Setup, the hidden console it runs in didn't survive Setup closing. -Start copes with an
; instance of the task that's still running (it would make Task Scheduler ignore the start).
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\pc-server\install-autostart.ps1"" -NoShortcut -Start"; \
  Flags: runhidden waituntilterminated; StatusMsg: "Starting Cuff for PC..."

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -Command ""Unregister-ScheduledTask -TaskName 'Cuff for PC' -Confirm:$false"""; \
  Flags: runhidden; RunOnceId: "task"
; The one-shot task an update may have started its installer with (updates.py).
Filename: "powershell.exe"; Parameters: "-NoProfile -Command ""Unregister-ScheduledTask -TaskName 'Cuff for PC update' -Confirm:$false -ErrorAction SilentlyContinue"""; \
  Flags: runhidden; RunOnceId: "updatetask"
; The firewall rules go in [Code] (CurUninstallStepChanged): only when someone can answer the prompt.

[UninstallDelete]
; The packages the first run downloaded, and Python's caches. Also what run-server.ps1 has uv
; and Hugging Face keep in {app}\cache: uv's cache, the Python uv fetched, and the speech model.
; (Downloads, not the person's data, so they go even on a silent uninstall; what a silent
; uninstall keeps is %LOCALAPPDATA%\cuff-pc.)
Type: filesandordirs; Name: "{app}\pc-server\.venv"
Type: filesandordirs; Name: "{app}\pc-server\__pycache__"
Type: filesandordirs; Name: "{app}\cache"

[Code]
function NeedsFirewall(): Boolean;
var
  Code: Integer;
begin
  { Exit code 0 when "Cuff for PC" rules exist already. }
  Result := not (Exec('powershell.exe', '-NoProfile -Command "if (Get-NetFirewallRule -DisplayName ''Cuff for PC*'' -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"',
                      '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0));
end;

function SetEnvironmentVariable(lpName, lpValue: String): BOOL;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';
function GetCurrentProcess(): THandle;
  external 'GetCurrentProcess@kernel32.dll stdcall';
function IsProcessInJob(ProcessHandle, JobHandle: THandle; var InJob: BOOL): BOOL;
  external 'IsProcessInJob@kernel32.dll stdcall';

function InAJob(): Boolean;
var
  InJob: BOOL;
begin
  InJob := False;
  Result := IsProcessInJob(GetCurrentProcess(), 0, InJob) and InJob;
end;

procedure StopApp();
var
  Code: Integer;
begin
  { The running app, so its files can be replaced: asked to quit (whichever copy holds the port,
    this one or a git checkout's), then the scheduled task and anything left running from here.
    The folder reaches PowerShell in an environment variable (CUFF_APP), so no quote or wildcard
    character in a path can break the command. This PowerShell and Setup (its parent) are left
    alone, and so is the uninstaller.
    Stop-ScheduledTask ends every process in the task's job object. When Setup itself is in a
    job, that may be the one (an older Cuff for PC started it from inside its task), so it's
    skipped: the rest stops the app. }
  SetEnvironmentVariable('CUFF_APP', ExpandConstant('{app}'));
  if InAJob() then
    SetEnvironmentVariable('CUFF_IN_JOB', '1')
  else
    SetEnvironmentVariable('CUFF_IN_JOB', '0');
  Exec('powershell.exe', '-NoProfile -Command "try { Invoke-RestMethod -Method Post http://127.0.0.1:8765/app/quit ' +
       '-Headers @{''X-Cuff-App''=''1''} -TimeoutSec 3 | Out-Null; Start-Sleep 2 } catch {}; ' +
       'if ($env:CUFF_IN_JOB -ne ''1'') { Stop-ScheduledTask -TaskName ''Cuff for PC'' -ErrorAction SilentlyContinue }; ' +
       '$d = $env:CUFF_APP.TrimEnd([char]92) + [char]92; ' +
       '$parent = (Get-CimInstance Win32_Process -Filter (''ProcessId='' + $PID)).ParentProcessId; ' +
       'Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.ProcessId -ne $parent -and (' +
       '($_.ExecutablePath -and $_.ExecutablePath.StartsWith($d, [StringComparison]::OrdinalIgnoreCase) -and ' +
       '-not [IO.Path]::GetFileName($_.ExecutablePath).StartsWith(''unins'', [StringComparison]::OrdinalIgnoreCase)) -or ' +
       '($_.CommandLine -and $_.CommandLine.IndexOf($d + ''pc-server'', [StringComparison]::OrdinalIgnoreCase) -ge 0)) } | ' +
       'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
       '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopApp();
    { The firewall rules need admin, so only when someone's there to answer Windows' prompt: a
      silent uninstall leaves them (they open one port, to the home network and the Frame). }
    if not UninstallSilent then
      ShellExec('runas', 'powershell.exe',
                '-NoProfile -WindowStyle Hidden -Command "Remove-NetFirewallRule -DisplayName ''Cuff for PC*''"',
                '', SW_HIDE, ewWaitUntilTerminated, Code);
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{localappdata}\cuff-pc');
    { Asked, never assumed: a silent uninstall keeps them. }
    if DirExists(Data) and (not UninstallSilent) and (MsgBox('Also remove the paired headsets, this app''s key to the headset, ' +
       'its settings and the downloaded-video thumbnails (' + Data + ')? Your downloaded videos stay.',
       mbConfirmation, MB_YESNO) = IDYES) then
      DelTree(Data, True, True, True);
  end;
end;
