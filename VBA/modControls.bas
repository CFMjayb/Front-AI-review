Attribute VB_Name = "modControls"
Option Explicit

' All workbook buttons live here, on the Controls sheet — never on a data
' sheet itself. A button sitting on top of the Triage sheet's own data
' columns is exactly what caused it to disappear under real loaded data
' (found 2026-08-21). Matches the Controls-tab convention already
' established elsewhere in this codebase (26-121/26-125/26-127/26-129).

Public Sub BtnRefreshAll_Click()
    modTriage.RefreshTriage
    modSenderRules.RefreshSenderRules
    modGuidance.RefreshGuidance
    ThisWorkbook.Sheets("Triage").Activate
    MsgBox "Refresh All complete — Triage, Sender Rules, and Guidance are " & _
           "all up to date.", vbInformation, "Refresh All"
End Sub

Public Sub BtnRefreshTriage_Click()
    modTriage.RefreshTriage
End Sub

Public Sub BtnUploadForProcessing_Click()
    modTriage.UploadForProcessing
End Sub

Public Sub BtnRefreshSenderRules_Click()
    modSenderRules.RefreshSenderRules
End Sub

Public Sub BtnSaveSenderRules_Click()
    modSenderRules.SaveSenderRules
End Sub

Public Sub BtnRefreshGuidance_Click()
    modGuidance.RefreshGuidance
End Sub

Public Sub BtnSaveGuidance_Click()
    modGuidance.SaveGuidance
End Sub

Public Sub BtnSendBriefing_Click()
    modBriefing.SendBriefing
End Sub
