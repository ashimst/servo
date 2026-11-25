---
id: brightness
title: Screen brightness (laptop displays)
triggers: [brightness, brighter, dimmer, dim the screen, screen light]
---
Commands are the code agent's steps: if you are another agent, set "next_agent" to "code".
There is no key Servo can press for brightness; use one PowerShell command. Commands already run inside PowerShell, so never wrap them in `powershell -command`.
- Read: `run command "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"`
- Set to N (0-100): `run command "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods).WmiSetBrightness(1, N)"`
"Increase a bit" = read first, then set to current + 20 (max 100); "decrease a bit" = current - 20 (min 10).
External monitors usually don't support this; if the command errors, say so in "reply".
