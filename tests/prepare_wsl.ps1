# Prepare only GitHub's disposable Windows acceptance machine.
# Project operators follow docs/windows-runner.md instead.
$ErrorActionPreference = 'Stop'

wsl.exe --version
if ($LASTEXITCODE -ne 0) { throw 'WSL 2 is unavailable on this Windows machine' }
wsl.exe --install --distribution Ubuntu-24.04 --no-launch --web-download
if ($LASTEXITCODE -ne 0) { throw 'Ubuntu 24.04 installation failed' }

wsl.exe --distribution Ubuntu-24.04 --user root --exec sh -c 'printf "[boot]\nsystemd=true\n" > /etc/wsl.conf'
if ($LASTEXITCODE -ne 0) { throw 'WSL configuration failed' }
wsl.exe --terminate Ubuntu-24.04
if ($LASTEXITCODE -ne 0) { throw 'WSL restart failed' }

$source = (& wsl.exe --distribution Ubuntu-24.04 --user root --exec wslpath -a -u $env:GITHUB_WORKSPACE).Trim()
if ($LASTEXITCODE -ne 0 -or -not $source.StartsWith('/')) { throw 'Cannot resolve the source checkout in WSL' }
wsl.exe --distribution Ubuntu-24.04 --user root --exec bash "$source/tests/install_docker_engine.sh"
if ($LASTEXITCODE -ne 0) { throw 'Docker Engine installation or host checks failed' }
