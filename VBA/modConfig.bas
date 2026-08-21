Attribute VB_Name = "modConfig"
Option Explicit

' Config sheet (VeryHidden): key=col A, value=col B
' Keys: ServerUrl, API_KEY, LastRefresh, Mailbox
' Mailbox: which cos/mailboxes.py key this workbook is scoped to (e.g. "cfm",
' "edom", "dme"), baked in by create_triage_workbook.py at build time. Blank
' means unscoped — shows every mailbox mixed, matching pre-2026-08-21 behavior.

Private Const CONFIG_SHEET As String = "Config"
Private Const DEFAULT_URL   As String = "https://front-ai-review-2k7f2bz3dq-ue.a.run.app"


Public Function GetConfig(key As String) As String
    Dim ws As Worksheet
    Set ws = GetConfigSheet(True)   ' always create if missing so defaults are seeded
    Dim i As Long
    For i = 1 To 100
        If Trim(CStr(ws.Cells(i, 1).Value)) = key Then
            GetConfig = Trim(CStr(ws.Cells(i, 2).Value))
            Exit Function
        End If
        If ws.Cells(i, 1).Value = "" And i > 5 Then Exit For
    Next i
    GetConfig = ""
End Function


Public Sub SetConfig(key As String, value As String)
    Dim ws As Worksheet
    Set ws = GetConfigSheet(True)
    Dim i As Long
    For i = 1 To 100
        If Trim(CStr(ws.Cells(i, 1).Value)) = key Then
            ws.Cells(i, 2).Value = value
            Exit Sub
        End If
        If ws.Cells(i, 1).Value = "" Then
            ws.Cells(i, 1).Value = key
            ws.Cells(i, 2).Value = value
            Exit Sub
        End If
    Next i
End Sub


Private Function GetConfigSheet(createIfMissing As Boolean) As Worksheet
    Dim s As Object
    For Each s In ThisWorkbook.Sheets
        If s.Name = CONFIG_SHEET Then
            Set GetConfigSheet = s
            Exit Function
        End If
    Next s
    If Not createIfMissing Then
        Set GetConfigSheet = Nothing
        Exit Function
    End If
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets.Add(Before:=ThisWorkbook.Sheets(1))
    ws.Name    = CONFIG_SHEET
    ws.Visible = xlSheetVeryHidden
    ws.Cells(1, 1).Value = "ServerUrl":   ws.Cells(1, 2).Value = DEFAULT_URL
    ws.Cells(2, 1).Value = "API_KEY":     ws.Cells(2, 2).Value = ""
    ws.Cells(3, 1).Value = "LastRefresh": ws.Cells(3, 2).Value = ""
    ws.Cells(4, 1).Value = "Mailbox":     ws.Cells(4, 2).Value = ""
    ws.Columns("A:B").AutoFit
    Set GetConfigSheet = ws
End Function
