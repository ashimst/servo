---
id: files_and_folders
title: Find, open and inspect files and folders
triggers: [folder, file, downloads, documents, desktop, find a file, explorer, install, setup, image, photo, pdf, vs code, open a folder]
---
- See what's in a folder: `list folder downloads` (also desktop, documents, or a full path).
- Find a folder by name: `run command "Get-ChildItem -Path $HOME -Directory -Recurse -Depth 4 -Filter '*<name>*' -ErrorAction SilentlyContinue | Select-Object -First 5 FullName"`
- Open a file or folder with its default app: `run command "Start-Process '<full path>'"`
- Open a folder in VS Code: `run command "code '<full path>'"`
- Running an installer (.exe or .msi) is the user's decision: open the folder in Explorer and let them double-click it, unless they explicitly asked you to run it.
