"""Training data for Needle's request and command agents (servo/cognition/intents.py).

Templates x values -> JSONL lines in Cactus's finetune format
({"query", "tools", "answers", "system"}). Each line shows the agent's own tool list (shuffled,
so position carries no signal) and the exact calls, or [] for a request the agent must leave
alone. Sentences in eval_cases.py are dropped, so the eval stays held out.

usage: python needle_model/gen_data.py [out.jsonl] [--seed N]
"""
from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from servo.cognition.intents import COMMAND_SYSTEM, COMMAND_TOOLS, REQUEST_SYSTEM, REQUEST_TOOLS  # noqa: E402
import eval_cases  # noqa: E402

APPS = [
    "notepad", "calculator", "paint", "word", "excel", "powerpoint", "outlook", "spotify", "discord", "slack",
    "teams", "zoom", "steam", "chrome", "firefox", "edge", "file explorer", "task manager", "control panel",
    "photos", "camera", "clock", "snipping tool", "terminal", "powershell", "vs code", "visual studio code",
    "obsidian", "notion", "whatsapp", "telegram", "vlc", "obs studio", "gimp", "audacity", "blender",
    "libreoffice writer", "thunderbird", "brave", "opera", "microsoft store", "sticky notes", "onenote",
    "media player", "calendar", "mail", "maps", "weather", "xbox", "figma", "postman", "pycharm",
]

FOLDERS = [
    "downloads", "documents", "desktop", "pictures", "music", "videos", "C:/Users/me/projects",
    "D:/backup", "C:/temp", "my documents folder", "D:/work", "C:/Projects/servo",
]

FILES = [
    "notes.txt", "todo.txt", "readme.md", "log.txt", "C:/temp/out.log", "D:/work/plan.txt", "ideas.txt",
    "config.json", "shopping list.txt", "journal.txt", "data.csv", "C:/Users/me/report.txt", "summary.txt",
]

DOCS = [
    "report.pdf", "budget.xlsx", "slides.pptx", "resume.docx", "invoice.pdf", "photo.jpg", "contract.docx",
    "C:/Users/me/Downloads/manual.pdf", "taxes 2023.xlsx", "song.mp3", "trip.mp4", "schedule.xlsx",
    "presentation.pptx", "document.pdf", "paper.pdf",
]

SITES = [
    "github.com", "youtube.com", "reddit.com", "wikipedia.org", "gmail.com", "netflix.com", "amazon.com",
    "stackoverflow.com", "bbc.co.uk", "https://example.org", "news.google.com", "twitter.com", "linkedin.com",
    "docs.python.org", "chat.openai.com", "www.nytimes.com", "maps.google.com", "facebook.com",
]

QUERIES = [
    "weather tomorrow", "how to boil an egg", "python list comprehension", "cheap flights to delhi",
    "best laptops 2024", "who won the world cup", "nepal population", "how to fix a leaking tap",
    "chocolate cake recipe", "meaning of serendipity", "time in tokyo", "rust vs go", "nearest pharmacy",
    "how many ounces in a cup", "latest iphone price", "world clock", "distance to moon", "how tall is everest",
]

VIDEOS = [
    "piano tutorial", "jazz for studying", "how to tie a tie", "funny dogs", "minecraft speedrun",
    "react course", "rain sounds", "guitar lessons", "news today", "yoga for beginners", "lofi beats",
    "cat videos", "python full course", "relaxing music",
]

TOPICS = [
    "bluetooth", "display", "wifi", "network", "sound", "time zone", "date and time", "windows update",
    "background", "wallpaper", "default apps", "night light", "notifications", "battery", "storage",
    "mouse", "keyboard", "language", "printers", "privacy", "lock screen", "dark mode", "taskbar",
    "startup apps", "vpn", "airplane mode", "accounts", "accessibility", "camera", "microphone",
]

WRITE_TEXTS = [
    "call the bank", "meeting at 3", "hello world", "pick up the kids", "remember the milk",
    "draft done", "pay rent on friday", "ideas for the trip", "buy milk", "submit invoice",
]


def pick(xs, n):
    return random.sample(xs, min(n, len(xs)))


def call(tool, **args):
    return {"name": tool, "arguments": args}


def request_examples():
    ex = []
    add = lambda q, *calls: ex.append((q, list(calls)))

    # --- Apps vs Windows ---
    for a in pick(APPS, 40):
        for t in pick([
            "open {a}", "launch {a}", "start {a}", "fire up {a}", "can you open {a}", "please open {a}",
            "open {a} for me", "switch to {a}", "bring up {a}", "go to my {a} window", "show me {a}",
            "i need {a}", "get {a} open", "open up {a}", "run {a}", "bring {a} to the front",
            "switch over to {a}", "open the {a} program", "open {a} please", "fire up the {a} app",
        ], 3):
            add(t.format(a=a), call("open_app", name=a))

        for t in pick([
            "close {a}", "quit {a}", "exit {a}", "shut {a}", "kill {a}", "close the {a} window",
            "shut down {a}", "please quit {a}", "quit {a} please", "can you close {a}", "get rid of {a}",
            "end {a}", "close down {a}", "shut down the {a} app", "exit {a} please", "please exit {a}",
            "close {a} please", "shut down {a} please",
        ], 3):
            add(t.format(a=a), call("close_app", name=a))

        for t in pick([
            "minimize {a}", "hide {a}", "minimise the {a} window", "send {a} to the taskbar",
            "hide the {a} window", "minimize the {a} window", "minimise {a}",
        ], 2):
            add(t.format(a=a), call("minimize_window", name=a))

        for t in pick([
            "maximize {a}", "make {a} full screen", "maximise the {a} window", "make the {a} window bigger",
            "make {a} full size", "maximize the {a} window", "expand {a}",
        ], 2):
            add(t.format(a=a), call("maximize_window", name=a))

    # --- Settings ---
    for topic in TOPICS:
        for t in pick([
            "open {t} settings", "take me to {t} settings", "show the {t} settings", "go to {t} settings",
            "i want to change the {t}", "i want to change my {t}", "where are the {t} settings",
            "where do i change {t}", "open settings for {t}", "change my {t}", "{t} settings please",
            "open the {t} page in settings", "take me to the {t} page", "check {t} settings",
            "check for {t}",
        ], 3):
            add(t.format(t=topic), call("open_settings", topic=topic))

    # --- Screen brightness ---
    for p in [10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 100]:
        for t in [
            "set brightness to {p}%", "brightness {p}", "make the screen {p} percent bright",
            "dim the display to {p}%", "change the brightness to {p}", "screen brightness {p} percent",
            "adjust brightness to {p}%", "set display brightness to {p}",
        ]:
            add(t.format(p=p), call("set_brightness", percent=p))

    # --- Volume UP (Dense directional contrast) ---
    vol_up_unmetered = [
        "louder", "make it louder", "turn the volume up", "turn it up", "volume up", "raise the volume",
        "increase the volume", "crank it up", "pump up the volume", "i can't hear, louder please", "too quiet",
        "turn up the sound", "a bit louder", "boost the volume", "louder please", "volume up a little",
        "turn the sound up a lot", "make the audio louder", "turn up audio", "sound is too quiet, raise it",
        "turn up the volume please", "can you make it louder", "increase sound", "raise audio volume",
        "boost sound level", "bump up the volume", "volume higher", "make sound louder", "turn it up please",
    ]
    for q in vol_up_unmetered:
        add(q, call("volume_up"))

    for p in [5, 10, 15, 20, 25, 30, 40, 50]:
        for pat in [
            "volume up {p}%", "turn the volume up by {p} percent", "raise volume {p}%", "louder by {p}%",
            "increase volume by {p}%", "turn up the sound by {p} percent", "bump the volume up {p}%",
        ]:
            add(pat.format(p=p), call("volume_up", percent=p))

    # --- Volume DOWN (Dense directional contrast) ---
    vol_down_unmetered = [
        "quieter", "make it quieter", "turn the volume down", "turn it down", "volume down", "lower the volume",
        "decrease the volume", "too loud", "reduce the volume", "turn down the sound", "a bit quieter",
        "softer please", "quieter please", "volume down a little", "lower the sound a lot", "less loud",
        "make the audio quieter", "turn down audio", "sound is too loud, lower it", "turn down the volume please",
        "can you make it quieter", "decrease sound", "lower audio volume", "soften sound level",
        "volume lower", "make sound quieter", "turn it down please", "tone it down",
    ]
    for q in vol_down_unmetered:
        add(q, call("volume_down"))

    for p in [5, 10, 15, 20, 25, 30, 40, 50]:
        for pat in [
            "volume down {p}%", "turn the volume down by {p} percent", "lower volume {p}%", "quieter by {p}%",
            "decrease volume by {p}%", "turn down the sound by {p} percent", "drop the volume by {p}%",
        ]:
            add(pat.format(p=p), call("volume_down", percent=p))

    # --- Mute vs Unmute ---
    mute_phrases = [
        "mute", "mute the audio", "silence the computer", "mute the volume", "mute everything",
        "turn the sound off", "mute the speakers", "shh, mute it", "silence the sound", "shut up the audio",
        "mute audio now", "cut the sound", "turn audio off", "kill the audio", "put on mute",
        "mute the system sound", "mute laptop", "make it silent", "silence audio please",
    ]
    for q in mute_phrases:
        add(q, call("mute"))
        add(f"please {q}", call("mute"))
        add(f"can you {q}", call("mute"))

    unmute_phrases = [
        "unmute", "unmute the audio", "turn the audio back on", "unmute the speakers", "sound on",
        "bring the sound back", "unmute the volume", "turn sound back on please", "restore the sound",
        "enable audio again", "unmute it", "take off mute", "bring back audio", "audio back on",
        "turn the sound on", "unmute the system audio", "bring audio back", "unmute now",
    ]
    for q in unmute_phrases:
        add(q, call("unmute"))
        add(f"please {q}", call("unmute"))
        add(f"can you {q}", call("unmute"))

    # --- Playback: Pause vs Resume ---
    pause_phrases = [
        "pause", "pause the song", "pause the video", "stop the music", "pause playback", "hit pause",
        "stop the video", "pause spotify music", "pause this track", "pause what's playing", "pause audio",
        "pause the player", "pause media", "pause the current track", "stop playing", "stop the audio",
        "stop the track", "pause the music please", "stop playing music", "halt playback",
    ]
    for q in pause_phrases:
        add(q, call("pause_media"))
        add(f"please {q}", call("pause_media"))
        add(f"can you {q}", call("pause_media"))

    resume_phrases = [
        "resume", "resume the song", "continue the video", "keep playing the music", "resume playback",
        "unpause", "play the music again", "continue playing", "unpause the music", "resume audio",
        "keep playing", "start playing again", "unpause the video", "resume playing", "play the track again",
        "continue the music", "resume the audio", "keep the music playing", "unpause playback",
    ]
    for q in resume_phrases:
        add(q, call("resume_media"))
        add(f"please {q}", call("resume_media"))
        add(f"can you {q}", call("resume_media"))

    # --- Track navigation: Next vs Previous ---
    next_phrases = [
        "next song", "skip", "skip this track", "next", "play the next song", "skip ahead",
        "skip to the next track", "go to the next song", "skip the song", "next track please",
        "play next track", "forward to the next song", "advance to next track", "skip to next song",
        "next track", "skip track", "jump to next song", "play next", "next one please",
    ]
    for q in next_phrases:
        add(q, call("next_track"))
        add(f"please {q}", call("next_track"))
        add(f"can you {q}", call("next_track"))

    prev_phrases = [
        "previous song", "previous track", "go back a song", "last track", "play the previous song",
        "back to the last song", "replay the last track", "go back to the song before",
        "go back to the previous track", "play the last song again", "restart the previous song",
        "go to previous track", "rewind to previous track", "previous track please",
        "play previous track", "replay track", "go back to last track",
    ]
    for q in prev_phrases:
        add(q, call("previous_track"))
        add(f"please {q}", call("previous_track"))
        add(f"can you {q}", call("previous_track"))

    # --- Browser Tabs: New vs Close vs Restore ---
    new_tab_phrases = [
        "open a new tab", "open a fresh tab", "open another tab", "give me a new browser tab",
        "open new tab", "open a new tab please", "create a new tab", "open a new empty tab",
        "open an empty browser tab", "open another browser tab", "start a new tab",
        "make a new tab", "add a new browser tab", "launch a new tab", "new browser tab",
        "open a blank tab", "give me another tab", "open a new tab in the browser",
        "create a fresh browser tab", "open one more tab",
    ]
    for q in new_tab_phrases:
        add(q, call("new_tab"))
        add(f"can you {q}", call("new_tab"))
        add(f"please {q}", call("new_tab"))

    close_tab_phrases = [
        "close the tab", "close this browser tab", "close the current tab", "shut this tab",
        "get rid of this tab", "close active tab", "close current browser tab", "shut the tab",
        "close this tab please", "shut this browser tab", "dismiss this tab", "kill this tab",
        "close tab", "exit this tab", "close the active browser tab", "close current tab",
    ]
    for q in close_tab_phrases:
        add(q, call("close_browser_tab"))
        add(f"please {q}", call("close_browser_tab"))
        add(f"can you {q}", call("close_browser_tab"))

    restore_tab_phrases = [
        "reopen the tab i closed", "bring back the last tab", "restore the closed tab", "undo closing the tab",
        "reopen my last closed tab", "get that tab back", "bring back the closed tab",
        "bring back the tab i just closed", "reopen the closed tab", "restore closed browser tab",
        "reopen last tab", "bring back last closed tab", "restore previous tab", "undo close tab",
        "reopen that closed tab", "bring back the tab i closed", "restore the tab i just closed",
    ]
    for q in restore_tab_phrases:
        add(q, call("restore_closed_tab"))
        add(f"please {q}", call("restore_closed_tab"))
        add(f"can you {q}", call("restore_closed_tab"))

    # --- Web navigation vs Search ---
    for s in SITES:
        for t in pick([
            "go to {s}", "open {s}", "visit {s}", "take me to {s}", "open the {s} website",
            "pull up {s}", "load {s}", "navigate to {s}", "open https://{s}", "visit https://{s}",
        ], 4):
            url = f"https://{s}" if "{s}" in t and "https://" in t and not s.startswith("http") else s
            add(t.format(s=s), call("open_website", address=url))

    for q in QUERIES:
        for t in pick([
            "search the web for {q}", "google {q}", "look up {q}", "search for {q}",
            "search online for {q}", "find out {q}", "can you google {q}",
            "search the internet for {q}", "look up {q} online",
        ], 4):
            add(t.format(q=q), call("search_web", query=q))

    for q in VIDEOS:
        for t in pick([
            "search youtube for {q}", "find {q} on youtube", "youtube search {q}",
            "look up {q} on youtube", "find me {q} videos on youtube",
            "search on youtube for {q}", "find videos of {q} on youtube",
            "search youtube videos for {q}", "look for {q} on youtube",
        ], 5):
            add(t.format(q=q), call("search_youtube", query=q))

    # --- File System ---
    for f in FOLDERS:
        for t in pick([
            "what's in {f}", "list {f}", "show me what's in {f}", "list the files in {f}",
            "what files are in {f}", "show the contents of {f}", "open and list {f}",
            "show me the {f} folder", "list everything in {f}",
        ], 5):
            add(t.format(f=f), call("list_folder", path=f))

    for f in FILES:
        for t in pick([
            "read {f}", "what does {f} say", "read the file {f}",
            "open and read {f}", "what is written in {f}", "display {f}", "print the contents of {f}",
        ], 5):
            add(t.format(f=f), call("read_file", path=f))

    for f in pick(FILES, 12):
        for w in pick(WRITE_TEXTS, 5):
            t = random.choice([
                'write "{w}" to {f}', 'save "{w}" in {f}', 'put "{w}" into {f}',
                'write {w} into {f}', 'append "{w}" to {f}',
            ])
            add(t.format(w=w, f=f), call("write_file", path=f, text=w))

    for d in DOCS:
        for t in pick([
            "open {d}", "open the file {d}", "open {d} for me", "open the {d} file",
            "open document {d}", "open {d} please",
        ], 4):
            add(t.format(d=d), call("open_file", path=d))

    # --- Multi-action requests (hand off to Planner LLM -> []) ---
    for a, b in [(random.choice(APPS), random.choice(APPS)) for _ in range(15)]:
        if a != b:
            add(f"open {a} and {b}")
            add(f"close {a} and open {b}")

    for q in [
        "pause the song and mute", "mute and pause the video", "unmute and resume the music",
        "close spotify and open vlc", "turn up the volume and open youtube",
        "open notepad and type hello", "open browser and go to reddit.com",
    ]:
        add(q)

    # ==============================================================================
    # DENSE NEGATIVE SET (Leave alone: multi-step, document editing, chat, commands)
    # ==============================================================================
    # 1. Multi-step planner tasks (do NOT execute direct tool call)
    multi_step_actions = [
        "write a letter to my landlord", "write a poem about winter", "draft a response to Sam",
        "calculate total revenue", "create a pivot table for 2024", "plot a line chart of sales",
        "make the title bold and red", "change font to Helvetica", "format the heading as H1",
        "translate this into Spanish", "summarize the key bullet points", "clean up the formatting",
        "save a copy to Google Drive", "export as PDF and email to boss", "review pull request comments",
    ]
    for a in pick(APPS, 25):
        for act in pick(multi_step_actions, 4):
            add(f"open {a} and {act}")
            add(f"in {a}, {act}")

    for verb in ["copy", "move", "sync", "backup", "archive", "compress"]:
        for f1 in pick(FOLDERS, 4):
            for f2 in pick(FOLDERS, 4):
                if f1 != f2:
                    add(f"{verb} all files from {f1} to {f2}")

    multi_step_negatives = [
        "open word and write a letter to my landlord",
        "find all my photos from 2022 and copy them to the drive",
        "make the title of this document bold",
        "fill in this form with my details",
        "book a flight to delhi",
        "buy a usb cable on amazon",
        "create a new folder called work and move my pdfs into it",
        "send an email to john saying i'm late",
        "set up a vpn connection",
        "uninstall spotify and reinstall it",
        "change the font size in this cell",
        "write a python script that renames files",
        "add a reminder to my calendar for friday",
        "translate this paragraph into french",
        "summarize the article on this page",
        "log into my bank account",
        "edit the second slide",
        "delete the old files in downloads",
        "compress the project folder into a zip",
        "convert this docx to pdf",
        "update all my apps",
        "clean up my desktop",
        "organize my downloads",
        "type my address into the form",
        "select all the text and copy it",
        "reply to this message",
        "change the cell color to red",
        "order pizza",
        "download the latest python installer",
        "install python and run setup",
        "set up my email account in outlook",
        "tag all photos in summer trip with 2023vacation",
        "move all the pdfs from downloads into documents",
        "turn on dark mode in vs code",
        "add a new row to this spreadsheet with today's sales",
        "make a new folder on the desktop called taxes and put my receipts in it",
        "sort the files in this folder by size",
        "write a summary of the report and email it",
        "open excel, enter the revenue numbers, and plot a chart",
    ]
    for q in multi_step_negatives:
        add(q)

    # 2. General chat, Q&A, and explanations
    concepts = [
        "a vpn", "quantum computing", "docker", "the cloud", "wifi 6", "machine learning",
        "a firewall", "bluetooth low energy", "cryptocurrency", "an operating system",
        "ram vs rom", "optical zoom", "dns resolution", "a microprocessor",
    ]
    for c in concepts:
        add(f"what is {c}?")
        add(f"can you explain what {c} is?")
        add(f"how does {c} work?")

    chat_negatives = [
        "what is the capital of france", "how are you", "good morning", "thank you", "thanks a lot",
        "ok", "cool", "who are you", "what can you do", "explain how wifi works", "tell me something fun",
        "is it going to rain", "how do i change my password", "what's 12 times 7", "nice job",
        "never mind", "that's it", "bye", "can you help me", "what does this error mean",
        "what is a vpn?", "why did that fail?", "tell me a joke", "why is the sky blue?",
        "how do i take a screenshot on windows", "what time is it right now", "what's the weather like",
        "who was the first president", "how tall is the eiffel tower", "how many kilometers in a mile",
        "what is the meaning of life", "are you an ai", "help me think of a gift idea",
        "hey there", "hi", "good evening", "see you later", "thanks so much", "that's enough for now",
        "how do i record my screen", "how do i check my ip address", "how do i free up disk space",
        "how do i zip a folder", "how do i uninstall a program", "why is my laptop slow",
        "why is the wifi disconnecting", "what is python used for", "what is an ssd",
        "can you tell me a story", "give me a fun fact", "install git", "install node", "install vscode",
        "install docker", "install ffmpeg", "download chrome installer", "set up my printer",
        "set up two factor auth", "configure my outlook email", "connect my bluetooth headphones",
    ]
    for q in chat_negatives:
        add(q)

    # 3. Negations (must not trigger action)
    for a in pick(APPS, 15):
        add(f"don't open {a}")
        add(f"do not close {a}")
        add(f"never open {a}")
        add(f"never close {a}")
        add(f"do not open {a}")
    negations = [
        "don't open notepad", "do not open spotify", "please don't mute", "never open edge",
        "don't turn the volume up", "do not go to reddit", "don't pause the music", "stop, don't do that",
        "don't search for that", "do not close this tab", "don't close chrome", "never mute the sound",
        "do not lower the volume", "don't delete anything", "don't shut down the computer",
        "do not mute the volume", "don't mute audio", "never mute", "don't skip this song",
        "do not pause playback", "don't resume the music", "never maximize chrome",
    ]
    for q in negations:
        add(q)

    # 4. Vague / Ambiguous phrases (must not trigger action)
    vague_negatives = [
        "open it", "close that", "do it again", "the other one", "make it work", "fix it", "go there",
        "do the usual", "same as before", "open the thing", "make it better", "do that thing from before",
        "run that", "try again", "click it", "select that", "continue", "yes", "no", "maybe",
    ]
    for q in vague_negatives:
        add(q)

    # 5. Commands addressed to Servo (must get NO call in request agent!)
    for w in [
        "in 10 minutes", "tomorrow at 9am", "every day at 9:00", "every 30 minutes", "at 5pm",
        "at noon", "each morning at 8",
    ]:
        for t in ["open notepad", "check my email", "play music", "lock screen", "mute volume"]:
            add(f"{w} {t}")
            add(f"{t} {w}")

    command_cross_negatives = [
        "dia is my default browser", "from now on use chrome for websites", "set edge as default browser",
        "cancel ab12cd", "list scheduled tasks", "clear the schedule", "my name is Ashim",
        "remember that my project lives in C:/Projects/servo", "fyi i always use vs code for editing",
        "after this, open the calculator", "when you're done with that, play some music",
        "show me the scheduled queue", "forget everything scheduled", "why did that task fail",
    ]
    for q in command_cross_negatives:
        add(q)

    return ex


def command_examples():
    ex = []
    add = lambda q, *calls: ex.append((q, list(calls)))

    tasks = [
        "open notepad", "check my email", "close chrome", "mute the sound", "open spotify",
        "remind me to drink water", "search the web for news", "open the calendar", "take a break",
        "turn the volume down", "open my project folder", "play some music", "lock the screen",
        "read my todo list", "close all tabs", "open calculator",
    ]

    whens = [
        "in 5 minutes", "in 10 minutes", "in 2 hours", "in an hour", "in 20 minutes", "in 30 minutes",
        "tomorrow at 7am", "tomorrow at 9am", "today at 4pm", "at 3:30 pm", "at 18:00", "at 5pm",
        "every day at 8:00", "every day at 9:00", "every day at 6pm", "daily at 9am",
        "every 15 minutes", "every 30 minutes", "every hour", "every 2 hours", "each evening at 7",
        "each morning at 8", "every monday at 10am", "at noon", "at midnight", "tonight at 10",
        "in half an hour", "at 6:30 pm",
    ]

    for w in whens:
        for t in pick(tasks, 1):
            add(f"{w} {t}", call("schedule_request", when=w, request=t))
            add(f"{t} {w}", call("schedule_request", when=w, request=t))
            add(f"could you {t} {w}", call("schedule_request", when=w, request=t))
            add(f"remind me to {t} {w}", call("schedule_request", when=w, request=f"remind me to {t}"))

    for t in pick(tasks, 10):
        for pat in [
            "after this, {t}", "when you're done, {t}", "once that finishes, {t}", "next, {t}",
            "queue {t}", "after that's done {t}", "when this is finished, {t}",
            "when you're done with that, {t}", "once you finish, {t}",
        ]:
            add(pat.format(t=t), call("queue_request", request=t))

    list_phrases = [
        "list scheduled tasks", "show the schedule", "what's scheduled", "what is in the queue",
        "show my scheduled jobs", "what have you got lined up", "any tasks scheduled?", "list the queue",
        "what have i got scheduled?", "show me the queue", "what is scheduled for today",
        "show all pending tasks", "list my scheduled jobs", "check the schedule", "show the task queue",
        "what tasks are scheduled?", "show pending jobs", "list all scheduled items",
    ]
    for q in list_phrases:
        add(q, call("list_scheduled"))
        add(f"please {q}", call("list_scheduled"))
        add(f"can you {q}", call("list_scheduled"))

    job_ids = [
        "ab12cd", "9f3e21", "4c4c4c", "ff00aa", "123abc", "deadbe", "c0ffee", "9a9a9a", "b4d455",
        "3fa9c1", "a1b2c3", "77aa88", "f0f0f0",
    ]
    for j in pick(job_ids, 10):
        for pat in [
            "cancel {j}", "remove job {j}", "delete scheduled task {j}", "drop {j}",
            "unschedule {j}", "cancel task {j} please", "please drop the job {j}",
            "remove task {j} from the schedule", "delete job {j}",
        ]:
            add(pat.format(j=j), call("cancel_scheduled", job_id=j))

    clear_phrases = [
        "clear the queue", "cancel everything scheduled", "remove all scheduled tasks", "wipe the schedule",
        "delete all my scheduled jobs", "clear all scheduled and queued tasks", "forget everything i scheduled",
        "cancel all my scheduled tasks", "clear the schedule", "cancel everything i scheduled",
        "wipe all scheduled jobs", "delete everything in the queue", "drop all scheduled tasks",
        "remove everything from the schedule", "unschedule everything", "clear all pending jobs",
        "cancel all queued tasks", "forget all scheduled jobs", "empty the schedule", "reset the schedule",
    ]
    for q in clear_phrases:
        add(q, call("clear_schedule"))
        add(f"please {q}", call("clear_schedule"))
        add(f"can you {q}", call("clear_schedule"))

    browsers = ["dia", "arc", "chrome", "edge", "firefox", "brave", "opera", "vivaldi"]
    for b in browsers:
        for pat in [
            "{b} is my default browser", "use {b} for web pages", "i prefer {b} for browsing",
            "from now on open websites in {b}", "my browser is {b}", "always use {b} for links",
            "set my browser to {b}", "from now on use {b} for websites",
            "i'd rather you open links in {b}", "make {b} the default browser",
            "set default browser to {b}",
        ]:
            add(pat.format(b=b), call("set_default_browser", browser=b))

    for q in [
        "why did it fail", "what happened", "why didn't that work", "explain the error", "what broke",
        "why did the last task fail", "diagnose the last run", "why did you stop", "that failed, why?",
        "why did that fail?", "what went wrong", "hmm that didn't work, what happened there",
        "why did it error out", "what caused that failure", "why didn't it work",
        "tell me what went wrong", "diagnose that failure", "what failed just now",
        "why did that step fail", "explain why that failed", "that didn't work, why?",
        "hmm why did that fail", "what happened there", "why did the last run fail",
        "can you diagnose why that failed", "please explain what went wrong",
        "check why the last task failed", "diagnose the error",
    ]:
        add(q, call("diagnose_failure"))
        add(f"please {q}", call("diagnose_failure"))

    run_ids = ["a1b2c3d4", "77aa88bb", "run42x", "3fa9c1", "88cc44dd"]
    for r in run_ids:
        for pat in [
            "diagnose run {r}", "why did run {r} fail", "explain run {r}", "diagnose run {r} please",
        ]:
            add(pat.format(r=r), call("diagnose_failure", run_id=r))

    facts = [
        "my name is Priya", "i live in Pokhara", "i prefer dark mode", "my work folder is D:/work",
        "i use firefox for work", "my favourite editor is vim", "call me Sam", "i'm left handed",
        "my birthday is in june", "i usually work late", "my name is Ashim",
        "remember that my project lives in C:/Projects/servo", "fyi i always use vs code for editing",
        "my email is user@example.com", "i am working from home today",
    ]
    for f in facts:
        for pat in [
            "{f}", "remember that {f}", "fyi {f}", "note that {f}", "just so you know, {f}",
            "please remember {f}",
        ]:
            add(pat.format(f=f), call("remember_fact", fact=f))

    # ==============================================================================
    # DENSE COMMAND NEGATIVES (Must get NO CALL [] in command agent!)
    # ==============================================================================
    # 1. Ordinary desktop requests (meant for request agent - NOT commands!)
    # Crucial: app openings must NOT trigger set_default_browser!
    for a in APPS:
        add(f"open {a}")
        add(f"close {a}")
        add(f"launch {a}")

    for b in browsers:
        add(f"open {b}")
        add(f"close {b}")
        add(f"launch {b}")
        add(f"start {b}")
        add(f"switch to {b}")

    for q in [
        "turn the volume up a bit", "what's the weather in kathmandu",
        "open notepad and write hello", "search the web for vpn", "close the browser", "set a timer for 10 minutes",
        "open the clock app", "what time is it", "schedule a meeting in outlook for friday",
        "list the files in downloads", "mute the sound", "read notes.txt", "louder", "quieter please",
        "next song", "pause the music", "take me to github.com", "open calculator", "maximize chrome",
        "open bluetooth settings", "dim screen to 50%", "close this tab", "unmute audio", "read todo.txt",
        "show me downloads", "new tab please", "skip this track", "make it louder", "turn down sound",
        "search youtube for lofi beats", "search online for pizza", "check wifi settings",
    ]:
        add(q)

    # 2. Non-schedule cancels (downloads, print jobs, subscriptions - NOT cancel_scheduled!)
    cancel_nouns = [
        "the download in chrome", "the print job in the printer window", "my subscription on amazon",
        "the download", "the file transfer", "printing", "the web search", "the upload", "the meeting in outlook",
        "the order on amazon", "the installation", "the update", "my reservation",
    ]
    for n in cancel_nouns:
        add(f"cancel {n}")
        add(f"please cancel {n}")
        add(f"stop {n}")

    # 3. Chat, Q&A, and General inquiries (NOT diagnose_failure!)
    for q in [
        "tell me a joke", "why is the sky blue?", "hello there", "thanks a lot", "who are you",
        "what can you do", "explain quantum computing", "how do i make pasta", "never mind",
        "how tall is everest", "what is a vpn?", "can you help me with something", "how are you today",
        "what is the capital of nepal", "why did dinosaurs go extinct", "explain what an api is",
    ]:
        add(q)

    return ex


def rows(examples, tools, system, held_out):
    out = []
    for q, calls in examples:
        if q.lower().strip() in held_out:
            continue
        ts = list(tools)
        random.shuffle(ts)
        out.append({"query": q, "tools": ts, "answers": calls, "system": system})
    return out


def main():
    out_path = next((a for a in sys.argv[1:] if not a.startswith("--")), os.path.join(os.path.dirname(__file__),
                                                                                         "train.jsonl"))
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 0
    random.seed(seed)
    held = {t.lower().strip() for t, _ in eval_cases.DIRECT + eval_cases.COMMANDS}
    held |= {t.lower().strip() for t in eval_cases.OUT + eval_cases.COMMANDS_OUT}

    req_data = rows(request_examples(), REQUEST_TOOLS, REQUEST_SYSTEM, held)
    cmd_data = rows(command_examples(), COMMAND_TOOLS, COMMAND_SYSTEM, held)
    data = req_data + cmd_data
    random.shuffle(data)

    with open(out_path, "w", encoding="utf-8") as f:
        for r in data:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_req = sum(r["system"] == REQUEST_SYSTEM for r in data)
    n_cmd = len(data) - n_req
    n_nocall = sum(not r["answers"] for r in data)
    print(f"Generated {len(data)} examples:")
    print(f"  Requests: {n_req} (no-call: {sum(not r['answers'] for r in req_data)})")
    print(f"  Commands: {n_cmd} (no-call: {sum(not r['answers'] for r in cmd_data)})")
    print(f"  Total no-call (negative): {n_nocall} ({n_nocall / len(data) * 100:.1f}%)")
    print(f"  Wrote to -> {out_path}")


if __name__ == "__main__":
    main()
