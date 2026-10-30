# ============================================================
# Campus Twin 演示环境停止
# 用法：.\tools\demo_stop.ps1
# ============================================================
$Root = Split-Path -Parent $PSScriptRoot
$ports = @(
    @{ Port = 8000; Name = '后端' },
    @{ Port = 8100; Name = 'AI Agent' },
    @{ Port = 5173; Name = '前端' },
    @{ Port = 9000; Name = 'Webhook 接收器' }
)
Write-Host "停止 Campus Twin 服务（PT 不动）..."
foreach ($p in $ports) {
    $conn = Get-NetTCPConnection -LocalPort $p.Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        foreach ($procId in ($conn.OwningProcess | Select-Object -Unique)) {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host ("  停止 {0}（:{1}  PID={2}）" -f $p.Name, $p.Port, $procId)
                Stop-Process -Id $procId -Force
            }
        }
    } else {
        Write-Host ("  {0}（:{1}）未运行" -f $p.Name, $p.Port) -ForegroundColor DarkGray
    }
}
Write-Host "完成。"
