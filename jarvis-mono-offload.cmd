@echo off
setlocal
REM Alias for the stability / partial-offload 31B profile (same as jarvis-mono.cmd).
call "%~dp0jarvis.cmd" start -Profile gemma4-mono %*
