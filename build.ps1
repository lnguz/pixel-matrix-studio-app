# Compila Pixel Matrix Studio en un unico .exe.
#   .\build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# El exe anterior queda bloqueado si la app sigue abierta.
Get-Process -Name PixelMatrixStudio -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "PixelMatrixStudio" --icon "icono.ico" `
    --collect-all pypixelcolor --collect-all bleak --collect-all winrt `
    --exclude-module matplotlib --exclude-module scipy --exclude-module pandas `
    --exclude-module PyQt5 --exclude-module PySide2 --exclude-module IPython `
    pixel_matrix_studio.py

if (-not (Test-Path ".\dist\PixelMatrixStudio.exe")) {
    throw "La compilacion no genero el ejecutable."
}

Remove-Item ".\PixelMatrixStudio.exe" -Force -ErrorAction SilentlyContinue
Move-Item ".\dist\PixelMatrixStudio.exe" ".\PixelMatrixStudio.exe" -Force
Remove-Item ".\build", ".\dist", ".\__pycache__" -Recurse -Force -ErrorAction SilentlyContinue

$f = Get-Item ".\PixelMatrixStudio.exe"
"Listo: $($f.FullName)  ({0:N1} MB)" -f ($f.Length / 1MB)
