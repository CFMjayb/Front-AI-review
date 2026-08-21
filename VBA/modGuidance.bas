Attribute VB_Name = "modGuidance"
Option Explicit

Private Const SHEET_NAME As String = "Guidance"

' Column indices (1-based)
Private Const COL_KEY    As Long = 1
Private Const COL_SCOPE  As Long = 2
Private Const COL_BODY   As Long = 3
Private Const COL_ACTIVE As Long = 4
Private Const COL_DEL    As Long = 5
Private Const TOTAL_COLS As Long = 5


Private Sub WriteHeader(ws As Worksheet)
    Dim headers As Variant
    headers = Array("Key", "Scope", "Body", "Active", "_delete")
    Dim widths As Variant
    widths = Array(18, 20, 65, 8, 8)
    Dim i As Long
    For i = 0 To 4
        With ws.Cells(1, i + 1)
            .Value = headers(i)
            .Font.Bold = True
            .Font.Color = RGB(255, 255, 255)
            .Font.Size = 11
            .Interior.Color = RGB(26, 107, 58)
            .HorizontalAlignment = xlCenter
            .VerticalAlignment = xlCenter
        End With
        ws.Columns(i + 1).ColumnWidth = widths(i)
    Next i
    ws.Rows(1).RowHeight = 20
End Sub


Public Sub RefreshGuidance()
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = ThisWorkbook.Sheets(SHEET_NAME)
    On Error GoTo 0
    If ws Is Nothing Then MsgBox "Guidance sheet not found.", vbCritical, "Error": Exit Sub

    Application.ScreenUpdating = False
    Application.StatusBar = "Fetching guidance..."

    Dim resp As String
    resp = HttpGet("/api/cos/guidance")
    If resp = "" Then
        Application.ScreenUpdating = True
        Application.StatusBar = False
        Exit Sub
    End If

    If ws.AutoFilterMode Then ws.AutoFilterMode = False
    Dim lastClear As Long
    lastClear = ws.Cells(ws.Rows.Count, COL_KEY).End(xlUp).Row
    If lastClear >= 2 Then ws.Rows("2:" & lastClear).Delete Shift:=xlUp

    WriteHeader ws

    Dim lines() As String
    lines = Split(Replace(resp, Chr(13), ""), Chr(10))
    ' TSV cols: key(0) scope(1) body(2) active(3)

    Dim rowFill As Long: rowFill = RGB(240, 255, 244)
    Dim rowIdx  As Long: rowIdx = 2
    Dim i As Long
    For i = 1 To UBound(lines)
        Dim line As String: line = lines(i)
        If Trim(line) = "" Then GoTo NextLine

        Dim f() As String: f = Split(line, Chr(9))
        Dim fc As Long:    fc = UBound(f) + 1

        Dim r As Range
        Set r = ws.Range(ws.Cells(rowIdx, 1), ws.Cells(rowIdx, TOTAL_COLS))
        r.Interior.Color    = rowFill
        r.VerticalAlignment = xlTop

        ws.Cells(rowIdx, COL_KEY).Value    = IIf(fc > 0, f(0), "")
        ws.Cells(rowIdx, COL_SCOPE).Value  = IIf(fc > 1, f(1), "")
        ws.Cells(rowIdx, COL_BODY).Value   = IIf(fc > 2, f(2), "")
        ws.Cells(rowIdx, COL_ACTIVE).Value = IIf(fc > 3, f(3), "yes")
        ws.Cells(rowIdx, COL_DEL).Value    = ""

        ws.Cells(rowIdx, COL_BODY).WrapText = True
        ws.Rows(rowIdx).RowHeight = 40

        rowIdx = rowIdx + 1
NextLine:
    Next i

    ' Active dropdown
    If rowIdx > 2 Then
        On Error Resume Next
        With ws.Range(ws.Cells(2, COL_ACTIVE), ws.Cells(rowIdx - 1, COL_ACTIVE)).Validation
            .Delete
            .Add Type:=xlValidateList, Formula1:="""yes,no"""
            .InCellDropdown = True
            .IgnoreBlank = True
        End With
        On Error GoTo 0
    End If

    ws.Activate
    If Not ws.AutoFilterMode Then ws.Range("A1").AutoFilter
    ActiveWindow.FreezePanes = False
    ws.Range("A2").Select
    ActiveWindow.FreezePanes = True

    Application.ScreenUpdating = True
    Application.StatusBar = (rowIdx - 2) & " guidance items loaded"
End Sub


Public Sub SaveGuidance()
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = ThisWorkbook.Sheets(SHEET_NAME)
    On Error GoTo 0
    If ws Is Nothing Then Exit Sub

    Dim lastRow As Long
    lastRow = ws.Cells(ws.Rows.Count, COL_KEY).End(xlUp).Row
    If lastRow < 2 Then
        MsgBox "No guidance to save.", vbInformation, "Save Guidance"
        Exit Sub
    End If

    Dim parts() As String
    ReDim parts(0 To lastRow - 2)
    Dim count As Long: count = 0

    Dim i As Long
    For i = 2 To lastRow
        Dim key As String: key = Trim(CStr(ws.Cells(i, COL_KEY).Value))
        If key = "" Then GoTo NextRow

        parts(count) = "{""key"":"""    & JsonEscape(key) & """," & _
                       """scope"":"""   & JsonEscape(Trim(CStr(ws.Cells(i, COL_SCOPE).Value))) & """," & _
                       """body"":"""    & JsonEscape(Trim(CStr(ws.Cells(i, COL_BODY).Value))) & """," & _
                       """active"":"""  & JsonEscape(Trim(CStr(ws.Cells(i, COL_ACTIVE).Value))) & """," & _
                       """_delete"":""" & JsonEscape(Trim(LCase(CStr(ws.Cells(i, COL_DEL).Value)))) & """}"
        count = count + 1
NextRow:
    Next i

    If count = 0 Then MsgBox "No guidance to save.", vbInformation, "Save": Exit Sub

    ReDim Preserve parts(0 To count - 1)
    Dim jBody As String
    jBody = "[" & Join(parts, ",") & "]"

    Application.StatusBar = "Saving guidance..."
    Dim resp As String: resp = HttpPost("/api/cos/guidance-save", jBody)

    If resp <> "" Then
        Application.StatusBar = "Guidance saved."
        If MsgBox("Saved. Refresh to confirm?", vbYesNo + vbQuestion, "Save Complete") = vbYes Then
            Call RefreshGuidance
        End If
    Else
        Application.StatusBar = "Save failed — check error message."
    End If
End Sub
