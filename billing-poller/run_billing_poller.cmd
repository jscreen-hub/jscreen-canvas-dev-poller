@echo off
REM Wrapper for the Windows Scheduled Task "CanvasLabBillingPoller".
REM Runs one poll cycle and appends output to .state\poller.log.
cd /d "C:\projects\canvas-orders\billing-poller"
if not exist ".state" mkdir ".state"
echo ---- %DATE% %TIME% ---- >> ".state\poller.log"
"C:\Users\vendor_mchriscoe\AppData\Local\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe" run python -m billing_poller --once >> ".state\poller.log" 2>&1
