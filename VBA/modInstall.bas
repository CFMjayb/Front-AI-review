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

    EnsureSheet "Triage",       RGB(44, 95, 138)
    EnsureSheet "Sender Rules", RGB(192, 90, 0)
    EnsureSheet "Guidance",     RGB(26, 107, 58)

    Dim wsTriage As Worksheet: Set wsTriage = ThisWorkbook.Sheets("Triage")
    Dim wsSR     As Worksheet: Set wsSR     = ThisWorkbook.Sheets("Sender Rules")
    Dim wsGuid   As Worksheet: Set wsGuid   = ThisWorkbook.Sheets("Guidance")

    ' Triage sheet buttons (positioned top-right, above data columns)
    AddButton wsTriage, "Refresh Triage",       "modTriage.RefreshTriage",        630, 2, 130, 20
    AddButton wsTriage, "Upload for Processing", "modTriage.UploadForProcessing", 770, 2, 150, 20
    AddButton wsTriage, "Send Briefing",        "modBriefing.SendBriefing",       930, 2, 110, 20

    ' Sender Rules sheet buttons
    AddButton wsSR, "Refresh",  "modSenderRules.RefreshSenderRules", 480, 2, 100, 20
    AddButton wsSR, "Save",     "modSenderRules.SaveSenderRules",    590, 2,  80, 20

    ' Guidance sheet buttons
    AddButton wsGuid, "Refresh", "modGuidance.RefreshGuidance",  630, 2, 100, 20
    AddButton wsGuid, "Save",    "modGuidance.SaveGuidance",     740, 2,  80, 20

    wsTriage.Activate

    Application.ScreenUpdating = True
    If Not SuppressCompletionMsgBox Then
        MsgBox "Workbook ready." & vbCrLf & vbCrLf & _
               "Click 'Refresh Triage' to load your loops." & vbCrLf & _
               "Click 'Upload for Processing' once you've filled in Triage Actions." & vbCrLf & _
               "Click 'Send Briefing' to send yourself a briefing now.", _
               vbInformation, "CoS Triage Workbook"
    End If
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
