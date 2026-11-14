' REGTeches RC - Start Menu launcher for a Scoop install (no console window).
' Keeps servers, logins and settings in Scoop's persist folder so updates never touch them.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Environment("PROCESS")("RC_DATA_DIR") = fso.GetAbsolutePathName(here & "\..\..\..\persist\rc")
sh.Run """" & here & "\RC.exe""", 1, False
