<#
.SYNOPSIS
    编译 Web Crawler 原生 Windows 桌面程序（基于系统内置 WebView2，无黑框、无浏览器标签页）。
.DESCRIPTION
    使用 Windows 内置的 .NET Framework csc.exe 与微软官方 Microsoft.Web.WebView2 程序集编译
    desktop-app\Web Crawler.exe。关闭窗口时自动通过 Windows Job Object 清理后台本地服务进程。
.PARAMETER DesktopShortcut
    同时在当前用户桌面创建快捷方式。
.PARAMETER PowerMode
    将本机编译的桌面程序默认设为满血模式（WEB_CRAWLER_POWER_MODE=1，仅限个人可信/授权测试环境；运行时可传 --safe 临时回退安全模式）。
#>
param(
    [switch]$DesktopShortcut,
    [switch]$PowerMode
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutDir = Join-Path $RepoRoot "desktop-app"
$ExeName = "Web Crawler.exe"
$ExePath = Join-Path $OutDir $ExeName
$SourceFile = Join-Path $PSScriptRoot "desktop\DesktopHost.cs"
$IconFile = Join-Path $PSScriptRoot "desktop\app.ico"
$WebView2Version = "1.0.4370.26"

# 下载并缓存微软官方 Microsoft.Web.WebView2 程序集（仅首次联网，约 2 MB）
$CacheDir = Join-Path $env:LOCALAPPDATA "xiaomao-desktop-cache\webview2\$WebView2Version"
$CoreDll = Join-Path $CacheDir "Microsoft.Web.WebView2.Core.dll"
$WinFormsDll = Join-Path $CacheDir "Microsoft.Web.WebView2.WinForms.dll"
$LoaderDll = Join-Path $CacheDir "WebView2Loader.dll"

if (-not ((Test-Path -LiteralPath $CoreDll) -and (Test-Path -LiteralPath $WinFormsDll) -and (Test-Path -LiteralPath $LoaderDll))) {
    Write-Host "[Desktop] 下载 Microsoft.Web.WebView2 $WebView2Version 程序集..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    $NupkgUrl = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$WebView2Version/microsoft.web.webview2.$WebView2Version.nupkg"
    $ZipPath = Join-Path $CacheDir "webview2.zip"
    $ExtractDir = Join-Path $CacheDir "pkg"
    Invoke-WebRequest -UseBasicParsing -Uri $NupkgUrl -OutFile $ZipPath
    if (Test-Path -LiteralPath $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force

    $NetLib = Get-ChildItem -Path (Join-Path $ExtractDir "lib") -Directory |
        Where-Object { $_.Name -match '^net4[5-8]' } |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if (-not $NetLib) { throw "在 WebView2 包中未找到 net4x 程序集目录" }

    Copy-Item -Force (Join-Path $NetLib.FullName "Microsoft.Web.WebView2.Core.dll") $CoreDll
    Copy-Item -Force (Join-Path $NetLib.FullName "Microsoft.Web.WebView2.WinForms.dll") $WinFormsDll
    Copy-Item -Force (Join-Path $ExtractDir "runtimes\win-x64\native\WebView2Loader.dll") $LoaderDll
    Remove-Item -Force $ZipPath
    Remove-Item -Recurse -Force $ExtractDir
}

# 使用 Windows 内置 .NET Framework csc.exe 编译原生桌面程序
$Csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $Csc)) {
    throw "未找到 Windows .NET Framework 编译器：$Csc"
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Copy-Item -Force $CoreDll $OutDir
Copy-Item -Force $WinFormsDll $OutDir
Copy-Item -Force $LoaderDll $OutDir

$CscArgs = @(
    "/nologo",
    "/target:winexe",
    "/platform:x64",
    "/optimize+",
    "/utf8output",
    "/out:$ExePath",
    "/r:System.dll",
    "/r:System.Drawing.dll",
    "/r:System.Windows.Forms.dll",
    "/r:System.Net.Http.dll",
    "/r:$CoreDll",
    "/r:$WinFormsDll"
)
if (Test-Path -LiteralPath $IconFile) {
    $CscArgs += "/win32icon:$IconFile"
}
if ($PowerMode) {
    $CscArgs += "/define:DEFAULT_POWER_MODE"
    Write-Host "[Desktop] 本机编译已启用默认满血模式（WEB_CRAWLER_POWER_MODE=1，可用 --safe 回退安全模式）" -ForegroundColor Yellow
}
$CscArgs += $SourceFile

Write-Host "[Desktop] 编译 $ExeName ..." -ForegroundColor Cyan
& $Csc @CscArgs
if ($LASTEXITCODE -ne 0) {
    throw "编译失败（csc 退出码 $LASTEXITCODE）"
}

Write-Host "[Desktop] 构建完成：$ExePath" -ForegroundColor Green

if ($DesktopShortcut) {
    $Desktop = [Environment]::GetFolderPath("Desktop")
    $LnkPath = Join-Path $Desktop "Web Crawler.lnk"
    $Wsh = New-Object -ComObject WScript.Shell
    $Shortcut = $Wsh.CreateShortcut($LnkPath)
    $Shortcut.TargetPath = $ExePath
    $Shortcut.WorkingDirectory = $OutDir
    if (Test-Path -LiteralPath $IconFile) { $Shortcut.IconLocation = $IconFile }
    $Shortcut.Description = "Web Crawler 控制台"
    $Shortcut.Save()
    Write-Host "[Desktop] 已创建桌面快捷方式：$LnkPath" -ForegroundColor Green
}
