[CmdletBinding()]
param(
    [switch]$Demo,
    [string]$Config,
    [string]$Screenshot,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$stateDir = Join-Path $projectRoot '.state'
$logPath = Join-Path $stateDir 'launcher.log'
$venvDir = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$venvPythonw = Join-Path $venvDir 'Scripts\pythonw.exe'
$requirementsFile = Join-Path $projectRoot 'requirements.txt'

function Write-Log([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Show-Message([string]$Message, [string]$Title, [int]$Style = 0) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup($Message, 0, $Title, $Style)
    } catch {
        Write-Log "无法显示消息框：$($_.Exception.Message)"
    }
}

function Quote-ProcessArgument([string]$Value) {
    # Windows 命令行的标准双引号转义，兼容含空格和中文的路径。
    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Invoke-CapturedProcess([string]$FilePath, [string[]]$Arguments) {
    # 不经过 PS 5.1 的原生命令参数重组；保留 Python -c 中的引号。
    # 同时直接读取 stderr，避免 ErrorActionPreference=Stop 截断 traceback。
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (($Arguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join ' ')
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [Text.Encoding]::UTF8
    $startInfo.StandardErrorEncoding = [Text.Encoding]::UTF8
    $startInfo.EnvironmentVariables['PYTHONUTF8'] = '1'
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw "无法启动程序：$FilePath" }
        # 两个管道同时读取，防止大量输出填满其中一个管道。
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Stdout = $stdoutTask.GetAwaiter().GetResult()
            Stderr = $stderrTask.GetAwaiter().GetResult()
        }
    } finally {
        $process.Dispose()
    }
}

function Invoke-Logged([string]$FilePath, [string[]]$Arguments, [string]$Label) {
    Write-Log "开始：$Label"
    $result = Invoke-CapturedProcess $FilePath $Arguments
    foreach ($output in @($result.Stdout, $result.Stderr)) {
        if ($output) { Add-Content -LiteralPath $logPath -Value $output.TrimEnd() -Encoding UTF8 }
    }
    Write-Log "结束：$Label（退出码 $($result.ExitCode)）"
    return $result.ExitCode
}

function Get-PinnedPySide6Version {
    $versions = @(
        foreach ($line in Get-Content -LiteralPath $requirementsFile -Encoding UTF8) {
            if ($line -match '^\s*PySide6\s*==\s*([^\s;#]+)\s*(?:#.*)?$') {
                $Matches[1]
            } elseif ($line -match '^\s*PySide6(?:\s|[<>=!~])') {
                throw 'requirements.txt 中的 PySide6 必须使用唯一的固定版本（PySide6==版本号）。'
            }
        }
    )
    if ($versions.Count -ne 1) {
        throw 'requirements.txt 中必须恰好包含一条 PySide6 固定版本声明（PySide6==版本号）。'
    }
    return [string]$versions[0]
}

function Get-InstalledPySide6Version {
    $result = Invoke-CapturedProcess $venvPython @('-c', 'import importlib.metadata as metadata; import PySide6; print(metadata.version("PySide6"))')
    if ($result.ExitCode -ne 0) {
        Write-Log "PySide6 检查失败（退出码 $($result.ExitCode)）。"
        if ($result.Stderr) { Add-Content -LiteralPath $logPath -Value $result.Stderr.TrimEnd() -Encoding UTF8 }
        return $null
    }
    $version = $result.Stdout.Trim()
    if (-not $version) { return $null }
    return $version
}

function Test-PySide6VersionMismatch([string]$RequiredVersion, [string]$InstalledVersion) {
    return $InstalledVersion -ne $RequiredVersion
}

try {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    Write-Log '启动器开始。'

    if (-not (Test-Path -LiteralPath $requirementsFile -PathType Leaf)) {
        throw "找不到依赖文件：$requirementsFile"
    }

    if (-not ((Test-Path -LiteralPath $venvPython -PathType Leaf) -and
              (Test-Path -LiteralPath $venvPythonw -PathType Leaf))) {
        $systemPython = Get-Command 'python.exe' -ErrorAction SilentlyContinue
        if (-not $systemPython) { $systemPython = Get-Command 'python' -ErrorAction SilentlyContinue }
        $pythonArgs = @()
        if ($systemPython) {
            $systemPythonPath = $systemPython.Source
            $versionCode = Invoke-Logged $systemPythonPath @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 2)') '检查 Python 版本'
            if ($versionCode -eq 2) { throw '需要 Python 3.11 或更新版本。' }
            if ($versionCode -ne 0) { throw "无法运行 Python：$systemPythonPath" }
        } else {
            $pythonLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
            if (-not $pythonLauncher) { $pythonLauncher = Get-Command 'py' -ErrorAction SilentlyContinue }
            if (-not $pythonLauncher) { throw '未找到 Python。请安装 Python 3.11 或更新版本，并启用 PATH。' }
            $systemPythonPath = $pythonLauncher.Source
            $pythonArgs = @('-3')
            $versionArgs = @($pythonArgs) + @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 2)')
            $versionCode = Invoke-Logged $systemPythonPath $versionArgs '检查 Python Launcher 版本'
            if ($versionCode -eq 2) { throw '需要 Python 3.11 或更新版本。' }
            if ($versionCode -ne 0) { throw "无法通过 Python Launcher 运行 Python 3：$systemPythonPath" }
        }

        Write-Log '项目虚拟环境不存在或不完整，正在创建 .venv。'
        $createArgs = @($pythonArgs) + @('-m', 'venv', $venvDir)
        $code = Invoke-Logged $systemPythonPath $createArgs '创建项目虚拟环境'
        if ($code -ne 0) { throw '创建 .venv 失败。请检查 Python 安装和项目目录权限。' }
    }

    if (-not ((Test-Path -LiteralPath $venvPython -PathType Leaf) -and
              (Test-Path -LiteralPath $venvPythonw -PathType Leaf))) {
        throw '项目虚拟环境中缺少 python.exe 或 pythonw.exe。'
    }

    $versionCode = Invoke-Logged $venvPython @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 2)') '检查项目 Python 版本'
    if ($versionCode -eq 2) { throw '项目虚拟环境需要 Python 3.11 或更新版本。' }
    if ($versionCode -ne 0) { throw '无法运行项目虚拟环境中的 Python。' }

    $requiredPySide6Version = Get-PinnedPySide6Version
    $installedPySide6Version = Get-InstalledPySide6Version
    if (Test-PySide6VersionMismatch $requiredPySide6Version $installedPySide6Version) {
        if ($installedPySide6Version) {
            Write-Log "PySide6 版本为 $installedPySide6Version，要求 $requiredPySide6Version；正在校正依赖。"
        } else {
            Write-Log 'PySide6 尚未安装或无法导入；正在安装依赖。'
        }
        $installArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $requirementsFile)
        $code = Invoke-Logged $venvPython $installArgs '安装桌面界面依赖'
        if ($code -ne 0) { throw "依赖安装失败。请检查网络连接和 requirements.txt；详情见 $logPath" }
        $installedPySide6Version = Get-InstalledPySide6Version
        if (Test-PySide6VersionMismatch $requiredPySide6Version $installedPySide6Version) {
            $actual = if ($installedPySide6Version) { $installedPySide6Version } else { '无法导入或读取版本' }
            throw "PySide6 安装后版本不符：要求 $requiredPySide6Version，实际 $actual；详情见 $logPath"
        }
    }

    $cliArgs = @('-m', 'linkmanager')
    if ($Config) {
        $configPath = if ([IO.Path]::IsPathRooted($Config)) {
            [IO.Path]::GetFullPath($Config)
        } else {
            [IO.Path]::GetFullPath((Join-Path $projectRoot $Config))
        }
        $cliArgs += @('--config', $configPath)
    }
    if ($Demo) { $cliArgs += '--demo' }
    if ($Screenshot) {
        $screenshotPath = if ([IO.Path]::IsPathRooted($Screenshot)) {
            [IO.Path]::GetFullPath($Screenshot)
        } else {
            [IO.Path]::GetFullPath((Join-Path $projectRoot $Screenshot))
        }
        $cliArgs += @('--screenshot', $screenshotPath)
    }
    if ($Check) {
        $cliArgs += '--check'
        $result = Invoke-CapturedProcess $venvPython $cliArgs
        $output = $result.Stdout + $result.Stderr
        $code = $result.ExitCode
        if ($output) { $output.TrimEnd() | ForEach-Object { Write-Log $_ } }
        if ($code -eq 0) {
            Show-Message ($output.Trim() + "`n`n日志：$logPath") '配置检查完成' 0x40
            exit 0
        }
        Show-Message (($output.Trim()) + "`n`n日志：$logPath") '配置检查失败' 0x10
        exit $code
    }

    $argumentString = (($cliArgs | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join ' ')
    $process = Start-Process -FilePath $venvPythonw -ArgumentList $argumentString `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    Write-Log "GUI 已启动，PID=$($process.Id)。"
} catch {
    $message = $_.Exception.Message
    try {
        if (-not (Test-Path -LiteralPath $stateDir -PathType Container)) {
            New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
        }
        Write-Log "启动失败：$message"
    } catch { }
    Show-Message "$message`n`n详情日志：$logPath" '软链接管理器启动失败' 0x10
    exit 1
}
