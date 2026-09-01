[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CliArgs
)

$ErrorActionPreference = "Stop"
$cli = Join-Path $PSScriptRoot "runpod_ssh.py"
& python $cli @CliArgs
exit $LASTEXITCODE
