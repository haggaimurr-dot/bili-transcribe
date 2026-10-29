# 一键连接 AutoDL 远程 GPU：免密登录（首次配置）→ 启动 worker → 建 SSH 隧道 → 等 worker 就绪 → 打开本地工具。
# 由 connect-gpu.bat 调用；窗口保持打开就是隧道在工作，关掉窗口隧道就断了。
# 不用 Stop：Windows PowerShell 5.1 会把 ssh 写到 stderr 的提示当成终止错误；各步骤自己检查 $LASTEXITCODE
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$ConfigPath = Join-Path $Root "data\remote_gpu.json"
$KeyPath = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
$RemoteDir = "/root/autodl-tmp/bili-transcribe"
$LocalPort = 9000
$SshOpts = @("-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10")

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

# ---------- 1. 连接信息（AutoDL 每次开机端口可能会变） ----------
function Parse-Login($text) {
    # 支持粘贴 AutoDL 控制台的整条登录指令：ssh -p 28017 root@connect.westd.seetacloud.com
    if ($text -match "-p\s+(\d+)\s+(\S+)@(\S+)") { return @{ port = [int]$Matches[1]; user = $Matches[2]; host = $Matches[3] } }
    if ($text -match "(\S+)@(\S+)\s+-p\s+(\d+)") { return @{ port = [int]$Matches[3]; user = $Matches[1]; host = $Matches[2] } }
    return $null
}

$cfg = $null
if (Test-Path $ConfigPath) {
    try { $j = Get-Content $ConfigPath -Raw | ConvertFrom-Json; $cfg = @{ host = $j.host; port = [int]$j.port; user = $j.user } } catch {}
}
if ($cfg) {
    Say "上次的连接：ssh -p $($cfg.port) $($cfg.user)@$($cfg.host)"
    $in = Read-Host "直接回车沿用；端口变了就粘贴控制台新的「登录指令」"
} else {
    Say "第一次使用：请到 AutoDL 控制台 → 容器实例 → SSH 登录，复制「登录指令」粘贴到这里" "Yellow"
    $in = Read-Host "登录指令"
}
if ($in.Trim()) {
    $new = Parse-Login $in
    if (-not $new) { Say "没认出登录指令，格式应类似：ssh -p 28017 root@connect.xxx.seetacloud.com" "Red"; exit 1 }
    $cfg = $new
}
if (-not $cfg) { Say "没有连接信息" "Red"; exit 1 }
New-Item -ItemType Directory -Force (Split-Path $ConfigPath) | Out-Null
($cfg | ConvertTo-Json) | Set-Content -Encoding UTF8 $ConfigPath
$Target = "$($cfg.user)@$($cfg.host)"
$PortArgs = @("-p", "$($cfg.port)")

# ---------- 2. 免密登录（只在第一次、或者实例重置后需要输一次密码） ----------
function Test-KeyLogin {
    & ssh @SshOpts -o BatchMode=yes @PortArgs $Target "true" 2>$null
    return ($LASTEXITCODE -eq 0)
}
Say "`n[1/4] 检查免密登录…" "Cyan"
if (-not (Test-KeyLogin)) {
    if (-not (Test-Path $KeyPath)) {
        Say "生成本机 SSH 密钥 $KeyPath"
        New-Item -ItemType Directory -Force (Split-Path $KeyPath) | Out-Null
        & ssh-keygen -t ed25519 -f $KeyPath -q -N '""' -C "bili-transcribe"
    }
    Say "把公钥装到 AutoDL 上，接下来会提示输入一次 SSH 密码（控制台「SSH 登录」那里的密码，输入时不显示）" "Yellow"
    $pub = (Get-Content "$KeyPath.pub" -Raw).Trim()
    & ssh @SshOpts @PortArgs $Target "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qxF '$pub' ~/.ssh/authorized_keys 2>/dev/null || echo '$pub' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
    if (-not (Test-KeyLogin)) { Say "免密登录配置失败：检查实例是否已开机、登录指令和密码是否正确" "Red"; exit 1 }
    Say "免密登录已配置，以后不用再输密码" "Green"
} else {
    Say "免密登录正常" "Green"
}

# ---------- 3. 启动 worker（已在运行时 run.sh 会直接跳过） ----------
Say "`n[2/4] 启动 AutoDL 上的 worker…" "Cyan"
& ssh @SshOpts -o BatchMode=yes @PortArgs $Target "cd $RemoteDir && bash worker/run.sh"
if ($LASTEXITCODE -ne 0) { Say "worker 启动命令失败（项目目录 $RemoteDir 是否存在？）" "Red"; exit 1 }

# ---------- 4. 建隧道 ----------
Say "`n[3/4] 建 SSH 隧道 127.0.0.1:$LocalPort → AutoDL:9000…" "Cyan"
# 关掉以前留下的隧道，避免端口被占
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
    Where-Object { $_.CommandLine -match "$LocalPort`:127\.0\.0\.1:9000" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$TunnelArgs = $SshOpts + @("-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes", "-N", "-L", "$LocalPort`:127.0.0.1:9000") + $PortArgs + @($Target)
function Start-Tunnel { Start-Process ssh -ArgumentList $TunnelArgs -NoNewWindow -PassThru }
$tunnel = Start-Tunnel

# ---------- 5. 等 worker 就绪（预加载 3 份 Nano 约 2～3 分钟） ----------
function Get-WorkerState {
    # 没带 Token 访问 /health：返回 401 就说明 worker 已在监听
    try { Invoke-WebRequest "http://127.0.0.1:$LocalPort/health" -UseBasicParsing -TimeoutSec 5 | Out-Null; return "up" }
    catch {
        $r = $_.Exception.Response
        if ($r -and [int]$r.StatusCode -eq 401) { return "up" }
        return "down"
    }
}
Say "`n[4/4] 等 worker 加载模型（首次开机约 2～3 分钟）…" "Cyan"
$t0 = Get-Date
while ((Get-WorkerState) -ne "up") {
    if ($tunnel.HasExited) { Say "隧道断开，重新连接…" "Yellow"; Start-Sleep 3; $tunnel = Start-Tunnel }
    $sec = [int]((Get-Date) - $t0).TotalSeconds
    if ($sec -gt 600) { Say "等了 10 分钟 worker 还没就绪，请在 AutoDL 上看 tail -30 /root/autodl-tmp/worker.log" "Red"; break }
    Write-Host "`r  已等待 $sec 秒…" -NoNewline
    Start-Sleep 5
}
Write-Host ""
if ((Get-WorkerState) -eq "up") { Say "✅ 远程 GPU 已就绪" "Green" }

# ---------- 打开本地工具 ----------
$appUp = $false
try { Invoke-WebRequest "http://127.0.0.1:8765/api/status" -UseBasicParsing -TimeoutSec 3 | Out-Null; $appUp = $true } catch {}
if (-not $appUp) {
    Say "本地工具没在运行，正在打开 start.bat（新窗口）"
    Start-Process -FilePath (Join-Path $Root "start.bat") -WorkingDirectory $Root
} else {
    Start-Process "http://127.0.0.1:8765/"
}

Say "`n这个窗口就是隧道，用远程 GPU 时请保持打开；用完关掉窗口，再去 AutoDL 控制台关机。" "Yellow"
# ---------- 守护隧道：断了自动重连 ----------
while ($true) {
    $tunnel.WaitForExit()
    Say "$(Get-Date -Format HH:mm:ss) 隧道断开，5 秒后重连…" "Yellow"
    Start-Sleep 5
    $tunnel = Start-Tunnel
}
