Attribute VB_Name = "modApi"
Option Explicit


Public Function HttpGet(path As String) As String
    Dim baseUrl As String: baseUrl = GetConfig("ServerUrl")
    Dim apiKey  As String: apiKey  = GetConfig("API_KEY")
    If apiKey = "" Then
        MsgBox "API Key not configured. Recreate the workbook to set it.", vbExclamation, "Not Configured"
        HttpGet = "": Exit Function
    End If
    Dim req As Object
    Set req = CreateObject("WinHttp.WinHttpRequest.5.1")
    req.Open "GET", baseUrl & path, False
    req.SetRequestHeader "X-API-Key", apiKey
    req.SetTimeouts 10000, 30000, 90000, 90000
    On Error GoTo ErrHandler
    req.Send
    If req.Status >= 400 Then
        MsgBox "HTTP " & req.Status & " from " & path & vbCrLf & Left(req.ResponseText, 300), _
               vbExclamation, "API Error"
        HttpGet = "": Exit Function
    End If
    HttpGet = req.ResponseText
    Exit Function
ErrHandler:
    MsgBox "Connection error: " & Err.Description, vbCritical, "Network Error"
    HttpGet = ""
End Function


Public Function HttpPost(path As String, jsonBody As String) As String
    Dim baseUrl As String: baseUrl = GetConfig("ServerUrl")
    Dim apiKey  As String: apiKey  = GetConfig("API_KEY")
    If apiKey = "" Then
        MsgBox "API Key not configured.", vbExclamation, "Not Configured"
        HttpPost = "": Exit Function
    End If
    Dim req As Object
    Set req = CreateObject("WinHttp.WinHttpRequest.5.1")
    req.Open "POST", baseUrl & path, False
    req.SetRequestHeader "X-API-Key", apiKey
    req.SetRequestHeader "Content-Type", "application/json"
    req.SetTimeouts 10000, 30000, 90000, 90000
    On Error GoTo ErrHandler
    req.Send jsonBody
    If req.Status >= 400 Then
        MsgBox "HTTP " & req.Status & vbCrLf & Left(req.ResponseText, 300), vbExclamation, "API Error"
        HttpPost = "": Exit Function
    End If
    HttpPost = req.ResponseText
    Exit Function
ErrHandler:
    MsgBox "Connection error: " & Err.Description, vbCritical, "Network Error"
    HttpPost = ""
End Function


' Binary upload — used by UploadForProcessing to send the saved workbook's
' raw bytes. WinHttpRequest.Send accepts a Byte array directly for a binary
' body; returns a Variant-typed result dict via JSON string, same as HttpPost.
Public Function HttpPostBytes(path As String, data() As Byte, contentType As String) As String
    Dim baseUrl As String: baseUrl = GetConfig("ServerUrl")
    Dim apiKey  As String: apiKey  = GetConfig("API_KEY")
    If apiKey = "" Then
        MsgBox "API Key not configured.", vbExclamation, "Not Configured"
        HttpPostBytes = "": Exit Function
    End If
    Dim req As Object
    Set req = CreateObject("WinHttp.WinHttpRequest.5.1")
    req.Open "POST", baseUrl & path, False
    req.SetRequestHeader "X-API-Key", apiKey
    req.SetRequestHeader "Content-Type", contentType
    ' Uploads can take longer than a JSON round-trip — the server both stores
    ' to GCS and processes every sheet before responding.
    req.SetTimeouts 10000, 30000, 120000, 120000
    On Error GoTo ErrHandler
    req.Send data
    If req.Status >= 400 Then
        MsgBox "HTTP " & req.Status & vbCrLf & Left(req.ResponseText, 500), vbExclamation, "Upload Error"
        HttpPostBytes = "": Exit Function
    End If
    HttpPostBytes = req.ResponseText
    Exit Function
ErrHandler:
    MsgBox "Connection error: " & Err.Description, vbCritical, "Network Error"
    HttpPostBytes = ""
End Function


Public Function JsonEscape(s As String) As String
    s = Replace(s, "\", "\\")
    s = Replace(s, """", "\""")
    s = Replace(s, Chr(10), "\n")
    s = Replace(s, Chr(13), "\r")
    s = Replace(s, Chr(9), "\t")
    JsonEscape = s
End Function


' Parse a simple {"key": 123} JSON response for a numeric field
Public Function JsonGetNum(json As String, key As String) As Long
    Dim pat As String: pat = """" & key & """\s*:\s*(-?\d+)"
    Dim re  As Object: Set re = CreateObject("VBScript.RegExp")
    re.Pattern = pat
    re.IgnoreCase = True
    Dim m As Object: Set m = re.Execute(json)
    If m.Count > 0 Then
        JsonGetNum = CLng(m(0).SubMatches(0))
    Else
        JsonGetNum = 0
    End If
End Function


' Parse a simple {"key": value} JSON response for a string field
Public Function JsonGetStr(json As String, key As String) As String
    Dim pat As String: pat = """" & key & """\s*:\s*""([^""]*)"""
    Dim re  As Object: Set re = CreateObject("VBScript.RegExp")
    re.Pattern = pat
    re.IgnoreCase = True
    Dim m As Object: Set m = re.Execute(json)
    If m.Count > 0 Then
        JsonGetStr = m(0).SubMatches(0)
    Else
        JsonGetStr = ""
    End If
End Function
