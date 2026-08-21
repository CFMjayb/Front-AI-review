Attribute VB_Name = "modInstall"
Option Explicit

' Called by create_triage_workbook.py after VBA modules are imported.
' Creates sheets and adds buttons. Does NOT auto-refresh (server may not be up yet).

' The completion MsgBox below requires a human click. Under headless COM
' automation (xl.Visible = False, as the build script uses) nothing can click
' it, so xl.Run("modInstall.RunInstall") hangs forever with no way to recover
' short of killing the Excel process. Same root cause already fixed for the
' 26-125 PG Data Review workbook — set this True before calling RunInstall
' from a script; leave it False (default) for a real interactive run.
Public SuppressCompletionMsgBox As Boolean

' Application.Run can only invoke a Sub/Function, not assign a public
' variable directly — this setter is what create_triage_workbook.py calls.
Public Sub SetSuppressCompletionMsgBox(v As Boolean)
    SuppressCompletionMsgBox = v
End Sub

Public Sub RunInstall()
    Application.ScreenUpdating = False

    ' Ensure Config sheet exists
    GetConfig "ServerUrl"

    EnsureSheet "Controls",     RGB(85, 85, 85)
    EnsureSheet "Triage",       RGB(44, 95, 138)
    EnsureSheet "Sender Rules", RGB(192, 90, 0)
    EnsureSheet "Guidance",     RGB(26, 107, 58)

    Dim wsControls As Worksheet: Set wsControls = ThisWorkbook.Sheets("Controls")
    Dim wsTriage   As Worksheet: Set wsTriage   = ThisWorkbook.Sheets("Triage")

    ' Every button lives on Controls, never on a data sheet — a button
    ' sitting on top of Triage's own columns is exactly what disappeared
    ' under real loaded data (found 2026-08-21). See modControls.bas.
    SetupControlsSheet wsControls

    ' Row 1: Triage actions. Top=45 clears the two header-text rows above
    ' (measured cumulative height ~32px) with a real margin, not a guess.
    AddButton wsControls, "Refresh All",           "modControls.BtnRefreshAll_Click",           10,  45, 130, 26
    AddButton wsControls, "Refresh Triage",        "modControls.BtnRefreshTriage_Click",        150, 45, 130, 26
    AddButton wsControls, "Upload for Processing", "modControls.BtnUploadForProcessing_Click",  290, 45, 160, 26
    AddButton wsControls, "Send Briefing",         "modControls.BtnSendBriefing_Click",         460, 45, 120, 26

    ' Row 2: Sender Rules + Guidance, refresh/save pairs
    AddButton wsControls, "Refresh Sender Rules",  "modControls.BtnRefreshSenderRules_Click",   10,  85, 160, 26
    AddButton wsControls, "Save Sender Rules",     "modControls.BtnSaveSenderRules_Click",      180, 85, 130, 26
    AddButton wsControls, "Refresh Guidance",      "modControls.BtnRefreshGuidance_Click",      320, 85, 140, 26
    AddButton wsControls, "Save Guidance",         "modControls.BtnSaveGuidance_Click",         470, 85, 110, 26

    wsControls.Activate

    Application.ScreenUpdating = True
    If Not SuppressCompletionMsgBox Then
        MsgBox "Workbook ready — start on the Controls tab." & vbCrLf & vbCrLf & _
               "Click 'Refresh All' to load everything, or refresh one section at " & _
               "a time." & vbCrLf & _
               "Fill in Triage Actions on the Triage tab, then come back to " & _
               "Controls and click 'Upload for Processing.'" & vbCrLf & _
               "'Send Briefing' sends yourself a briefing on demand.", _
               vbInformation, "CoS Triage Workbook"
    End If
End Sub


Private Sub SetupControlsSheet(ws As Worksheet)
    ws.Cells(1, 1).Value = "CoS Triage Workbook — Controls"
    ws.Cells(1, 1).Font.Bold = True
    ws.Cells(1, 1).Font.Size = 14
    ws.Cells(2, 1).Value = "Every button lives here. Refresh All pulls Triage + " & _
        "Sender Rules + Guidance in one click; each section also has its own " & _
        "Refresh/Save if you only need one."
    ws.Cells(2, 1).Font.Italic = True
    ws.Columns("A").ColumnWidth = 100
End Sub


Private Sub EnsureSheet(name As String, tabColor As Long)
    Dim s As Object
    For Each s In ThisWorkbook.Sheets
        If s.Name = name Then Exit Sub
    Next s
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
    ws.Name = name
    ws.Tab.Color = tabColor
End Sub


Private Sub AddButton(ws As Worksheet, caption As String, macroName As String, _
                      left As Long, top As Long, width As Long, height As Long)
    ' Remove duplicate if re-running install
    Dim s As Shape
    For Each s In ws.Shapes
        If s.TextFrame2.TextRange.Text = caption Then s.Delete: Exit For
    Next s

    Dim btn As Shape
    Set btn = ws.Shapes.AddShape(msoShapeRoundedRectangle, left, top, width, height)
    With btn
        .Name     = "btn_" & Replace(caption, " ", "_")
        .OnAction = macroName
        .Fill.ForeColor.RGB = RGB(44, 95, 138)
        .Line.Visible       = msoFalse
        .LockAspectRatio    = msoFalse
        .TextFrame.Characters.Text = caption
        With .TextFrame.Characters.Font
            .Color = RGB(255, 255, 255)
            .Bold  = True
            .Size  = 10
        End With
        .TextFrame.HorizontalAlignment = xlHAlignCenter
        .TextFrame.VerticalAlignment   = xlVAlignCenter
        .Placement = xlFreeFloating
    End With
End Sub
