# QYH-GD300 上位机 —— 一键打包为单文件桌面 exe
#
# 用法（在项目根目录下）：
#     powershell -ExecutionPolicy Bypass -File build/build_exe.ps1
#
# 产物：build/dist/QYH-GD300上位机.exe

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "[1/3] PyInstaller 打包中（单文件，首次较慢）..."
python -m PyInstaller build/gd300.spec --noconfirm --clean `
    --workpath build/pyinstaller-work --distpath build/dist
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败，退出码 $LASTEXITCODE" }

$Exe = Join-Path $Root "build/dist/QYH-GD300上位机.exe"
if (-not (Test-Path $Exe)) { throw "未找到产物：$Exe" }

Write-Host "[2/3] 自检产物（二进制链路，--proto 25）..."
# --proto 25 是必须的：自检断言"模拟源 25 字节帧零 CRC 错"，
# 若产物旁 settings.json 里存的是别的帧格式，这个断言会假失败。
# 注意：exe 是 GUI 子系统程序，PowerShell 不会自动等待，必须 Start-Process -Wait
$proc = Start-Process -FilePath $Exe -ArgumentList "--mock", "--self-check", "--proto", "25" `
    -Wait -NoNewWindow -PassThru
$code = $proc.ExitCode
if ($code -ne 0) { throw "二进制链路自检未通过，退出码 $code" }

Write-Host "[3/3] 自检产物（文本行链路，--proto text）..."
$proc = Start-Process -FilePath $Exe -ArgumentList "--mock", "--self-check", "--proto", "text" `
    -Wait -NoNewWindow -PassThru
$code = $proc.ExitCode

$size = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
Write-Host ""
Write-Host "产物: $Exe  ($size MB)"
if ($code -ne 0) { throw "文本链路自检未通过，退出码 $code" }
Write-Host "自检通过。双击该 exe 即可启动桌面端。"