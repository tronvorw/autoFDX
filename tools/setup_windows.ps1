#Requires -Version 5.1
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 脚本位于 tools/ 目录，项目根目录是其父级
Set-Location -Path (Join-Path $PSScriptRoot "..")

# ── Python 版本配置 ────────────────────────────────────────────────────────────
$pyVersion    = "3.12"
$pyFull       = "3.12.10"
$installerUrl = "https://www.python.org/ftp/python/$pyFull/python-$pyFull-amd64.exe"

# ── 辅助输出函数 ───────────────────────────────────────────────────────────────
function Write-Step($msg)    { Write-Host "`n$msg" -ForegroundColor Cyan }
function Write-Info($msg)    { Write-Host "  $msg" -ForegroundColor Gray }
function Write-Ok($msg)      { Write-Host "  $msg" -ForegroundColor Green }
function Write-WarnMsg($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-ErrMsg($msg)  { Write-Host "  [ERROR] $msg" -ForegroundColor Red }

# ── 刷新当前会话 PATH（安装程序只写注册表，不更新已打开的终端）───────────────
function Refresh-EnvPath {
    $machine  = [System.Environment]::GetEnvironmentVariable("PATH", "Machine")
    $user     = [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $combined = ($machine + ";" + $user) -split ";" |
                Where-Object { $_ -ne "" } |
                Select-Object -Unique
    $env:PATH = $combined -join ";"
    Write-Info "PATH 已从注册表刷新"
}

# ── 查找 python.exe 完整路径 ──────────────────────────────────────────────────
# 返回完整路径而非命令字符串，方便后续使用 & 操作符直接调用，避免 Invoke-Expression 的解析问题
function Find-PythonExe {
    # py 启动器：让它帮我们定位正确版本的 python.exe
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $exe = (& py "-$pyVersion" -c "import sys; print(sys.executable)" 2>&1 |
                Where-Object { ($_ -is [string]) -and (Test-Path $_ -PathType Leaf) } |
                Select-Object -First 1)
        if ($exe) {
            Write-Info "通过 py 启动器找到 Python：$exe"
            return $exe
        }
    }

    # 直接 python 命令
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    # Windows 常见安装位置（per-user / all-users）
    foreach ($p in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "C:\Python312\python.exe",
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files (x86)\Python312\python.exe"
    )) {
        if (Test-Path $p) {
            Write-Info "在固定路径找到 Python：$p"
            return $p
        }
    }
    return $null
}

# ── 验证 Python 版本 ───────────────────────────────────────────────────────────
# 使用 & 操作符直接调用，避免 Invoke-Expression 解析 Python 代码时把 % 当 PS 运算符
function Assert-PythonVersion($pyExe) {
    $verStr = (& $pyExe -c "import sys; v=sys.version_info; print(str(v.major)+'.'+str(v.minor))").Trim()
    if ([version]$verStr -lt [version]$pyVersion) {
        throw "Python 版本 ($verStr) 低于要求的 $pyVersion，请升级后重试。"
    }
    Write-Ok "Python 版本校验通过：$verStr"
}

# ── 验证关键包能否正常导入 ────────────────────────────────────────────────────
function Test-Imports($pyExe) {
    $checks = @(
        @{ module = "cv2";       label = "opencv-python" },
        @{ module = "numpy";     label = "numpy" },
        @{ module = "PIL";       label = "Pillow" },
        @{ module = "pyscreeze"; label = "pyscreeze" },
        @{ module = "pyautogui"; label = "pyautogui" },
        @{ module = "keyboard";  label = "keyboard" }
    )
    $allOk = $true
    foreach ($c in $checks) {
        # 捕获 stderr，防止 $ErrorActionPreference=Stop 因非零退出码抛异常
        $out = (& $pyExe -c "import $($c.module)" 2>&1)
        if ($LASTEXITCODE -ne 0) {
            Write-WarnMsg "$($c.label) 导入失败：$out"
            $allOk = $false
        } else {
            Write-Ok "$($c.label) OK"
        }
    }
    if (-not $allOk) {
        throw "部分包导入验证失败，请查看上方警告。"
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════════
try {

    # ── 步骤 1：检查 / 安装 Python ─────────────────────────────────────────────
    Write-Step "[1/5] 检查 Python..."
    $pyExe = Find-PythonExe

    if (-not $pyExe) {
        Write-WarnMsg "未检测到 Python，开始自动安装 Python $pyFull..."

        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Info "使用 winget 安装..."
            winget install --id Python.Python.3.12 -e --silent `
                --accept-package-agreements --accept-source-agreements
            Refresh-EnvPath
        } else {
            $tmpInstaller = Join-Path $env:TEMP "python-$pyFull-amd64.exe"
            Write-Info "下载官方安装包到：$tmpInstaller"
            Invoke-WebRequest -Uri $installerUrl -OutFile $tmpInstaller -UseBasicParsing

            if (-not (Test-Path $tmpInstaller)) {
                throw "Python 安装包下载失败，请检查网络连接。"
            }

            Write-Info "静默安装 (PrependPath=1)..."
            Start-Process -FilePath $tmpInstaller `
                -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0" `
                -Wait
            Remove-Item $tmpInstaller -ErrorAction SilentlyContinue
            Refresh-EnvPath
        }

        $pyExe = Find-PythonExe
    }

    if (-not $pyExe) {
        throw "Python 安装后仍无法找到可执行文件。`n请关闭终端重新打开，或手动安装 Python $pyVersion 并勾选 'Add to PATH'。"
    }

    # ── 步骤 2：版本校验 ───────────────────────────────────────────────────────
    Write-Step "[2/5] Python 版本校验..."
    & $pyExe --version
    Assert-PythonVersion $pyExe

    # ── 步骤 3：升级 pip ───────────────────────────────────────────────────────
    Write-Step "[3/5] 升级 pip..."
    & $pyExe -m pip install --upgrade pip --no-warn-script-location

    # ── 步骤 4：安装依赖 ───────────────────────────────────────────────────────
    if (-not (Test-Path "tools/requirements.txt")) {
        throw "找不到 tools/requirements.txt，当前目录：$(Get-Location)"
    }

    Write-Step "[4/5] 安装依赖包 (tools/requirements.txt)..."
    Write-Info "pyautogui 全部子依赖已在 requirements.txt 中显式声明，确保完整安装。"

    # --prefer-binary  → 优先下载预编译 wheel，避免无 MSVC 时 C 扩展编译失败
    # --no-warn-script-location → 抑制 Scripts 目录不在 PATH 的警告（刷新后自然解决）
    & $pyExe -m pip install -r tools/requirements.txt --prefer-binary --no-warn-script-location
    Refresh-EnvPath

    # ── 步骤 5：导入验证 ───────────────────────────────────────────────────────
    Write-Step "[5/5] 验证关键包可正常导入..."
    Test-Imports $pyExe

    # ── 完成 ──────────────────────────────────────────────────────────────────
    Write-Host ""
    Write-Ok "======================================"
    Write-Ok "  [DONE] 部署完成，可直接启动程序："
    Write-Host "    python fallen_doll.py" -ForegroundColor Green
    Write-Ok "======================================"
    exit 0

} catch {
    Write-Host ""
    Write-ErrMsg "======================================"
    Write-ErrMsg "  [FAILED] 部署失败"
    Write-ErrMsg $_.Exception.Message
    Write-ErrMsg "======================================"
    exit 1
}
