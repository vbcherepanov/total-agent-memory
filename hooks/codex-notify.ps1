#Requires -Version 5.1
<#
.SYNOPSIS
    Codex CLI notify hook — reminder to save knowledge after agent turns

.DESCRIPTION
    This hook is OPTIONAL. Codex hooks are experimental.
    Memory works without hooks — AGENTS.md instructions are sufficient.

    Add to ~/.codex/config.toml:
      notify = ["powershell", "-ExecutionPolicy", "Bypass", "-File", "C:/path/to/hooks/codex-notify.ps1"]
#>

# Read JSON payload from stdin
$Payload = $null
try {
    $Payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
} catch {}

# Detect project
$Cwd = if ($Payload -and $Payload.cwd) { $Payload.cwd } else { Get-Location }
$Project = Split-Path -Leaf $Cwd

# Windows toast notification
try {
    Add-Type -AssemblyName System.Windows.Forms
    $notify = New-Object System.Windows.Forms.NotifyIcon
    try {
        $notify.Icon = [System.Drawing.SystemIcons]::Information
        $notify.Visible = $true
        $notify.ShowBalloonTip(3000, "Total Memory — $Project", "Remember: memory_save & self_reflect", [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Seconds 3
    } finally {
        $notify.Dispose()
    }
} catch {
    # Silently ignore if notification fails
}
