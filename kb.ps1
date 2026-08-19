[CmdletBinding()]
param(
    [Parameter(ValueFromPipeline = $true)]
    [AllowEmptyString()]
    [AllowNull()]
    [object] $InputObject,

    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $KbArguments
)

begin {
    $pipelineItems = [System.Collections.Generic.List[string]]::new()
}

process {
    if ($null -ne $InputObject) {
        $pipelineItems.Add([string] $InputObject)
    }
}

end {

$pythonCandidates = @()
$systemPython = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $systemPython) {
    $pythonCandidates += $systemPython.Source
}
$pythonCandidates += Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

$pythonExecutable = $null
foreach ($candidate in $pythonCandidates | Select-Object -Unique) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        continue
    }
    & $candidate -c 'import sys' 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pythonExecutable = $candidate
        break
    }
}

if ($null -eq $pythonExecutable) {
    Write-Error 'No usable Python interpreter was found. Install Python 3.11+ or restore the Codex bundled runtime.'
    exit 4
}

$env:PYTHONPATH = Join-Path $PSScriptRoot 'src'
$env:PYTHONUTF8 = '1'
if ($pipelineItems.Count -gt 0) {
    ($pipelineItems -join [Environment]::NewLine) | & $pythonExecutable -B -m kb2.cli --root $PSScriptRoot --json @KbArguments
} else {
    & $pythonExecutable -B -m kb2.cli --root $PSScriptRoot --json @KbArguments
}
exit $LASTEXITCODE
}
