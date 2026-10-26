# ClipMaker first-time setup (Windows). Run by start.bat - you don't need to open this.
# Downloads a private copy of everything ClipMaker needs into the "runtime" folder.
# Nothing is installed on your PC: delete the clipmaker folder and it's all gone.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rt  = Join-Path $dir 'runtime'
$tmp = Join-Path $dir 'runtime_download'
$pyver = '3.12.10'

if (Test-Path $rt)  { Remove-Item $rt -Recurse -Force }
New-Item -ItemType Directory -Force -Path $rt, $tmp | Out-Null

Write-Host '[1/4] Downloading the engine...'
$pyzip = Join-Path $tmp 'py.zip'
Invoke-WebRequest "https://www.python.org/ftp/python/$pyver/python-$pyver-embed-amd64.zip" -OutFile $pyzip
Expand-Archive $pyzip -DestinationPath $rt -Force

# let it use add-on packages and find ClipMaker's own files in the folder above
$pth = Get-ChildItem $rt -Filter 'python*._pth' | Select-Object -First 1
$lines = Get-Content $pth.FullName | ForEach-Object { $_ -replace '^#\s*import site', 'import site' }
$lines += '..'
Set-Content -Path $pth.FullName -Value $lines

Write-Host '[2/4] Downloading the video tool...'
$ffzip = Join-Path $tmp 'ffmpeg.zip'
Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $ffzip
Expand-Archive $ffzip -DestinationPath (Join-Path $tmp 'ff') -Force
$ffexe = Get-ChildItem (Join-Path $tmp 'ff') -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
Copy-Item $ffexe.FullName (Join-Path $rt 'ffmpeg.exe')

Write-Host '[3/4] Getting ready...'
$getpip = Join-Path $tmp 'get-pip.py'
Invoke-WebRequest 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getpip
& (Join-Path $rt 'python.exe') $getpip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw 'Step 3 failed.' }

Write-Host '[4/4] Downloading ClipMaker parts (the biggest step, a few minutes)...'
& (Join-Path $rt 'python.exe') -m pip install --no-warn-script-location -r (Join-Path $dir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Step 4 failed.' }

Remove-Item $tmp -Recurse -Force
New-Item -ItemType File -Path (Join-Path $rt 'ready.txt') -Force | Out-Null
Write-Host 'Setup finished!'
