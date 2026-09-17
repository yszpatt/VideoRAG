<#
.SYNOPSIS
    下载 ffmpeg / ffprobe 的 Windows 静态构建到 packaging/vendor/ffmpeg。

.DESCRIPTION
    桌面包需要 ffmpeg：yt-dlp 合并音视频、抽音频、视觉旁路抽帧都依赖它。
    Docker 镜像由 apt 安装，裸机由用户自备；桌面包则随包捆绑，否则开箱即用无从谈起。

    **刻意不把二进制提交进仓库**：Windows 静态构建通常是 GPL 授权（含 libx264 等），
    体积也有上百 MB。改为构建时下载 → 随安装包分发 → README 注明来源与许可，
    仓库本身保持干净。

    产物布局（与 app/runtime_env.py 的 FFMPEG_SUBDIRS 约定一致）：
        packaging/vendor/ffmpeg/ffmpeg.exe
        packaging/vendor/ffmpeg/ffprobe.exe

.PARAMETER Url
    下载地址，默认 gyan.dev 的 release-essentials 构建（含 ffmpeg/ffprobe，体积适中）。

.PARAMETER Force
    目标已存在时也重新下载。

.PARAMETER OutDir
    输出目录，默认 <本脚本目录>/vendor/ffmpeg。

.EXAMPLE
    pwsh -File packaging/vendor-ffmpeg.ps1
    pwsh -File packaging/vendor-ffmpeg.ps1 -Force
#>
[CmdletBinding()]
param(
    [string]$Url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
    [string[]]$FallbackUrls = @(
        'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
    ),
    [switch]$Force,
    [string]$OutDir = (Join-Path $PSScriptRoot 'vendor\ffmpeg')
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 默认可能仍是 TLS 1.0，访问 GitHub/gyan.dev 会直接失败
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    Write-Verbose "无法设置 TLS 1.2（忽略）：$_"
}

function Get-RemoteFile {
    param([string]$Uri, [string]$Destination)
    Write-Host "  下载 $Uri"
    # curl.exe 在 Windows 10 1803+ 自带，比 Invoke-WebRequest 快且能显示进度
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source -L --fail --retry 3 --retry-delay 2 -o $Destination $Uri
        if ($LASTEXITCODE -ne 0) { throw "curl 下载失败（exit $LASTEXITCODE）：$Uri" }
    } else {
        Invoke-WebRequest -Uri $Uri -OutFile $Destination -UseBasicParsing
    }
}

$ffmpegExe = Join-Path $OutDir 'ffmpeg.exe'
$ffprobeExe = Join-Path $OutDir 'ffprobe.exe'

if ((Test-Path $ffmpegExe) -and (Test-Path $ffprobeExe) -and -not $Force) {
    Write-Host "ffmpeg 已就绪，跳过下载：$OutDir" -ForegroundColor Green
    & $ffmpegExe -version | Select-Object -First 1
    exit 0
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("vr-ffmpeg-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$zip = Join-Path $work 'ffmpeg.zip'

try {
    $urls = @($Url) + $FallbackUrls
    $downloaded = $false
    $lastError = $null
    foreach ($u in $urls) {
        try {
            Get-RemoteFile -Uri $u -Destination $zip
            $downloaded = $true
            break
        } catch {
            $lastError = $_
            Write-Warning "下载失败，尝试下一个源：$_"
        }
    }
    if (-not $downloaded) { throw "所有下载源均失败：$lastError" }

    Write-Host '  解压…'
    $extract = Join-Path $work 'x'
    Expand-Archive -Path $zip -DestinationPath $extract -Force

    # 不同发行包内部结构不同（bin/ 或直接平铺），统一按文件名递归查找
    $found = @{}
    foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
        $f = Get-ChildItem -Path $extract -Recurse -Filter $name -File |
             Sort-Object Length -Descending | Select-Object -First 1
        if (-not $f) { throw "压缩包内未找到 $name" }
        $found[$name] = $f.FullName
    }

    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    foreach ($name in $found.Keys) {
        Copy-Item -Path $found[$name] -Destination (Join-Path $OutDir $name) -Force
    }

    Write-Host "  已安装到 $OutDir" -ForegroundColor Green
    & $ffmpegExe -version | Select-Object -First 1
    & $ffprobeExe -version | Select-Object -First 1

    Write-Host ''
    Write-Host '许可提示：Windows 静态构建多为 GPL 授权（含 libx264 等）。' -ForegroundColor Yellow
    Write-Host '  本脚本仅用于构建时获取二进制；仓库不提交它们。分发安装包时请一并' -ForegroundColor Yellow
    Write-Host '  提供对应许可文本，并在产品说明中注明 ffmpeg 来源。' -ForegroundColor Yellow
} finally {
    if (Test-Path $work) { Remove-Item -Path $work -Recurse -Force -ErrorAction SilentlyContinue }
}
