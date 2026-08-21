Attribute VB_Name = "modTriage"
Option Explicit

Private Const SHEET_NAME    As String = "Triage"

' Column indices (1-based) — match COLS in cos_triage_export.py
Private Const COL_NUM       As Long = 1
Private Const COL_URGENCY   As Long = 2
Private Const COL_DIR       As Long = 3
Private Const COL_ACTTYPE   As Long = 4
Private Const COL_COUNTER   As Long = 5
Private Const COL_SUMMARY   As Long = 6
Private Const COL_CATEGORY  As Long = 7
Private Const COL_AGE       As Long = 8
Private Const COL_DUE       As Long = 9
Private Const COL_EMAILDATE As Long = 10
Private Const COL_SENTIMENT As Long = 11
Private Const COL_LINK      As Long = 12
Private Const COL_ACTION    As Long = 13
Private Const COL_NOTES     As Long = 14
Private Const COL_ID        As Long = 15
Private Const TOTAL_COLS    As Long = 15


Private Function RowFillColor(urgency As String, direction As String, _
                               dirLabel As String, rowType As String) As Long
    If rowType = "divider"  Then RowFillColor = RGB(208, 216, 228): Exit Function
    If rowType = "deferred" Then RowFillColor = RGB(227, 242, 253): Exit Function
    If dirLabel = "FYI"     Then RowFillColor = RGB(245, 245, 245): Exit Function
    Select Case LCase(urgency)
        Case "urgent"
            RowFillColor = IIf(direction = "i_owe", RGB(255, 208, 208), RGB(255, 234, 208))
        Case "high"
            RowFillColor = IIf(direction = "i_owe", RGB(255, 228, 228), RGB(255, 244, 224))
        Case "normal"
            RowFillColor = IIf(direction = "i_owe", RGB(255, 240, 240), RGB(255, 255, 240))
        Case Else
            RowFillColor = IIf(direction = "i_owe", RGB(248, 248, 248), RGB(250, 250, 250))
    End Select
End Function


Private Function IsoToExcelDate(s As String) As Variant
    If Len(s) < 10 Then IsoToExcelDate = "": Exit Function
    On Error Resume Next
    IsoToExcelDate = DateSerial(CInt(Left(s, 4)), CInt(Mid(s, 6, 2)), CInt(Right(s, 2)))
    On Error GoTo 0
End Function


Private Sub WriteHeader(ws As Worksheet)
    Dim headers As Variant
    headers = Array("#", "Urgency", "Dir", "Action Type", "Counterparty", "Summary", _
                    "Category", "Age", "Due", "Email Date", "Sentiment", "Link", _
                    "Triage Action", "Notes", "_id")
    Dim widths As Variant
    widths = Array(5, 9, 9, 12, 22, 55, 13, 6, 11, 11, 11, 10, 22, 35, 1)

    Dim i As Long
    For i = 0 To 14
        With ws.Cells(1, i + 1)
            .Value = headers(i)
            .Font.Bold = True
            .Font.Color = RGB(255, 255, 255)
            .Font.Size = 11
            .Interior.Color = RGB(44, 95, 138)
            .HorizontalAlignment = xlCenter
            .VerticalAlignment = xlCenter
        End With
        ws.Columns(i + 1).ColumnWidth = widths(i)
    Next i
    ws.Rows(1).RowHeight = 20
    ws.Columns(COL_ID).Hidden = True
End Sub


Public Sub RefreshTriage()
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = ThisWorkbook.Sheets(SHEET_NAME)
    On Error GoTo 0
    If ws Is Nothing Then MsgBox "Triage sheet not found.", vbCritical, "Error": Exit Sub

    Application.ScreenUpdating = False
    Application.StatusBar = "Fetching loops from server..."

    ' Mailbox scoping: a workbook built for one person/mailbox only ever shows
    ' that mailbox's loops. Blank Mailbox config = unscoped (every mailbox mixed).
    Dim mb As String: mb = GetConfig("Mailbox")
    Dim getPath As String: getPath = "/api/cos/loops"
    If mb <> "" Then getPath = getPath & "?mailbox=" & mb

    Dim resp As String
    resp = HttpGet(getPath)
    If resp = "" Then
        Application.ScreenUpdating = True
        Application.StatusBar = False
        Exit Sub
    End If

    ' Remove any active filter before clearing rows
    If ws.AutoFilterMode Then ws.AutoFilterMode = False

    ' Clear data rows
    Dim lastClear As Long
    lastClear = ws.Cells(ws.Rows.Count, COL_ID).End(xlUp).Row
    If lastClear >= 2 Then ws.Rows("2:" & lastClear).Delete Shift:=xlUp

    WriteHeader ws

    Dim lines() As String
    lines = Split(Replace(resp, Chr(13), ""), Chr(10))
    ' lines(0) = TSV header row from server — skip it
    ' TSV cols: id(0) num(1) row_type(2) urgency(3) direction(4) dir_label(5)
    '           action_type(6) counterparty(7) summary(8) category(9) age_days(10)
    '           due_at(11) source_date(12) sentiment_display(13) source_link(14)

    Dim rowIdx As Long: rowIdx = 2
    Dim i As Long
    For i = 1 To UBound(lines)
        Dim line As String: line = lines(i)
        If Trim(line) = "" Then GoTo NextLine

        Dim f() As String: f = Split(line, Chr(9))
        Dim fc As Long:    fc = UBound(f) + 1

        Dim fId       As String: fId       = IIf(fc > 0,  f(0),  "")
        Dim fNum      As String: fNum      = IIf(fc > 1,  f(1),  "")
        Dim fRowType  As String: fRowType  = IIf(fc > 2,  f(2),  "active")
        Dim fUrgency  As String: fUrgency  = IIf(fc > 3,  f(3),  "normal")
        Dim fDir      As String: fDir      = IIf(fc > 4,  f(4),  "owed_to_me")
        Dim fDirLabel As String: fDirLabel = IIf(fc > 5,  f(5),  "")
        Dim fActType  As String: fActType  = IIf(fc > 6,  f(6),  "")
        Dim fCounter  As String: fCounter  = IIf(fc > 7,  f(7),  "")
        Dim fSummary  As String: fSummary  = IIf(fc > 8,  f(8),  "")
        Dim fCategory As String: fCategory = IIf(fc > 9,  f(9),  "")
        Dim fAge      As String: fAge      = IIf(fc > 10, f(10), "")
        Dim fDue      As String: fDue      = IIf(fc > 11, f(11), "")
        Dim fEmailDt  As String: fEmailDt  = IIf(fc > 12, f(12), "")
        Dim fSentim   As String: fSentim   = IIf(fc > 13, f(13), "")
        Dim fLink     As String: fLink     = IIf(fc > 14, f(14), "")

        Dim fillColor As Long
        fillColor = RowFillColor(fUrgency, fDir, fDirLabel, fRowType)

        If fRowType = "divider" Then
            ws.Cells(rowIdx, 1).Value = "Deferred  —  Review Later"
            With ws.Range(ws.Cells(rowIdx, 1), ws.Cells(rowIdx, TOTAL_COLS))
                .Interior.Color = fillColor
                .Font.Bold      = True
                .Font.Size      = 10
                .Font.Color     = RGB(85, 85, 85)
            End With
            ws.Rows(rowIdx).RowHeight = 16
        Else
            With ws
                ' Values
                .Cells(rowIdx, COL_NUM).Value      = IIf(IsNumeric(fNum), CLng(fNum), "")
                .Cells(rowIdx, COL_URGENCY).Value  = fUrgency
                .Cells(rowIdx, COL_DIR).Value      = fDirLabel
                .Cells(rowIdx, COL_ACTTYPE).Value  = fActType
                .Cells(rowIdx, COL_COUNTER).Value  = fCounter
                .Cells(rowIdx, COL_SUMMARY).Value  = fSummary
                .Cells(rowIdx, COL_CATEGORY).Value = fCategory
                .Cells(rowIdx, COL_SENTIMENT).Value = fSentim
                .Cells(rowIdx, COL_ACTION).Value   = ""
                .Cells(rowIdx, COL_NOTES).Value    = ""
                .Cells(rowIdx, COL_ID).Value       = fId

                ' Age
                If IsNumeric(fAge) Then
                    .Cells(rowIdx, COL_AGE).Value = CLng(fAge)
                    If CLng(fAge) >= 30 Then
                        .Cells(rowIdx, COL_AGE).Font.Color = RGB(204, 0, 0)
                        .Cells(rowIdx, COL_AGE).Font.Bold  = True
                    ElseIf CLng(fAge) >= 14 Then
                        .Cells(rowIdx, COL_AGE).Font.Color = RGB(204, 102, 0)
                        .Cells(rowIdx, COL_AGE).Font.Bold  = True
                    End If
                End If

                ' Dates
                Dim dueVal As Variant: dueVal = IsoToExcelDate(fDue)
                If Not IsEmpty(dueVal) And dueVal <> "" Then
                    .Cells(rowIdx, COL_DUE).Value         = dueVal
                    .Cells(rowIdx, COL_DUE).NumberFormat  = "MM/DD/YYYY"
                End If
                Dim emDtVal As Variant: emDtVal = IsoToExcelDate(fEmailDt)
                If Not IsEmpty(emDtVal) And emDtVal <> "" Then
                    .Cells(rowIdx, COL_EMAILDATE).Value        = emDtVal
                    .Cells(rowIdx, COL_EMAILDATE).NumberFormat = "MM/DD/YYYY"
                End If

                ' Hyperlink
                If fLink <> "" Then
                    .Hyperlinks.Add Anchor:=.Cells(rowIdx, COL_LINK), _
                        Address:=fLink, TextToDisplay:="open"
                    .Cells(rowIdx, COL_LINK).Font.Color     = RGB(5, 99, 193)
                    .Cells(rowIdx, COL_LINK).Font.Underline = xlUnderlineStyleSingle
                End If

                ' Row fill
                Dim rng As Range
                Set rng = .Range(.Cells(rowIdx, 1), .Cells(rowIdx, TOTAL_COLS))
                rng.Interior.Color       = fillColor
                rng.VerticalAlignment    = xlTop

                ' Summary wraps
                .Cells(rowIdx, COL_SUMMARY).WrapText = True
                .Rows(rowIdx).RowHeight = 40

                ' Urgency bold
                If fUrgency = "urgent" Or fUrgency = "high" Then
                    .Cells(rowIdx, COL_URGENCY).Font.Bold = True
                End If
            End With
        End If

        rowIdx = rowIdx + 1
        If rowIdx Mod 25 = 0 Then
            Application.StatusBar = "Loading... " & (rowIdx - 2) & " rows"
        End If
NextLine:
    Next i

    ' Data validation dropdown on Triage Action column — fetched live from the
    ' server (single source of truth: cos_triage_export._triage_action_list)
    ' instead of a hardcoded copy. A hardcoded copy here is exactly how this
    ' dropdown went two months without delegate actions before 2026-08-21 —
    ' see feedback_untracked_parallel_implementation. Falls back to a safe
    ' baseline list only if the fetch itself fails, so a transient network
    ' hiccup doesn't leave the dropdown empty.
    Dim actionList As String
    actionList = HttpGet("/api/cos/triage/actions")
    If actionList = "" Then
        actionList = "done,drop,exclude,subscribe,fyi,defer," & _
                     "snooze 1d,snooze 3d,snooze 1w,snooze 2w,snooze 1m"
    End If

    Dim lastDataRow As Long: lastDataRow = rowIdx - 1
    If lastDataRow >= 2 Then
        On Error Resume Next
        With ws.Range(ws.Cells(2, COL_ACTION), ws.Cells(lastDataRow, COL_ACTION)).Validation
            .Delete
            .Add Type:=xlValidateList, AlertStyle:=xlValidAlertInformation, _
                 Formula1:="""" & actionList & """"
            .InCellDropdown = True
            .IgnoreBlank    = True
            .ShowError      = False
        End With
        On Error GoTo 0
    End If

    ' Auto-filter + freeze
    ws.Activate
    If Not ws.AutoFilterMode Then ws.Range("A1").AutoFilter
    ActiveWindow.FreezePanes = False
    ws.Range("A2").Select
    ActiveWindow.FreezePanes = True

    SetConfig "LastRefresh", CStr(Now())
    Application.ScreenUpdating = True
    Application.StatusBar = (rowIdx - 2) & " rows loaded  |  Last refresh: " & Format(Now(), "h:mm AM/PM")
End Sub


' Replaces the old row-by-row SaveTriage (which POSTed one JSON call per row
' to a live endpoint that had quietly drifted out of sync with the real
' import logic — see feedback_untracked_parallel_implementation, 2026-08-21).
' This saves a copy of the whole workbook and uploads it whole; the server
' runs the same process_triage_workbook() the CLI import path uses, so there
' is exactly one implementation of what each action does, not two.
Public Sub UploadForProcessing()
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = ThisWorkbook.Sheets(SHEET_NAME)
    On Error GoTo 0
    If ws Is Nothing Then Exit Sub

    Dim lastRow As Long
    lastRow = ws.Cells(ws.Rows.Count, COL_ID).End(xlUp).Row
    If lastRow < 2 Then
        MsgBox "No data. Click Refresh first.", vbInformation, "Upload for Processing"
        Exit Sub
    End If

    ' Pre-scan: count rows with actions, same check the old button made.
    Dim actionCount As Long: actionCount = 0
    Dim i As Long
    For i = 2 To lastRow
        If Trim(CStr(ws.Cells(i, COL_ID).Value)) <> "" And _
           Trim(LCase(CStr(ws.Cells(i, COL_ACTION).Value))) <> "" Then
            actionCount = actionCount + 1
        End If
    Next i

    If actionCount = 0 Then
        MsgBox "No Triage Actions filled in. Nothing to upload.", vbInformation, "Upload for Processing"
        Exit Sub
    End If

    If MsgBox(actionCount & " action(s) to upload. Proceed?", _
              vbYesNo + vbQuestion, "Upload for Processing") = vbNo Then
        Exit Sub
    End If

    Application.ScreenUpdating = False
    Application.StatusBar = "Saving a copy to upload..."

    ' SaveCopyAs, not Save/SaveAs — captures whatever is currently on screen
    ' (including unsaved edits) into a standalone file without touching
    ' ThisWorkbook's own path or triggering a format dialog. The VBA-native
    ' equivalent of the SaveAs-via-temp discipline in feedback_excel_com_vba_save.md.
    Dim tempPath As String
    tempPath = Environ("TEMP") & "\CoS_Upload_" & Format(Now(), "yyyymmdd_hhmmss") & ".xlsm"
    On Error GoTo SaveErr
    ThisWorkbook.SaveCopyAs tempPath
    On Error GoTo 0

    Dim fnum As Integer: fnum = FreeFile
    Dim fileBytes() As Byte
    On Error GoTo ReadErr
    Open tempPath For Binary Access Read As #fnum
    If LOF(fnum) > 0 Then
        ReDim fileBytes(1 To LOF(fnum))
        Get #fnum, , fileBytes
    End If
    Close #fnum
    On Error GoTo 0

    Application.StatusBar = "Uploading " & actionCount & " action(s)..."
    Dim resp As String
    resp = HttpPostBytes("/api/cos/triage/upload", fileBytes, _
        "application/vnd.ms-excel.sheet.macroEnabled.12")

    ' The temp file was only ever a transport copy — clean it up regardless
    ' of outcome. If processing failed after the server stored it, its own
    ' copy stays in the bucket for diagnosis; this local one has no further use.
    On Error Resume Next
    Kill tempPath
    On Error GoTo 0

    Application.ScreenUpdating = True
    Application.StatusBar = False

    If resp = "" Then
        MsgBox "Upload failed — see the error above. Nothing was changed; " & _
               "your Triage Actions are still filled in here, safe to retry.", _
               vbExclamation, "Upload Failed"
        Exit Sub
    End If

    Dim doneN As Long, dropN As Long, snoozeN As Long, errN As Long
    doneN   = JsonGetNum(resp, "done")
    dropN   = JsonGetNum(resp, "dropped")
    snoozeN = JsonGetNum(resp, "snoozed")
    errN    = JsonGetNum(resp, "errored")

    Dim msg As String
    msg = "Done: " & doneN & "   Dropped: " & dropN & "   Snoozed: " & snoozeN
    If errN > 0 Then
        msg = msg & vbCrLf & errN & " row(s) had errors — the file was kept " & _
              "on the server for diagnosis instead of being deleted."
    End If
    MsgBox msg, vbInformation, "Upload Complete"

    ' The server is now the source of truth for what's left — always refresh
    ' rather than trying to reconcile row-by-row locally.
    Call RefreshTriage
    Exit Sub

SaveErr:
    Application.ScreenUpdating = True
    Application.StatusBar = False
    MsgBox "Could not save a copy to upload: " & Err.Description, vbCritical, "Upload Failed"
    Exit Sub

ReadErr:
    Application.ScreenUpdating = True
    Application.StatusBar = False
    On Error Resume Next
    Close #fnum
    Kill tempPath
    On Error GoTo 0
    MsgBox "Could not read the saved copy for upload: " & Err.Description, vbCritical, "Upload Failed"
End Sub
