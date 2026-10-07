# Read Aloud for Claude Code

Have Claude Code read a document, or its last reply, out loud. The voice runs on your own computer, so nothing you listen to is sent anywhere.

**Windows only for now.** It was built and tested on Windows 11. Windows 10 should work but has not been tried. macOS and Linux are not supported yet. See [Limits](#limits).

## Why

Claude Code can take dictation, but it cannot speak. Long replies and long documents are easier to take in by ear, especially away from the desk. This adds one command, `/read-aloud`, with pause, speed, skip and back.

We wanted something small enough to read and check before trusting it with client documents: one script, a pinned set of packages, an open voice model, and no network access.

## What you get

- `/read-aloud` reads Claude's most recent reply.
- `/read-aloud <file>` reads an `.html`, `.md` or `.txt` file.
- Pause, resume, faster, slower, skip and back, by hotkey from any window or by command.
- A natural-sounding voice ([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)) that runs on an ordinary laptop processor. No graphics card needed.

## Install

You need Windows, [Claude Code](https://code.claude.com/docs), [git](https://git-scm.com/download/win), [uv](https://docs.astral.sh/uv/) and about 450 MB of free disk space.

If you do not have uv:

```powershell
winget install --id=astral-sh.uv -e
```

Then, in PowerShell, from a folder where the tool can live permanently:

```powershell
git clone https://github.com/EastridgeAnalytics/read-aloud-claude-code-skill.git
cd read-aloud-claude-code-skill
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer takes a few minutes, most of it a one-time 340 MB download of the voice model. At the end you should hear one sentence. Restart Claude Code and the `/read-aloud` command is there.

Do not move the folder afterwards. The command points at it. If you do move it, run `install.ps1` again.

### What the installer does

1. Creates an isolated Python 3.13 environment inside this folder. Your own Python is not touched.
2. Installs 13 packages at exact versions, refusing any file whose hash does not match `requirements.txt`.
3. Downloads the voice model and checks it against the SHA-256 values in `models/SHA256SUMS`.
4. Writes two small files outside this folder:
   - `%USERPROFILE%\.claude\commands\read-aloud.md`, the slash command
   - `%USERPROFILE%\.local\bin\read-aloud.cmd`, so `read-aloud pause` works in any terminal. If that folder is not on your PATH, the installer says so; the slash command and hotkeys work either way.

## Use

In Claude Code:

| Type | What happens |
|---|---|
| `/read-aloud` | Reads Claude's most recent reply |
| `/read-aloud C:\path\to\file.html` | Reads a file |
| `/read-aloud pause` or `resume` | Pauses or resumes, mid-sentence |
| `/read-aloud faster` or `slower` | Changes speed by 0.25 |
| `/read-aloud speed 1.5` | Sets the speed (0.5 to 2.0) |
| `/read-aloud skip` or `back` | Moves one paragraph |
| `/read-aloud stop` | Stops |
| `/read-aloud status` | Says where it is |

Going through Claude takes a moment for each command. Two quicker ways:

**Hotkeys**, from any window, while something is being read:

| Keys | Does |
|---|---|
| Ctrl+Alt+P | Pause or resume |
| Ctrl+Alt+= | Faster |
| Ctrl+Alt+- | Slower |
| Ctrl+Alt+] | Skip a paragraph |
| Ctrl+Alt+[ | Back a paragraph |
| Ctrl+Alt+Q | Stop |

**A terminal**, or `!` in Claude Code: `read-aloud pause`, `read-aloud speed 1.5`, `read-aloud last`.

Other options, from a terminal:

```
read-aloud voices                      list the voices
read-aloud report.md --voice am_michael
read-aloud report.md --from 12         start at piece 12
read-aloud report.md --dry-run         show what would be read, without sound
read-aloud report.md --save report.wav write an audio file to take with you
```

The speed you set is remembered for next time. To change the default voice, or to turn the hotkeys off, put this in `config.json` in the tool's folder:

```json
{ "voice": "af_heart", "speed": 1.5, "hotkeys": true }
```

## How it keeps your text on your computer

- **The voice model runs locally.** There is no account, no API key and no cloud service.
- **The reader switches off its own network access** before it loads any package. Any attempt by any of its code to open a connection fails.
- **The packages are pinned and hash-checked**, and installed from prebuilt wheels only, so nothing runs code at install time.
- **The model files are checksum-verified** against the values published with the [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) release.
- **The hotkeys are not a keylogger.** They use the Windows `RegisterHotKey` call, which tells the reader about those six combinations and nothing else you type. They exist only while a reading is in progress.
- **Reading the last reply** means opening Claude Code's own transcript file for the current session on your disk. Nothing is copied elsewhere except one working file in the tool's folder.

The whole reader is `read_aloud.py`. It is about 800 lines and worth reading before you trust it.

## Limits

- **Windows only.** The sound, pause and hotkey parts use Windows built-ins. A macOS or Linux version needs a different sound layer and has not been written.
- **File types.** `.html`, `.md` and `.txt`. No PDF or Word yet.
- **Top speed is 2.0.** That is the voice model's ceiling.
- **A speed change takes a few seconds.** The speech has to be made again, so there is a short silence and the current paragraph restarts at the new speed. Pause, skip and back are instant.
- **What it skips.** Navigation links, diagrams and code blocks. Tables are read row by row after the column names.
- **Hotkey clashes.** While a reading is in progress, the six combinations take priority over the same keys in whatever program you are in.
- **English.** The voice model supports other languages, but this tool is set up and tested for English only.

Measured on the laptop it was built on (Intel i5-1235U, no graphics card): speech is made about 2.5 times faster than real time, the first words come 5 to 7 seconds after the command, and the reader uses about 800 MB of memory while it runs.

## Update and uninstall

To update: `git pull`, then run `install.ps1` again. It keeps the model if the checksum still matches.

To uninstall, delete three things:

1. this folder
2. `%USERPROFILE%\.claude\commands\read-aloud.md`
3. `%USERPROFILE%\.local\bin\read-aloud.cmd`

## How it works

`read_aloud.py` turns the document into short pieces of plain text, a paragraph or a few sentences each. One thread turns pieces into speech a few ahead of where you are listening. The main thread plays them through the Windows wave-out device and listens for commands, which arrive from the hotkeys or from a small control file that the `read-aloud` command writes to. Reading runs as its own background process, so Claude Code is free while you listen.

## Credits and licences

This repository is MIT licensed. See `LICENSE`.

It installs, and does not include, the following:

- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) voice model, Apache 2.0
- [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx), MIT
- [ONNX Runtime](https://onnxruntime.ai/), MIT
- [phonemizer](https://github.com/bootphon/phonemizer) and [espeak-ng](https://github.com/espeak-ng/espeak-ng), GPL 3.0, used to work out pronunciation

Built by Eastridge Analytics.
