# Pair a headset with Cuff for PC: run this with the headset on, then type the 4-digit code
# the wrist panel shows into the "Pair with Cuff" window. Pairing stays open for 5 minutes.
#   powershell -ExecutionPolicy Bypass -File pc-server\pair.ps1          (-Console: ask here instead)
# Paired headsets are listed in %LOCALAPPDATA%\cuff-pc\paired.json; delete an entry to unpair.
param([int]$Port = 8765, [switch]$Console)
$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:$Port"
try {
    Invoke-RestMethod -Method Post "$base/pair/open" | Out-Null
} catch {
    Write-Host "Cuff for PC isn't running. Start it with: Start-ScheduledTask -TaskName 'Cuff for PC'"
    exit 1
}
Write-Host "Pairing is open for 5 minutes. Look at your wrist: Cuff shows a 4-digit code."

# A small window, kept on top, so the code has somewhere obvious to go. Returns "" on Cancel.
function Ask-Code([string]$note) {
    if ($Console) { return Read-Host "Code (or press Enter to stop)" }
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $form = New-Object Windows.Forms.Form
    $form.Text = "Pair with Cuff"
    $form.ClientSize = New-Object Drawing.Size(320, 150)
    $form.FormBorderStyle = "FixedDialog"
    $form.StartPosition = "CenterScreen"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.TopMost = $true
    $label = New-Object Windows.Forms.Label
    $label.Text = "Type the 4-digit code your wrist shows.`n$note"
    $label.SetBounds(12, 10, 296, 44)
    $box = New-Object Windows.Forms.TextBox
    $box.SetBounds(12, 60, 296, 30)
    $box.MaxLength = 4
    $box.Font = New-Object Drawing.Font("Segoe UI", 14)
    $ok = New-Object Windows.Forms.Button
    $ok.Text = "Pair"
    $ok.SetBounds(140, 108, 80, 28)
    $ok.DialogResult = "OK"
    $cancel = New-Object Windows.Forms.Button
    $cancel.Text = "Cancel"
    $cancel.SetBounds(228, 108, 80, 28)
    $cancel.DialogResult = "Cancel"
    $form.Controls.AddRange(@($label, $box, $ok, $cancel))
    $form.AcceptButton = $ok
    $form.CancelButton = $cancel
    $form.Add_Shown({ $form.Activate(); $box.Focus() })
    if ($form.ShowDialog() -eq "OK") { return $box.Text }
    return ""
}

$note = "Pairing is open for 5 minutes."
while ($true) {
    $code = Ask-Code $note
    if (-not $code) { exit 0 }
    try {
        $r = Invoke-RestMethod -Method Post "$base/pair/confirm" -ContentType "application/json" `
            -Body (@{ code = $code.Trim() } | ConvertTo-Json)
        Write-Host "Paired with $($r.name)."
        if (-not $Console) {
            [Windows.Forms.MessageBox]::Show("Paired with $($r.name).", "Pair with Cuff") | Out-Null
        }
        exit 0
    } catch {
        $msg = $_.ErrorDetails.Message
        if ($msg) { $msg = ($msg | ConvertFrom-Json).error } else { $msg = $_.Exception.Message }
        Write-Host "Not paired: $msg"
        if ($msg -match "isn't open") {
            if (-not $Console) {
                [Windows.Forms.MessageBox]::Show("Pairing closed: $msg`nRun pair.ps1 again.", "Pair with Cuff") | Out-Null
            }
            exit 1
        }
        $note = "Not paired: $msg"
    }
}
