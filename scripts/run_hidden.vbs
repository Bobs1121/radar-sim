Option Explicit

' Launch one PowerShell script without creating a visible console window.
' The first argument is the script path; all remaining arguments are passed
' through as individually quoted values so paths containing spaces remain safe.

If WScript.Arguments.Count < 1 Then
    WScript.Quit 2
End If

Function QuoteArgument(ByVal value)
    QuoteArgument = Chr(34) & Replace(CStr(value), Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function

Dim command, index, shell
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " _
    & QuoteArgument(WScript.Arguments(0))

For index = 1 To WScript.Arguments.Count - 1
    command = command & " " & QuoteArgument(WScript.Arguments(index))
Next

Set shell = CreateObject("WScript.Shell")
shell.Run command, 0, False
WScript.Quit 0
