#requires -Version 5.1
<#
.SYNOPSIS
    Автозапуск торгового бота при входе в систему (Планировщик задач Windows).

.DESCRIPTION
    Создаёт задачу "tbank-daemon" (ONLOGON, LIMITED - без прав администратора),
    которая поднимает headless-демон: pythonw.exe cli.py daemon run.
    Демон переживает закрытие TUI и пишет лог в var\daemon.log.

.PARAMETER Uninstall
    Удаляет задачу автозапуска. Если задачи нет - скрипт завершается успешно.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskName = 'tbank-daemon'
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path -Path $PSScriptRoot -ChildPath '..\..'))
$PythonwPath = Join-Path -Path $RepoRoot -ChildPath '.venv\Scripts\pythonw.exe'
$CliPath = Join-Path -Path $RepoRoot -ChildPath 'cli.py'
$SchtasksPath = Join-Path -Path $env:SystemRoot -ChildPath 'System32\schtasks.exe'

function Invoke-Schtasks {
    # Запускает schtasks.exe с готовой строкой аргументов и возвращает код выхода + вывод.
    # Строка собирается вручную: в PowerShell 5.1 вложенные кавычки в /TR иначе теряются.
    param(
        [Parameter(Mandatory = $true)]
        [string]$Arguments
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $SchtasksPath
    $startInfo.Arguments = $Arguments
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    [void]$process.WaitForExit()

    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        Output   = (($stdout.Trim() + [Environment]::NewLine + $stderr.Trim()).Trim())
    }
}

if ($Uninstall) {
    Write-Host "== Удаление автозапуска '$TaskName'"

    # Сначала проверяем существование: так "задачи нет" не путается с отказом в доступе.
    $query = Invoke-Schtasks -Arguments ('/Query /TN "{0}"' -f $TaskName)
    if ($query.ExitCode -ne 0) {
        if ($query.Output -match '(?i)отказано в доступе|access is denied') {
            Write-Host "[!!] Нет доступа к Планировщику задач: $($query.Output)"
            Write-Host "     Запустите PowerShell от текущего пользователя (права администратора не нужны)."
            exit 1
        }
        Write-Host "[i] Задача не найдена: '$TaskName' - автозапуск не установлен или уже удалён."
        exit 0
    }

    $delete = Invoke-Schtasks -Arguments ('/Delete /TN "{0}" /F' -f $TaskName)
    if ($delete.ExitCode -ne 0) {
        Write-Host "[!!] Не удалось удалить задачу '$TaskName' (код $($delete.ExitCode))."
        Write-Host $delete.Output
        exit 1
    }

    Write-Host "[ok] Автозапуск удалён: задача '$TaskName' больше не создаётся при входе."
    exit 0
}

Write-Host "== Установка автозапуска '$TaskName'"
Write-Host "   Каталог проекта: $RepoRoot"

if (-not (Test-Path -LiteralPath $PythonwPath -PathType Leaf)) {
    Write-Host "[!!] Не найден $PythonwPath"
    Write-Host "     Создайте окружение и установите зависимости:"
    Write-Host "       python -m venv .venv"
    Write-Host "       .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

if (-not (Test-Path -LiteralPath $CliPath -PathType Leaf)) {
    Write-Host "[!!] Не найден $CliPath - скрипт запускается из корня репозитория."
    exit 1
}

# Команда задачи: кавычки вокруг обоих путей - проект может лежать в каталоге с пробелами.
$taskCommand = '"{0}" "{1}" daemon run' -f $PythonwPath, $CliPath
$createArgs = '/Create /TN "{0}" /SC ONLOGON /RL LIMITED /F /TR "\"{1}\" \"{2}\" daemon run"' -f $TaskName, $PythonwPath, $CliPath

$create = Invoke-Schtasks -Arguments $createArgs
if ($create.ExitCode -ne 0) {
    Write-Host "[!!] Не удалось создать задачу '$TaskName' (код $($create.ExitCode))."
    Write-Host $create.Output
    Write-Host "     Проверьте, что служба 'Планировщик задач' запущена и политика не запрещает задачи пользователя."
    exit 1
}

Write-Host "[ok] Автозапуск установлен."
Write-Host "     Задача:  $TaskName (при входе в систему, без прав администратора)"
Write-Host "     Команда: $taskCommand"
Write-Host "     Лог:     var\daemon.log (в каталоге проекта; путь задаётся LOG_FILE в .env)"
Write-Host "     Стоп:    .venv\Scripts\python cli.py daemon stop"
Write-Host "     Удалить: powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1 -Uninstall"
exit 0
