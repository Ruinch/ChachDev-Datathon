param([ValidateSet('2b','4b','3.5-4b','3.5-9b','3.5-9b-q3')][string]$Model = '3.5-4b',
      [ValidateRange(1,65535)][int]$Port = 8091,
      [ValidateRange(64,4096)][int]$ImageMinTokens = 256,
      [ValidateRange(64,4096)][int]$ImageMaxTokens = 768,
      [ValidateRange(4096,32768)][int]$ContextSize = 16384,
      [ValidatePattern('^(all|[0-9]{1,2})$')][string]$GpuLayers = 'all',
      [ValidateRange(0,4096)][int]$ReasoningBudget = 0, [switch]$Background)
$ErrorActionPreference = 'Stop'
if ($ImageMinTokens -gt $ImageMaxTokens) { throw 'ImageMinTokens must not exceed ImageMaxTokens' }
$projectRoot = Split-Path $PSScriptRoot -Parent
$runtime = Join-Path $projectRoot '.local_ml/llama/llama-server.exe'
$weightsName = switch ($Model) { '3.5-9b-q3' { 'Qwen3.5-9B-Q3_K_M.gguf' }; '3.5-9b' { 'Qwen3.5-9B-Q4_K_M.gguf' }; '3.5-4b' { 'Qwen3.5-4B-Q4_K_M.gguf' }; '4b' { 'Qwen3VL-4B-Instruct-Q4_K_M.gguf' }; '2b' { 'Qwen3-VL-2B-Instruct-Q8_0.gguf' } }
$projectorName = switch ($Model) { { $_ -in @('3.5-9b','3.5-9b-q3') } { 'mmproj-Qwen3.5-9B-F16.gguf' }; '3.5-4b' { 'mmproj-F16.gguf' }; '4b' { 'mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf' }; '2b' { 'mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf' } }
if ($Model -eq '3.5-9b' -and -not $PSBoundParameters.ContainsKey('GpuLayers')) { $GpuLayers = '24' }
if ($Model -eq '3.5-9b-q3' -and -not $PSBoundParameters.ContainsKey('GpuLayers')) { $GpuLayers = '30' }
$weights = Join-Path $projectRoot ('.local_ml/models/' + $weightsName)
$projector = Join-Path $projectRoot ('.local_ml/models/' + $projectorName)
foreach ($file in @($runtime, $weights, $projector)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing local runtime file: $file" }
}
# No -hf or remote model URLs: inference starts only from local files.
$modelArguments = @('-m', $weights, '--mmproj', $projector, '--host', '127.0.0.1', '--port', "$Port",
    '--alias', 'local-cash-vlm', '--ctx-size', "$ContextSize", '--parallel', '1', '--gpu-layers', $GpuLayers,
    '--flash-attn', 'on', '--reasoning', $(if ($ReasoningBudget) { 'on' } else { 'off' }), '--image-min-tokens', "$ImageMinTokens",
    '--image-max-tokens', "$ImageMaxTokens", '--offline', '--log-disable')
# Keep pairs adjacent so Qwen's temporal encoder can merge consecutive frames.
$modelArguments += @('--video-fps', '1', '--video-timestamp-interval', '2000')
$videoTools = Join-Path $projectRoot '.local_tools/ffmpeg/bin'
if ($env:APP_FFMPEG_BIN -and (Test-Path -LiteralPath $env:APP_FFMPEG_BIN -PathType Leaf)) {
    $configuredVideoTools = Split-Path -Parent $env:APP_FFMPEG_BIN
    if (Test-Path -LiteralPath (Join-Path $configuredVideoTools 'ffmpeg.exe')) { $videoTools = $configuredVideoTools }
}
if (Test-Path -LiteralPath (Join-Path $videoTools 'ffmpeg.exe')) {
    $modelArguments += @('--video-ffmpeg-dir', $videoTools)
}
if ($ReasoningBudget) { $modelArguments += @('--reasoning-budget', "$ReasoningBudget") }
if ($Background) {
    $quoted = $modelArguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    return Start-Process -FilePath $runtime -ArgumentList $quoted -WindowStyle Hidden -PassThru
}
& $runtime @modelArguments
exit $LASTEXITCODE
