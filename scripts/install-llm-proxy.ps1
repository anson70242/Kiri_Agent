# Start backend.llm_proxy at every logon, hidden, restarting if it dies.
# Run once in PowerShell (no admin needed):  .\scripts\install-llm-proxy.ps1
# Remove:  Unregister-ScheduledTask -TaskName "Kiri LLM Proxy" -Confirm:$false

$repo = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw - run 'uv sync' in $repo first." }

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m backend.llm_proxy --log data/llm_proxy.log" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Kiri LLM Proxy" -Action $action -Trigger $trigger -Settings $settings `
    -Description "HTTPS proxy so the HK server can reach LLM APIs (backend.llm_proxy)" -Force | Out-Null
Start-ScheduledTask -TaskName "Kiri LLM Proxy"
Start-Sleep -Seconds 5

Get-ScheduledTask -TaskName "Kiri LLM Proxy" | Select-Object TaskName, State
Get-Content (Join-Path $repo "data\llm_proxy.log") -Tail 3 -ErrorAction SilentlyContinue
