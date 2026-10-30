# ============================================================
# Campus Twin 演示环境一键启动
# 用法（在 PowerShell 里）：
#   .\tools\demo_start.ps1                 # 启动未运行的服务（已运行的跳过）
#   .\tools\demo_start.ps1 -Restart        # 重启全部服务
#   .\tools\demo_start.ps1 -OpenBrowser    # 启动后自动打开大屏
#   .\tools\demo_start.ps1 -WithMcpInspector  # 额外启动 MCP Inspector（可视化演示 MCP Server）
# ============================================================
param(
    [switch]$Restart,
    [switch]$OpenBrowser,
    [switch]$WithMcpInspector
)
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$McpPy = Join-Path $Root '.venv-mcp314\Scripts\python.exe'
$Logs = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Test-Port([int]$Port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $ar = $c.BeginConnect('127.0.0.1', $Port, $null, $null)
        $ok = $ar.AsyncWaitHandle.WaitOne(400)
        if ($ok) { $c.EndConnect($ar) }
        $c.Close()
        return $ok
    } catch { return $false }
}

function Wait-Port([int]$Port, [string]$Name, [int]$TimeoutSec = 40) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $Port) { Write-Host ("  [OK]   {0} :{1} 已就绪" -f $Name, $Port) -ForegroundColor Green; return $true }
        Start-Sleep -Milliseconds 800
    }
    Write-Host ("  [FAIL] {0} :{1} 超时未就绪（看 logs/ 下日志）" -f $Name, $Port) -ForegroundColor Red
    return $false
}

function Stop-PortProcess([int]$Port, [string]$Name) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $procIds = $conn.OwningProcess | Select-Object -Unique
        foreach ($procId in $procIds) {
            $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($p) { Write-Host ("  停止 {0}（:{1}  PID={2}）" -f $Name, $Port, $procId); Stop-Process -Id $procId -Force }
        }
        Start-Sleep -Seconds 1
    }
}

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " Campus Twin 演示环境启动" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

if ($Restart) {
    Write-Host "[0/6] 重启模式：先停止已有服务..."
    Stop-PortProcess 8000 '后端'
    Stop-PortProcess 8100 'AI Agent'
    Stop-PortProcess 5173 '前端'
    Stop-PortProcess 8200 'MCP Bridge'
    Stop-PortProcess 9000 'Webhook 接收器'
    Start-Sleep -Seconds 2
}

# ---------- 1. Webhook 接收器 ----------
Write-Host "[1/6] Webhook 接收器（:9000）..."
if (Test-Port 9000) {
    Write-Host "  已在运行，跳过" -ForegroundColor DarkGray
} else {
    Start-Process -FilePath $Py -ArgumentList (Join-Path $Root 'tools\webhook_receiver.py') `
        -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Logs 'webhook-receiver.out.log') `
        -RedirectStandardError (Join-Path $Logs 'webhook-receiver.err.log') | Out-Null
    Wait-Port 9000 'Webhook 接收器' 15 | Out-Null
}

# ---------- 2. 后端 ----------
Write-Host "[2/6] 后端（:8000，netcon 驱动 + Webhook 通知）..."
if (Test-Port 8000) {
    Write-Host "  已在运行，跳过" -ForegroundColor DarkGray
} else {
    $env:NOTIFY_ENABLED = 'true'
    $env:NOTIFY_WEBHOOK_URL = 'http://127.0.0.1:9000/webhook'
    $env:NOTIFY_WEBHOOK_TYPE = 'generic'
    $env:NOTIFY_MIN_SEVERITY = 'critical'
    Start-Process -FilePath $Py -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--log-level', 'info' `
        -WorkingDirectory (Join-Path $Root 'backend') -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Logs 'backend.out.log') `
        -RedirectStandardError (Join-Path $Logs 'backend.err.log') | Out-Null
    Remove-Item Env:\NOTIFY_ENABLED, Env:\NOTIFY_WEBHOOK_URL, Env:\NOTIFY_WEBHOOK_TYPE, Env:\NOTIFY_MIN_SEVERITY -ErrorAction SilentlyContinue
    Wait-Port 8000 '后端' 40 | Out-Null
}

# ---------- 3. AI Agent ----------
Write-Host "[3/6] AI Agent（:8100，LangGraph 引擎 + MCP 工具）..."
if (Test-Port 8100) {
    Write-Host "  已在运行，跳过" -ForegroundColor DarkGray
} else {
    Start-Process -FilePath $Py -ArgumentList '-m', 'uvicorn', 'app.api:app', '--host', '127.0.0.1', '--port', '8100', '--log-level', 'info' `
        -WorkingDirectory (Join-Path $Root 'agent') -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Logs 'agent.out.log') `
        -RedirectStandardError (Join-Path $Logs 'agent.err.log') | Out-Null
    Wait-Port 8100 'AI Agent' 40 | Out-Null
}

# ---------- 4. 前端 ----------
Write-Host "[4/6] 前端大屏（:5173）..."
if (Test-Port 5173) {
    Write-Host "  已在运行，跳过" -ForegroundColor DarkGray
} else {
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "npm run dev -- --port 5173 --host 127.0.0.1 > `"$Logs\frontend.log`" 2>&1" `
        -WorkingDirectory (Join-Path $Root 'frontend') -WindowStyle Hidden | Out-Null
    Wait-Port 5173 '前端大屏' 40 | Out-Null
}

# ---------- 5. MCP Bridge ----------
Write-Host "[5/6] MCP Bridge（:8200，Python 3.14 独立进程，供 Agent 调用外部 MCP 工具）..."
if (Test-Port 8200) {
    Write-Host "  已在运行，跳过" -ForegroundColor DarkGray
} elseif (-not (Test-Path $McpPy)) {
    Write-Host "  [WARN] 未找到 .venv-mcp314，跳过 MCP Bridge（Agent 的 MCP 工具将不可用）" -ForegroundColor Yellow
} else {
    $env:NPM_CONFIG_REGISTRY = 'https://registry.npmmirror.com'
    Start-Process -FilePath $McpPy -ArgumentList 'bridge.py', '--port', '8200' `
        -WorkingDirectory (Join-Path $Root 'mcp_bridge') -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Logs 'mcp_bridge.out.log') `
        -RedirectStandardError (Join-Path $Logs 'mcp_bridge.err.log') | Out-Null
    Remove-Item Env:\NPM_CONFIG_REGISTRY -ErrorAction SilentlyContinue
    Wait-Port 8200 'MCP Bridge' 45 | Out-Null
}

# ---------- 6. 检查 PT ----------
Write-Host "[6/6] Packet Tracer Network Controller（:58000）..."
if (Test-Port 58000) {
    Write-Host "  [OK]   PT NC 已监听（数据源在线）" -ForegroundColor Green
} else {
    Write-Host "  [WARN] PT NC 未监听！请打开 Packet Tracer → campus-final.pkt →" -ForegroundColor Yellow
    Write-Host "         确认 Network Controller 的 Real World Access 已启用（端口 58000）" -ForegroundColor Yellow
}

# ---------- 可选：MCP Inspector ----------
if ($WithMcpInspector) {
    Write-Host ""
    Write-Host "[可选] 启动 MCP Inspector（可视化展示 MCP Server 工具/资源/提示词）..."
    if (Test-Port 6274) {
        Write-Host "  已在运行，跳过" -ForegroundColor DarkGray
    } elseif ((Test-Path $McpPy) -and (Test-Path (Join-Path $Root 'mcp_server\server.py'))) {
        $env:NPM_CONFIG_REGISTRY = 'https://registry.npmmirror.com'
        $env:BACKEND_BASE = 'http://127.0.0.1:8000'
        $cmd = "npx -y @modelcontextprotocol/inspector -- `"$McpPy`" `"$(Join-Path $Root 'mcp_server\server.py')`" > `"$Logs\inspector.log`" 2>&1"
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $cmd -WindowStyle Hidden | Out-Null
        Remove-Item Env:\BACKEND_BASE -ErrorAction SilentlyContinue
        Remove-Item Env:\NPM_CONFIG_REGISTRY -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 30
        $inspLog = Get-Content (Join-Path $Logs 'inspector.log') -Raw -ErrorAction SilentlyContinue
        if ($inspLog -match 'http://127\.0\.0\.1:6274\?MCP_INSPECTOR_API_TOKEN=([0-9a-f]+)') {
            Write-Host ("  Inspector 地址：http://127.0.0.1:6274?MCP_INSPECTOR_API_TOKEN=" + $matches[1]) -ForegroundColor Green
        } else {
            Write-Host "  Inspector 启动中，稍后看 logs/inspector.log 里的地址" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  [WARN] 缺少 .venv-mcp314 或 mcp_server/server.py，跳过 Inspector" -ForegroundColor Yellow
    }
}

# ---------- 汇总 ----------
Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " 演示环境状态" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
$checks = @(
    @{ Name = 'PT Network Controller'; Port = 58000; Url = '（Packet Tracer 内）' },
    @{ Name = '后端 API'; Port = 8000; Url = 'http://127.0.0.1:8000/docs' },
    @{ Name = 'AI Agent'; Port = 8100; Url = 'http://127.0.0.1:8100/docs' },
    @{ Name = '3D 大屏'; Port = 5173; Url = 'http://127.0.0.1:5173/' },
    @{ Name = 'MCP Bridge'; Port = 8200; Url = 'http://127.0.0.1:8200/health' },
    @{ Name = 'Webhook 接收器'; Port = 9000; Url = 'http://127.0.0.1:9000/webhook（日志 logs/webhook.log）' }
)
foreach ($c in $checks) {
    $ok = Test-Port $c.Port
    $mark = if ($ok) { '[OK]  ' } else { '[DOWN]' }
    $color = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("  {0} {1,-22} {2}" -f $mark, $c.Name, $c.Url) -ForegroundColor $color
}
Write-Host ""
if (Test-Port 8000) {
    try {
        $h = (Invoke-WebRequest 'http://127.0.0.1:8000/api/health' -TimeoutSec 8 -UseBasicParsing).Content | ConvertFrom-Json
        Write-Host ("  后端: driver={0} 设备={1} 链路={2} 主机={3}" -f $h.driver, $h.counts.devices, $h.counts.links, $h.counts.hosts) -ForegroundColor DarkGray
    } catch { }
}
if (Test-Port 8100) {
    try {
        $a = (Invoke-WebRequest 'http://127.0.0.1:8100/api/health' -TimeoutSec 8 -UseBasicParsing).Content | ConvertFrom-Json
        Write-Host ("  Agent: engine={0} provider={1}/{2} | MCP tools={3}" -f $a.engine, $a.provider, $a.model, $a.mcp_bridge.total_tools) -ForegroundColor DarkGray
    } catch { }
}
if (Test-Port 8200) {
    try {
        $m = (Invoke-WebRequest 'http://127.0.0.1:8200/health' -TimeoutSec 8 -UseBasicParsing).Content | ConvertFrom-Json
        Write-Host ("  MCP Bridge: servers={0} total_tools={1}" -f (($m.servers | ForEach-Object { $_.name }) -join '/'), $m.total_tools) -ForegroundColor DarkGray
    } catch { }
}
Write-Host ""
Write-Host "  录屏前检查：浏览器打开 http://127.0.0.1:5173/ ，确认右上角" -ForegroundColor Gray
Write-Host "  『后端连接 已连接 / 数据来源 PT NC 真实采集』" -ForegroundColor Gray
Write-Host ""
if ($OpenBrowser) { Start-Process 'http://127.0.0.1:5173/' }
