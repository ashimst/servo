---
id: windows_settings
title: Open a specific Windows Settings page
triggers: [settings, bluetooth, wi-fi, wifi, display settings, sound settings, windows update, default apps, night light, notifications]
---
Jump straight to the page instead of clicking through Settings:
`run command "Start-Process ms-settings:<page>"`, page one of:
bluetooth, network-wifi, display, sound, nightlight, notifications, windowsupdate, defaultapps,
batterysaver, apps-volume, personalization-background, privacy-microphone, about.
Then work on that page next round using the labels on screen.
Toggling Wi-Fi or Bluetooth: open the page, then `click <toggle label>` (e.g. "Bluetooth").
