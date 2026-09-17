<#
.SYNOPSIS
    构建 videoRAG Windows 桌面包（onedir + 捆绑 ffmpeg + 冒烟测试 + 打 zip）。

.DESCRIPTION
    完整链路：

      1. 构建前端          web/  → web/dist（后端静态托管）
      2. 准备 Python 依赖  .venv（含 desktop extra：sherpa-onnx / pyinstaller）
      3. 获取 ffmpeg       packaging/vendor/ffmpeg（构建时下载，不进仓库）
      4. PyInstaller       packaging/videorag.spec → dist/videoRAG/
      5. 放置 ffmpeg       dist/videoRAG/vendor/ffmpeg（runtime_env 从这里注入 PATH）
      6. 冒烟测试          运行打包产物 --self-test，校验 /health 与 /mcp
      7. 打 zip            dist/videoRAG-<版本>-windows-x64.zip

    第 6 步是这条链路的价值所在：PyInstaller 收集原生包（lancedb / onnxruntime /
    rapidocr / sherpa-onnx）极易漏文件，而漏了往往要等用户跑到某条功能才暴露。
    把「能不能起来 + MCP 工具列表能不能拿到」变成构建门禁，比事后排查便宜得多。

.PARAMETER Venv
    Python 虚拟环境目录，默认 .venv。

.PARAMETER Version
    包版本号（用于 zip 命名），默认从 pyproject.toml 读取。

.PARAMETER SkipWeb
    跳过前端构建（web/dist 已存在时可用，例如 CI 已单独构建过）。

.PARAMETER SkipFfmpeg
    跳过 ffmpeg 获取（仅调试打包流程时可省时间；正式分发包不要跳过）。

.PARAMETER SkipSmoke
    跳过冒烟测试（不推荐；仅在排查打包问题时临时使用）。

.EXAMPLE
    pwsh -File packaging/build-windows.ps1
    pwsh -File packaging/build-windows.ps1 -SkipWeb -SkipFfmpeg
#>
[CmdletBinding()]
param(
    [string]$Venv = (Join-Path $PSScriptRoot '..\.venv'),
    [string]$Version,
    [string]$Python = 'python',
    [switch]$SkipWeb,
    [switch]$SkipFfmpeg,
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $Root
try {
    function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }

    # ---- 版本号 ----
    if (-not $Version) {
        $pyproject = Get-Content (Join-Path $Root 'pyproject.toml') -Raw
        if ($pyproject -match '(?m)^\s*version\s*=\s*"([^"]+)"') { $Version = $Matches[1] }
        else { $Version = '0.0.0' }
    }
    Write-Host "videoRAG Windows 构建 —— 版本 $Version"

    $DistApp = Join-Path $Root 'dist\videoRAG'
    $WindowExe = Join-Path $DistApp 'videorag.exe'   # 窗口形态（用户双击这个）
    # 自检一律用控制台版：窗口版 console=False，stdout 不可用，检查结果根本打不出来
    $ExePath = Join-Path $DistApp 'videorag-cli.exe'

    # ---- 1. 前端 ----
    Step 1 '构建前端（web/dist）'
    $webDist = Join-Path $Root 'web\dist'
    if ($SkipWeb) {
        Write-Host '  已跳过（-SkipWeb）'
    } else {
        if (-not (Test-Path (Join-Path $Root 'web\node_modules'))) {
            Write-Host '  npm ci …'
            npm --prefix (Join-Path $Root 'web') ci --registry=https://registry.npmmirror.com
        }
        npm --prefix (Join-Path $Root 'web') run build
    }
    if (-not (Test-Path (Join-Path $webDist 'index.html'))) {
        throw "前端产物缺失：$webDist\index.html"
    }

    # ---- 2. Python 依赖 ----
    Step 2 '准备 Python 依赖（含 desktop extra）'
    $venvPython = Join-Path $Venv 'Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        Write-Host "  创建虚拟环境 $Venv …"
        & $Python -m venv $Venv
    }

    # 已就绪就跳过安装：`pip install -e` 会先卸载自身，而在部分 Windows 环境下
    # （安全软件 / 文件过滤驱动）这个卸载动作可能异常，能不打就不打。
    $ready = $false
    try {
        $probe = & $venvPython -c "import PyInstaller, sherpa_onnx, fastembed, lancedb; print('ready')" 2>$null
        if ($LASTEXITCODE -eq 0 -and "$probe" -match 'ready') { $ready = $true }
    } catch { }

    if ($ready) {
        Write-Host '  依赖已就绪，跳过安装'
    } else {
        Write-Host '  安装依赖（首次需下载数百 MB）…'
        # 优先 uv（快）；uv 在部分 Windows 环境会因「更新 PE 资源被拒」失败，故失败即回落 pip
        $installed = $false
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            try {
                & uv pip install --link-mode=copy -p $venvPython -e '.[desktop]' `
                    -i https://pypi.tuna.tsinghua.edu.cn/simple
                if ($LASTEXITCODE -eq 0) { $installed = $true }
            } catch {
                Write-Warning "uv 安装失败，回落 pip：$_"
            }
        }
        if (-not $installed) {
            & $venvPython -m pip install -e '.[desktop]' `
                -i https://pypi.tuna.tsinghua.edu.cn/simple --retries 3
        }
    }
    & $venvPython -c "import PyInstaller, sherpa_onnx; print('  PyInstaller', PyInstaller.__version__, '/ sherpa-onnx ok')"

    # ---- 3. ffmpeg ----
    Step 3 '获取 ffmpeg / ffprobe'
    if ($SkipFfmpeg) {
        Write-Host '  已跳过（-SkipFfmpeg）'
    } else {
        & (Join-Path $PSScriptRoot 'vendor-ffmpeg.ps1')
    }

    # ---- 4. PyInstaller ----
    Step 4 'PyInstaller 打包（onedir）'
    if (Test-Path $DistApp) { Remove-Item $DistApp -Recurse -Force }
    & $venvPython -m PyInstaller (Join-Path $PSScriptRoot 'videorag.spec') `
        --noconfirm --distpath (Join-Path $Root 'dist') --workpath (Join-Path $Root 'build')
    if (-not (Test-Path $WindowExe)) { throw "打包产物缺失（窗口版）：$WindowExe" }
    if (-not (Test-Path $ExePath)) { throw "打包产物缺失（控制台版）：$ExePath" }

    # ---- 5. 放置 ffmpeg ----
    # 与 app/runtime_env.py 的 FFMPEG_SUBDIRS（vendor/ffmpeg）保持一致
    Step 5 '放置 ffmpeg 到产物目录'
    $vendorSrc = Join-Path $PSScriptRoot 'vendor\ffmpeg'
    if (Test-Path (Join-Path $vendorSrc 'ffmpeg.exe')) {
        $vendorDst = Join-Path $DistApp 'vendor\ffmpeg'
        New-Item -ItemType Directory -Path $vendorDst -Force | Out-Null
        Copy-Item (Join-Path $vendorSrc '*.exe') $vendorDst -Force
        Write-Host "  → $vendorDst"
    } elseif ($SkipFfmpeg) {
        Write-Host '  未找到捆绑 ffmpeg（-SkipFfmpeg），产物将依赖系统 PATH' -ForegroundColor Yellow
    } else {
        throw '  捆绑 ffmpeg 缺失——请先运行 packaging/vendor-ffmpeg.ps1'
    }

    # ---- 6. 冒烟测试 ----
    Step 6 '冒烟测试（打包产物自检）'
    if ($SkipSmoke) {
        Write-Host '  已跳过（-SkipSmoke）' -ForegroundColor Yellow
    } else {
        $smokeData = Join-Path ([IO.Path]::GetTempPath()) ("vr-smoke-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
        try {
            # 独立数据目录避免污染真实数据；--self-test 只起服务、不弹窗口，跑完自行退出
            & $ExePath --self-test --data-dir $smokeData --open-timeout 180
            if ($LASTEXITCODE -ne 0) { throw "冒烟测试失败（exit $LASTEXITCODE）" }
        } finally {
            if (Test-Path $smokeData) { Remove-Item $smokeData -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }

    # ---- 7. 打包 zip ----
    Step 7 '打包 zip'
    $zip = Join-Path $Root "dist\videoRAG-$Version-windows-x64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $DistApp '*') -DestinationPath $zip -CompressionLevel Optimal
    $sizeMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "  → $zip ($sizeMb MB)" -ForegroundColor Green

    Write-Host "`n构建完成。" -ForegroundColor Green
    Write-Host "  目录树 : $DistApp"
    Write-Host "  压缩包 : $zip"
    Write-Host "  说明   : 解压后双击 videorag.exe 即可（数据落在 %LOCALAPPDATA%\videoRAG）"
} finally {
    Pop-Location
}
