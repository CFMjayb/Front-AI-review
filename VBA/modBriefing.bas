Attribute VB_Name = "modBriefing"
Option Explicit

Public Sub SendBriefing()
    Dim msg As String
    msg = "Send a CoS briefing now to jay@cfmins.org?" & vbCrLf & vbCrLf & _
          "This will run the full briefing job (gather loops, call Claude," & vbCrLf & _
          "render, and send email). Takes 30-60 seconds."
    If MsgBox(msg, vbYesNo + vbQuestion, "Send CoS Briefing") <> vbYes Then Exit Sub

    Application.StatusBar = "Sending briefing... (this may take up to 60 seconds)"
    Application.Cursor = xlWait

    Dim resp As String
    resp = HttpPost("/api/cos/briefing", "{}")

    Application.Cursor = xlDefault
    Application.StatusBar = False

    If resp = "" Then Exit Sub   ' HttpPost already showed an error

    Dim subject As String: subject = JsonGetStr(resp, "subject")
    Dim status  As String: status  = JsonGetStr(resp, "status")
    Dim errMsg  As String: errMsg  = JsonGetStr(resp, "error")

    If errMsg <> "" Then
        MsgBox "Briefing failed:" & vbCrLf & errMsg, vbCritical, "Send Briefing"
    Else
        MsgBox "Briefing sent." & vbCrLf & vbCrLf & "Subject: " & subject, _
               vbInformation, "Send Briefing"
    End If
End Sub
