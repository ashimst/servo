---
id: edge_app_mode_mic
title: Edge App Mode Microphone Permission Suppression
app: msedge
scope: servo-internal
surface: Electron
symptom: Microphone recording aborts immediately upon launch in standalone Edge app mode without prompting the user.
root_cause: In standalone app mode (--app=http://...), Edge hides the omnibar and suppresses standard permission popups, causing getUserMedia to reject with NotAllowedError immediately.
mitigation: Launch Edge with --use-fake-ui-for-media-stream and pre-populate media_stream_mic setting in Preferences JSON.
tags: [msedge, microphone, permissions, app_mode, audio]
related_nodes: [surface_electron, bridge_copilot_app, primitive_window_focus]
---

## Description
When Microsoft Edge is executed with `--app=http://127.0.0.1:PORT`, it runs in an application shell mode devoid of standard browser navigation controls. If the loaded web application calls `navigator.mediaDevices.getUserMedia({ audio: true })`, Chromium attempts to display a permission infobar/bubble. Because the omnibar anchor is absent, Chromium immediately dismisses the prompt, causing the promise to reject with `NotAllowedError`.

## Mitigation Playbook
1. Always pass `--use-fake-ui-for-media-stream` on the command line when launching Edge in `--app` mode. This flag instructs Chromium to auto-approve requests for the real default hardware microphone without displaying a UI prompt.
2. In the isolated profile `Default/Preferences` file, write:
   ```json
   "profile": {
     "content_settings": {
       "exceptions": {
         "media_stream_mic": {
           "http://127.0.0.1:*,*": { "setting": 1 },
           "http://localhost:*,*": { "setting": 1 }
         }
       }
     },
     "default_content_setting_values": {
       "media_stream_mic": 1
     }
   }
   ```
3. Record directly via Web Audio `AudioContext` and convert to 16 kHz mono 16-bit PCM WAV in browser memory before sending to Cactus Whistle backend.
