# 使用现有Python运行，不安装依赖，不保存密钥。
$ErrorActionPreference = 'Stop'
$taskPythonPath = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
Push-Location -LiteralPath $PSScriptRoot
try {
    $env:PYTHONIOENCODING = 'utf-8'
    if (Test-Path -LiteralPath $taskPythonPath) {
        & $taskPythonPath -m finblocks @args
    } else {
        & py -3 -m finblocks @args
    }
    $taskExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $taskExitCode
