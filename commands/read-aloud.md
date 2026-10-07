---
description: Read a document or the last reply aloud, offline. /read-aloud [file | last | pause | resume | faster | slower | speed 1.5 | skip | back | stop | status]
argument-hint: [file | last | pause | resume | faster | slower | speed N | skip | back | stop | status]
---
Read something aloud to the user with the offline Kokoro reader in `{{FOLDER}}`.

Arguments: $ARGUMENTS

Run the reader from Bash, passing the arguments through:

`"{{PYTHON}}" "{{SCRIPT}}" <arguments>`

What to pass:

- **Nothing, `last`, "that", "your last reply"**: pass `last`. The reader takes the most recent finished reply straight from this session's transcript. Do not retype the reply into `--text`.
- **A file path** (.html, .md or .txt): pass the absolute path in quotes.
- **A control word**: `pause`, `resume`, `faster`, `slower`, `speed 1.5`, `skip`, `back`, `stop`, `status`. Pass it as is.
- **Other text the user points at** that is neither a file nor the last reply: write it to a .md file in a temporary folder and pass that file.
- Options in plain words: a voice (`--voice am_michael`; `voices` lists them), a start point (`--from N`; piece numbers come from `--dry-run`), an audio file to take along (`--save out.wav`).

Before the Bash call, say nothing or one short line. Text written before the call is not read, but it delays the start. After it, report in one line what the reader printed. For a control word, the reader's own one-line answer is enough.

The user can also control a reading without Claude:

- Keys from any window while it reads: Ctrl+Alt+P pause or resume, Ctrl+Alt+= faster, Ctrl+Alt+- slower, Ctrl+Alt+] skip, Ctrl+Alt+[ back, Ctrl+Alt+Q stop.
- In a terminal, or with `!` in Claude Code: `read-aloud pause`, `read-aloud speed 1.5`, `read-aloud last`.

The speed the user sets is remembered for the next reading. 2.0 is the model's ceiling.

The reader is offline by design and blocks its own network access. Never substitute an online speech service for it.
