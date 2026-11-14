@echo off
rem REGTeches RC - launcher for a Scoop install.
rem Scoop replaces this program folder on every update, so keep servers, logins and settings in Scoop's
rem persist folder (...\scoop\persist\rc), which sits three folders up from here and survives updates.
set "RC_DATA_DIR=%~dp0..\..\..\persist\rc"
start "" "%~dp0RC.exe" %*
