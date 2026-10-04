@echo off
rem runner from PowerShell or cmd: runs the runner script (this folder) with Git for Windows' bash.
rem Git's bin\bash.exe, never WSL's bash.exe, which would run it inside Linux without gh.
setlocal
set "RUNNER_GITBASH="
for /f "delims=" %%G in ('where git 2^>nul') do (
  if not defined RUNNER_GITBASH if exist "%%~dpG..\bin\bash.exe" set "RUNNER_GITBASH=%%~dpG..\bin\bash.exe"
)
if not defined RUNNER_GITBASH if exist "%ProgramFiles%\Git\bin\bash.exe" set "RUNNER_GITBASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined RUNNER_GITBASH if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "RUNNER_GITBASH=%LocalAppData%\Programs\Git\bin\bash.exe"
if not defined RUNNER_GITBASH (
  echo runner: Git for Windows not found. Install it with: winget install Git.Git
  exit /b 1
)
rem From this folder, so the script finds its own files; the arguments are never paths.
pushd "%~dp0"
"%RUNNER_GITBASH%" ./runner %*
set "rc=%ERRORLEVEL%"
popd
exit /b %rc%
