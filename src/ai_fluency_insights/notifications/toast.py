"""Native Windows toast notifications via PowerShell/WinRT (no dependencies)."""

import os
import subprocess


def send_windows_toast(title, message):
    """Fire a native Windows toast via the built-in WinRT API through
    powershell.exe — no pip dependency, no module install, matches how
    Restart-Insights.ps1 already shells out to PowerShell."""
    if os.name != "nt":
        return
    ps_script = (
        '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
        'ContentType=WindowsRuntime] > $null;'
        '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, '
        'ContentType=WindowsRuntime] > $null;'
        '$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('
        '[Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
        '$x = $t.GetElementsByTagName("text");'
        '$x.Item(0).AppendChild($t.CreateTextNode($env:AFI_TOAST_TITLE)) > $null;'
        '$x.Item(1).AppendChild($t.CreateTextNode($env:AFI_TOAST_MSG)) > $null;'
        '$n = [Windows.UI.Notifications.ToastNotification]::new($t);'
        '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('
        '"AI Fluency Insights").Show($n)'
    )
    env = dict(os.environ, AFI_TOAST_TITLE=title, AFI_TOAST_MSG=message)
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", ps_script],
            env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
