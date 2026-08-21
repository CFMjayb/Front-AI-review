Attribute VB_Name = "modSenderRules"
Option Explicit

Private Const SHEET_NAME As String = "Sender Rules"

' Column indices (1-based)
Private Const COL_EMAIL   As Long = 1
Private Const COL_ACTION  As Long = 2
Private Const COL_CAT     As Long = 3
Private Const COL_DIR     As Long = 4
Private Const COL_IMP     As Long = 5
Private Const COL_SUBJ    As Long = 6
Private Const COL_NOTES   As Long = 7
Private Const COL_DEL     As Long = 8
Private Const TOTAL_COLS  As Long = 8

Private Const HDR_COLOR   As Long = &HC05A00   ' Orange


Private Sub WriteHeader(ws As Worksheet)
    Dim headers As Variant
    headers = Array("Email / Domain", "Action", "Category", "Direction", _
                    "Importance", "Subject Pattern", "Notes", "_delete")
    Dim widths As Variant
    widths = Array(28, 14, 14, 13, 11, 22, 35, 8)
    Dim i As Long
    For i = 0 To 7
        With ws.Cells(1, i + 1)
            .Value = headers(i)
            .Font.Bold = True
            .Font.Color = RGB(255, 255, 255)
            .Font.Size = 11
            .Interior.Color = RGB(192, 90, 0)
            .HorizontalAlignment = xlCenter
            .VerticalAlignment = xlCenter
        End With
        ws.Columns(i + 1).ColumnWidth = widths(i)
    Next i
    ws.Rows(1).RowHeight = 20
End Sub


Public Sub RefreshSenderRules()
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = ThisWorkbook.Sheets(SHEET_NAME)
    On Error GoTo 0
    If ws Is Nothing Then MsgBox "Sender Rules sheet not found.", vbCritical, "Error": Exit Sub

    Application.ScreenUpdating = False
    Application.StatusBar = "Fetching sender rules..."

    Dim resp As String
    resp = HttpGet("/api/cos/sender-rules")
    If resp = "" Then
        Application.ScreenUpdating = True
        Application.StatusBar = False
        Exit Sub
    End If

    If ws.AutoFilterMode Then ws.AutoFilterMode = False
    Dim lastClear As Long
    lastClear = ws.Cells(ws.Rows.Count, COL_EMAIL).End(xlUp).Row
    If lastClear >= 2 Then ws.Rows("2:" & lastClear).Delete Shift:=xlUp

    WriteHeader ws

    Dim lines() As String
    lines = Split(Replace(resp, Chr(13), ""), Chr(10))
    ' TSV cols: email(0) action(1) category(2) direction(3) importance(4) subject_pattern(5) notes(6)

    Dim rowFill As Long: rowFill = RGB(255, 248, 240)
    Dim rowIdx  As Long: rowIdx = 2
    Dim i As Long
    For i = 1 To UBound(lines)
        Dim line As String: line = lines(i)
        If Trim(line) = "" Then GoTo NextLine

        Dim f() As String: f = Split(line, Chr(9))
        Dim fc As Long:    fc = UBound(f) + 1

        Dim r As Range
        Set r = ws.Range(ws.Cells(rowIdx, 1), ws.Cells(rowIdx, TOTAL_COLS))
        r.Interior.Color = rowFill
        r.VerticalAlignment = xlTop

        ws.Cells(rowIdx, COL_EMAIL).Value  = IIf(fc > 0, f(0), "")
        ws.Cells(rowIdx, COL_ACTION).Value = IIf(fc > 1, f(1), "")
        ws.Cells(rowIdx, COL_CAT).Value    = IIf(fc > 2, f(2), "")
        ws.Cells(rowIdx, COL_DIR).Value    = IIf(fc > 3, f(3), "")
        ws.Cells(rowIdx, COL_IMP).Value    = IIf(fc > 4, f(4), "")
        ws.Cells(rowIdx, COL_SUBJ).Value   = IIf(fc > 5, f(5), "")
        ws.Cells(rowIdx, COL_NOTES).Value  = IIf(fc > 6, f(6), "")
        ws.Cells(rowIdx, COL_DEL).Value    = ""

        rowIdx = rowIdx + 1
NextLine:
    Next i

    ' Action dropdown
    If rowIdx > 2 Then
        On Error Resume Next
        With ws.Range(ws.Cells(2, COL_ACTION), ws.Cells(rowIdx - 1, COL_ACTION)).Validation
            .Delete
            .Add Type:=xlValidateList, Formula1:="""exclude,fyi,force-category,subscribe"""
            .InCellDropdown = True
            .IgnoreBlank    = True
            .ShowError      = False
        End With
        On Error GoTo 0
    End If

    ws.Activate
    If Not ws.AutoFilterMode Then ws.Range("A1").AutoFilter
    ActiveWindow.FreezePanes = False
    ws.Range("A2").Select
    ActiveWindow.FreezePanes = True

    Application.ScreenUpdating = True
    Application.StatusBar = (rowIdx - 2) & " sender rules loaded"
End Sub


Public Sub SaveSenderRules()
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = ThisWorkbook.Sheets(SHEET_NAME)
    On Error GoTo 0
    If ws Is Nothing Then Exit Sub

    Dim lastRow As Long
    lastRow = ws.Cells(ws.Rows.Count, COL_EMAIL).End(xlUp).Row
    If lastRow < 2 Then
        MsgBox "No rules to save.", vbInformation, "Save Sender Rules"
        Exit Sub
    End If

    ' Build JSON array of all rows
    Dim parts() As String
    ReDim parts(0 To lastRow - 2)
    Dim count As Long: count = 0

    Dim i As Long
    For i = 2 To lastRow
        Dim email As String: email = Trim(CStr(ws.Cells(i, COL_EMAIL).Value))
        If email = "" Then GoTo NextRow

        parts(count) = "{""email"":"""    & JsonEscape(email) & """," & _
                       """action"":"""    & JsonEscape(Trim(CStr(ws.Cells(i, COL_ACTION).Value))) & """," & _
                       """category"":""" & JsonEscape(Trim(CStr(ws.Cells(i, COL_CAT).Value))) & """," & _
                       """direction"":"""  & JsonEscape(Trim(CStr(ws.Cells(i, COL_DIR).Value))) & """," & _
                       """importance"":""" & JsonEscape(Trim(CStr(ws.Cells(i, COL_IMP).Value))) & """," & _
                       """subject_pattern"":""" & JsonEscape(Trim(CStr(ws.Cells(i, COL_SUBJ).Value))) & """," & _
                       """notes"":"""  & JsonEscape(Trim(CStr(ws.Cells(i, COL_NOTES).Value))) & """," & _
                       """_delete"":""" & JsonEscape(Trim(LCase(CStr(ws.Cells(i, COL_DEL).Value)))) & """}"
        count = count + 1
NextRow:
    Next i

    If count = 0 Then MsgBox "No rules to save.", vbInformation, "Save": Exit Sub

    ReDim Preserve parts(0 To count - 1)
    Dim jBody As String
    jBody = "[" & Join(parts, ",") & "]"

    Application.StatusBar = "Saving sender rules..."
    Dim resp As String: resp = HttpPost("/api/cos/sender-rules-save", jBody)

    If resp <> "" Then
        Application.StatusBar = "Sender rules saved."
        If MsgBox("Saved. Refresh to confirm?", vbYesNo + vbQuestion, "Save Complete") = vbYes Then
            Call RefreshSenderRules
        End If
    Else
        Application.StatusBar = "Save failed — check error message."
    End If
End Sub
