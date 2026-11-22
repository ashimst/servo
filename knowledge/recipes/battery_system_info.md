---
id: battery_system_info
title: Battery, disk, memory, network and other system facts
triggers: [battery, charge, charging, disk space, storage, ram, memory usage, cpu, ip address, wifi name, uptime, windows version, specs]
---
Answer with one read-only command, then put the answer in "reply" with done=true:
- Battery: `run command "Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining, BatteryStatus"` (BatteryStatus 2 = charging)
- Disk: `run command "Get-PSDrive -PSProvider FileSystem | Select-Object Name, Free, Used"`
- Memory: `run command "Get-CimInstance Win32_OperatingSystem | Select-Object FreePhysicalMemory, TotalVisibleMemorySize"`
- IP: `run command "Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias, IPAddress"`
- Wi-Fi: `run command "netsh wlan show interfaces"`
- Windows version: `run command "Get-ComputerInfo -Property OsName, OsVersion"`
Do not open Settings and click around for facts a command can read.
