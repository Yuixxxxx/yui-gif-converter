[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$VenvDirectory = '.venv314',
    [switch]$RunSmokeTests
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Python {
    param([string[]]$Arguments)
    & $script:YuiPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

Push-Location -LiteralPath $PSScriptRoot
try {
    $script:YuiPython = Join-Path $PSScriptRoot ($VenvDirectory+'\Scripts\python.exe')
    if (-not (Test-Path -LiteralPath $script:YuiPython)) {
        & $Python -c "import sys,struct; assert sys.version_info >= (3,14,8) and sys.version_info[:2] == (3,14) and struct.calcsize('P') == 8, 'Use 64-bit Python 3.14.8 or newer 3.14 maintenance release.'"
        if ($LASTEXITCODE -ne 0) { throw 'Specify 64-bit Python 3.14.8 or newer 3.14 maintenance release with -Python.' }
        & $Python -m venv (Join-Path $PSScriptRoot $VenvDirectory)
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
    }
    Invoke-Python -Arguments @('-c', "import sys,struct; assert sys.version_info >= (3,14,8) and sys.version_info[:2] == (3,14) and struct.calcsize('P') == 8, 'Use 64-bit Python 3.14.8 or newer 3.14 maintenance release.'")
    Invoke-Python -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt', '-r', 'requirements-build.txt')
    Invoke-Python -Arguments @('tools\fetch_ffmpeg.py')
    Invoke-Python -Arguments @('tools\make_assets.py')
    Invoke-Python -Arguments @('tools\make_profile_button.py')
    Invoke-Python -Arguments @('tools\embed_release_materials.py', 'prepare')

    if ($RunSmokeTests) {
        Invoke-Python -Arguments @('tests\editing_settings_test.py', (Join-Path $PSScriptRoot 'work\editing-settings-check'))
        Invoke-Python -Arguments @('tests\editing_integration_test.py', (Join-Path $PSScriptRoot 'work\editing-integration-check'))
        Invoke-Python -Arguments @('tests\edit_engine_test.py', (Join-Path $PSScriptRoot 'work\edit-engine-check'))
        Invoke-Python -Arguments @('tests\media_tools_test.py', (Join-Path $PSScriptRoot 'work\media-tools-check'))
        Invoke-Python -Arguments @('tests\watermark_test.py', (Join-Path $PSScriptRoot 'work\watermark-check'))
        Invoke-Python -Arguments @('tests\dialogs_test.py', (Join-Path $PSScriptRoot 'work\dialogs-check'))
        Invoke-Python -Arguments @('tests\overlays_test.py', (Join-Path $PSScriptRoot 'work\overlay-check'))
        Invoke-Python -Arguments @('tests\designer_test.py', (Join-Path $PSScriptRoot 'work\designer-check'))
        Invoke-Python -Arguments @('tests\designer_integration_test.py', (Join-Path $PSScriptRoot 'work\designer-integration-check'))
        Invoke-Python -Arguments @('tests\drag_drop_test.py', (Join-Path $PSScriptRoot 'work\drag-drop-check'))
        Invoke-Python -Arguments @('tests\panel_corners_test.py', (Join-Path $PSScriptRoot 'work\panel-corners-check'))
        Invoke-Python -Arguments @('tests\in_app_bundle_test.py', (Join-Path $PSScriptRoot 'work\in-app-bundle-check'))
        Invoke-Python -Arguments @('-u', 'tests\smoke_test.py', (Join-Path $PSScriptRoot 'work\smoke'))
        Invoke-Python -Arguments @('tests\privacy_test.py', (Join-Path $PSScriptRoot 'work\privacy-check'))
        Invoke-Python -Arguments @('tests\preview_test.py', (Join-Path $PSScriptRoot 'work\preview-check'))
        Invoke-Python -Arguments @('tests\interaction_test.py', (Join-Path $PSScriptRoot 'work\interaction-check'))
        Invoke-Python -Arguments @('tests\footer_alignment_test.py', (Join-Path $PSScriptRoot 'work\footer-alignment-check'))
        Invoke-Python -Arguments @('tests\background_test.py', (Join-Path $PSScriptRoot 'work\smoke\media'),
                                  (Join-Path $PSScriptRoot 'work\background-check'))
        Invoke-Python -Arguments @('tests\reliability_test.py', (Join-Path $PSScriptRoot 'work\reliability-check'))
        Invoke-Python -Arguments @('tests\presets_test.py', (Join-Path $PSScriptRoot 'work\presets-check'))
        Invoke-Python -Arguments @('tests\preview_controls_test.py', (Join-Path $PSScriptRoot 'work\preview-controls-check'))
        Invoke-Python -Arguments @('tests\save_copy_test.py', (Join-Path $PSScriptRoot 'work\save-copy-check'))
        Invoke-Python -Arguments @('tests\features_test.py', (Join-Path $PSScriptRoot 'work\features-check'))
        Invoke-Python -Arguments @('tests\polish_test.py', (Join-Path $PSScriptRoot 'work\polish-check'))
        Invoke-Python -Arguments @('tests\security_paths_test.py', (Join-Path $PSScriptRoot 'work\security-paths-check'))
        Invoke-Python -Arguments @('tests\security_media_test.py', (Join-Path $PSScriptRoot 'work\security-media-check'))
        Invoke-Python -Arguments @('tests\security_integration_test.py', (Join-Path $PSScriptRoot 'work\security-integration-check'))
        Invoke-Python -Arguments @('tests\runtime_integrity_test.py', (Join-Path $PSScriptRoot 'work\runtime-integrity-check'))
    }

    Invoke-Python -Arguments @('-m', 'PyInstaller', '--noconfirm', 'YuiGifConverter.spec')
    Invoke-Python -Arguments @('tools\embed_release_materials.py', 'embed')
    $exe = Join-Path $PSScriptRoot 'dist\YuiGifConverter.exe'
    $testDir = Join-Path ([IO.Path]::GetTempPath()) ('yui-build-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $testDir | Out-Null
    try {
        $selftest = Start-Process -FilePath $exe -ArgumentList @('--selftest', ('"' + $testDir + '"')) -WindowStyle Hidden -PassThru
        if (-not $selftest.WaitForExit(180000)) {
            Stop-Process -Id $selftest.Id -ErrorAction SilentlyContinue
            throw 'The frozen selftest timed out after three minutes.'
        }
        $selftest.Refresh()
        $report = Join-Path $testDir 'selftest.json'
        if (-not (Test-Path -LiteralPath $report)) { throw 'The frozen selftest did not write its report.' }
        $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
        if ($selftest.ExitCode -ne 0 -or $result.ok -ne $true -or $result.size -le 0 -or $result.size -gt 10485760) {
            throw ('The frozen selftest failed: ' + (Get-Content -LiteralPath $report -Raw))
        }
        Copy-Item -LiteralPath $report -Destination (Join-Path $PSScriptRoot 'dist\selftest.json') -Force
        Write-Output ('Frozen selftest passed: ' + $result.size + ' bytes.')
    }
    finally {
        # Delete only the unique test folder created above, after checking its resolved parent.
        $resolvedTestDir = [IO.Path]::GetFullPath($testDir)
        $resolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if ($resolvedTestDir.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($resolvedTestDir).StartsWith('yui-build-')) {
            Remove-Item -LiteralPath $resolvedTestDir -Recurse -Force
        }
    }

    $version = (& $script:YuiPython -c 'from yui import VERSION; print(VERSION)').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the application version.' }
    $zip = Join-Path $PSScriptRoot ('dist\YuiGifConverter-' + $version + '-win64.zip')
    Invoke-Python -Arguments @('tools\package_release.py')
    Invoke-Python -Arguments @('tools\check_release_privacy.py')
    $fullZip = Join-Path $PSScriptRoot ('dist\YuiGifConverter-' + $version + '-full-package.zip')
    Get-FileHash -LiteralPath $exe, $zip, $fullZip -Algorithm SHA256 |
        ForEach-Object { $_.Hash + '  ' + [IO.Path]::GetFileName($_.Path) } |
        Set-Content -LiteralPath (Join-Path $PSScriptRoot 'dist\SHA256SUMS.txt') -Encoding ASCII
    Write-Output ('Built: ' + $exe)
    Write-Output ('Release: ' + $zip)
}
finally {
    Pop-Location
}
