@echo off
rem Add or remove the cxxref usage instructions: local (only you; .github\instructions\cxxref.instructions.md, hidden from git)
rem or project (.github\copilot-instructions.md; everyone once committed).
rem Usage: setup-instructions.bat [-Root <src>] [-Action Add|Remove|Status] [-Scope Local|Project]   (no arguments: asks)
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-instructions.ps1" %*
if "%~1"=="" pause
