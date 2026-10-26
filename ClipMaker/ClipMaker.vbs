' ClipMaker launcher: starts ClipMaker quietly (no black window).
' The very first time it runs the setup window instead, since that needs to show progress.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
If fso.FileExists(dir & "\runtime\ready.txt") And fso.FileExists(dir & "\runtime\pythonw.exe") Then
  sh.Run """" & dir & "\runtime\pythonw.exe"" """ & dir & "\server.py"" --launch", 0, False
Else
  sh.Run """" & dir & "\start.bat""", 1, False
End If
