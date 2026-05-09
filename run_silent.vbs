Dim fso, dir, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "cmd /c cd /d """ & dir & """ && venv\Scripts\pythonw.exe main.py"
CreateObject("WScript.Shell").Run cmd, 0, False
