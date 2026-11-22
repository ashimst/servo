---
id: spotify
title: Play music on Spotify (web player)
triggers: [spotify, album, playlist]
---
1. `search spotify for <song or artist>` (opens open.spotify.com/search/... directly).
2. Next round: if the screen shows "Log in" / "Sign up", stop and ask the user to log in; Servo never types passwords.
3. Otherwise `click Play <name>` for the top result (the play button's label starts with "Play").
4. Done when a "Pause" button is listed in the player bar.
If the Spotify desktop app is installed and the user asked for it, `open spotify` instead and use its search box.
