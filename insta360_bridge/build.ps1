# Build insta360_bridge with the newest installed MSVC (VS 2019 or 2022).
# Usage: .\build.ps1

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sdkRoot = if ($env:INSTA360_SDK_ROOT) { $env:INSTA360_SDK_ROOT } else {
    Join-Path $repoRoot "Windows_CameraSDK-2.1.1_MediaSDK-3.1.3"
}

if (-not (Test-Path (Join-Path $sdkRoot "CameraSDK-20250812_192505-2.1.1-win64\lib\CameraSDK.lib"))) {
    Write-Error "Insta360 SDK not found at $sdkRoot. Set INSTA360_SDK_ROOT."
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
    Write-Error "vswhere not found. Install Visual Studio 2019/2022 Build Tools with the C++ workload."
}

$installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $installPath) {
    Write-Error @"
No MSVC toolchain found. Install one of:
  winget install Microsoft.VisualStudio.2022.BuildTools
Then open 'Visual Studio Installer' -> Modify -> check 'Desktop development with C++'.
"@
}

$version = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property catalog_productLineVersion
$generator = switch ($version) {
    "2022" { "Visual Studio 17 2022" }
    "2019" { "Visual Studio 16 2019" }
    default {
        Write-Warning "Unknown VS version '$version'; trying Visual Studio 16 2019"
        "Visual Studio 16 2019"
    }
}

Write-Host "Using $generator at $installPath"
Write-Host "INSTA360_SDK_ROOT=$sdkRoot"

$buildDir = Join-Path $PSScriptRoot "build"
if (Test-Path $buildDir) {
    Remove-Item -Recurse -Force $buildDir
}

Push-Location $PSScriptRoot
try {
    cmake -B build -G $generator -A x64 -DINSTA360_SDK_ROOT="$sdkRoot"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    cmake --build build --config Release
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
