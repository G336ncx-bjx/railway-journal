$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.build-venv\Scripts\python.exe')) {
    python -m venv .build-venv
    if ($LASTEXITCODE -ne 0) { throw '创建构建环境失败' }
}
& '.\.build-venv\Scripts\python.exe' -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw '安装打包依赖失败' }
& '.\.build-venv\Scripts\python.exe' -m PyInstaller --noconfirm --clean --onefile --windowed --name RailwayJournal --icon 'web\logo.ico' --add-data 'web:web' desktop.py
if ($LASTEXITCODE -ne 0) { throw '生成 EXE 失败' }
Write-Host '完成：dist\RailwayJournal.exe。单文件，不需要 Python 或手动解压。'
