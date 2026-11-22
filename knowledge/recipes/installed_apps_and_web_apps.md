---
id: installed_apps_and_web_apps
title: Store apps and installed web apps (WhatsApp, Facebook, Teams, Instagram…)
triggers: [whatsapp, facebook, messenger, instagram, teams, outlook, telegram, linkedin, netflix, prime video, pwa, web app, chat]
---
Many Windows apps are not ordinary programs: WhatsApp, Teams, Instagram and the new
Outlook are Store apps, and Facebook, YouTube or Messenger are often "installed web apps"
that run inside Edge or Chrome but have their own window and Start-menu entry.

- Open them by name: `launch whatsapp`, `launch facebook`. Servo starts them the way the
  Start menu does and finds their window by app identity.
- Use the website only when the user asks for the browser ("open facebook in dia") or the
  Windows context says the app is NOT installed.
- In the screen description such a window is labelled "installed web app" or "Store app".
  It is not a browser: don't press ctrl+t, don't type URLs; use the app's own controls
  (click its search box by its listed label, then type).
- Chats: search for the contact in the app's search box, click the matching chat, click
  the message box, then type. Sending a message is shown to the user for approval; stop
  after typing if the user only asked to draft.
- `close facebook` closes only that app's window, not the browser.
