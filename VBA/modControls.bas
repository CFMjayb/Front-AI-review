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

' Called from ThisWorkbook's Workbook_Open event (inserted at build time by
' create_triage_workbook.py — VBA's ThisWorkbook is a special object module,
' code is inserted into it directly, not imported as a .bas file). Lets a
' single static, unchanging .xlsm be emailed every day and still show that
' day's real data the moment it's opened — no server-side regeneration
' needed, since Refresh already pulls live from the API on every call.
' Deliberately silent (no completion MsgBox) — the whole point is that
' opening the file just works, with nothing to click through first.
Public Sub AutoRefreshOnOpen()
    On Error Resume Next
    modTriage.RefreshTriage
    modSenderRules.RefreshSenderRules
    modGuidance.RefreshGuidance
    ThisWorkbook.Sheets("Triage").Activate
    On Error GoTo 0
End Sub
