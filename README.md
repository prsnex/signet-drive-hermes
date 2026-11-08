# Signet Drive for Hermes

Signet Drive delivers post-quantum encrypted cloud storage for humans and their AI agents. It treats humans and their AI agents both as first class users. The human UI/UX is typical of cloud storage platforms. The AI agent UI/UX is "AI native", allowing AI agents to easily use the platform.

The Signet Drive plugin for Hermes brings secure cloud storage directly into your Hermes Agent and Hermes Desktop experience.

The plugin also provides a special service that automatically backs up copies of your AI agent's memory and skills files, to protect against loss.

Your agent's Signet Drive account is its own. Only you and your agent can open it. Your agent gets four tools within the plugin, to save, fetch, list and restore, and a status item in Hermes Desktop says whether its drive is in step.

**What you need.** A Signet Drive account, created at https://drive.mysignet.ca. An Apple-silicon Mac with the signed Signet app from your account page. Hermes Agent 0.23.0, with Hermes Desktop if you want the status item.

**Install, in three blocks.**
1. *Signet Drive.* Create your account, then from your account page install the Signet app and connect it with its setup code, all in one place.
2. *Hermes.* Install this plugin from the catalog or from Git in Hermes Desktop, and restart when it asks.
3. *Connect the two.* Add account on your account page, then `/signet setup` followed by the one-time code in the Hermes chat. The full steps are below.

Tested with Signet Drive 1.2.0 and Hermes 0.23.0 on an Apple-silicon Mac, November 2026. *(Draft mark, removed before publication. This sentence is earned by the final suite run on the submission tree, Bob's condition, and is pending until that run passes. If Aiko's run is on another Hermes version, that version is named here too.)*

---


## The details

Everything below says the same things at length, for a reader who wants full details.


## Plugin and Signet Drive UI/UX

The plugin allows you to view your AI agent files directly within Hermes Desktop.

Humans primarily use Signet Drive from any device via web browser, to access their own account and the accounts of their AI agents, which we call PRSNs, pronounced "persons". Each agent's account sits under its human's.

Today Signet Drive supports AI agents operating via local Apple-silicon Macs. Windows and Linux support is coming soon.


## What the plugin does

**A place for your agent's files.** Four tools, to save, fetch, list and restore. Saves go into a folder called Files and never overwrite. The four are a deliberately small set, so a tool call can never remove anything. The full Signet Drive command set, delete and sharing within the capability you give it included, stays available to your agent through the Signet app's command line, as for any agent using Signet Drive natively.

**Backup copies of your agent's memory.** After every change your agent makes to its memory or its own skills, the plugin saves them into a folder of its drive called Memory Mirror, as one dated file, and the day's file is replaced each time. It keeps one file per day for nine days, and one more for nine days after a restore, and it removes only its own older copies, so the folder never grows without end. Your agent can restore any of those copies on the same Mac, and the plugin saves the present state first so a restore can be undone.

**A status item in Hermes Desktop.** A few words are always in the status bar, such as Drive in step, Drive behind or Drive needs you. A click opens a view of the agent's files and its restore points, to read and not to change, with a button that opens your own drive in the browser.

**Plugin not required.** The plugin makes memory backup and a few other things easy and automatic. Your agent can use Signet Drive without it, natively through the Signet app's command line.


## Install

Three blocks, each finished before the next.

**1. Signet Drive.** Create your account at https://drive.mysignet.ca. On your account page, under Signet Drive access, install the Signet app on your Mac and enter the setup code the page gives you, so the app is connected to your account. This is done once per Mac.

**2. Hermes.** In Hermes Desktop, open Capabilities and then Plugins. Install Signet Drive from the catalog with one click, or, until it is listed there, choose Install plugin, then Install from Git, and give the repository `https://github.com/prsnex/signet-drive-hermes`. Both halves are on when it installs, the agent half and the Desktop half with the status item; the Desktop half can be turned off there. Restart Hermes when it asks. From a terminal the same is `hermes plugins install prsnex/signet-drive-hermes`, then `hermes plugins enable signet-drive`, then `hermes gateway restart`.

**3. Connect the two.** On your account page, open PRSN Accounts, choose Add account and then Natively on this Mac. The page shows a one-time code and waits. In the Hermes chat, type `/signet setup` followed by that code, and approve the enrolment on your account page with your passkey. The plugin narrates the rest and ends with the first check and the first copy. From a terminal the same is `hermes signet-drive setup` with the code.

Everything after that is automatic.


## In plain words

- You can see your agent's files and memory copies in your own drive, and deleting one is permanent.
- What your agent saves counts against your storage.
- Your agent's memory here means its two memory files and its own skills, never your conversations.
- Your agent keeps one memory file per day for nine days in its drive, ten files at most and one more for nine days after a restore, and removes its own older ones. A file you rename it never touches.
- Your agent's keys live in this Mac's Secure Enclave. Keys made with Signet 1.2.0 or later work once the Mac has been unlocked after it started, so copies continue while the screen is locked. Keys made with an earlier client work only while the Mac is unlocked, and copies pause when it locks and resume when you unlock it.
- Every 30 days you re-confirm your agent's access on your account page. You are warned from 7 days before. If you miss the day, the copies pause, your agent can still read for 7 more days, and then its access stops until you re-confirm.
- If your plan lapses, your account turns read-only for a time and is then deleted. If a free trial ends without an upgrade, the account is deleted. Either way your agent's account and everything in its drive go with yours.
- Your agent can restore its memory and skills on this Mac. On a new Mac it is a new account, and you give it the old folder by steps we describe.


## What it never does

- Its tools never delete anything, and the plugin itself removes only its own older restore points, by exact name. Deleting a file is your act in the browser, or your agent's through the Signet app's command line.
- It never copies your conversations.
- It never speaks to the server itself. Every call goes through the signed Signet app, which holds your agent's keys.
- It never holds a credential of its own.
- A file your agent fetches comes back labelled as data, never as an instruction, since you or anyone you invited could have put it there.


## The status line

The check runs at every start and never delays Hermes. When all is well it reads like this. "Checked 11:28. Client genuine, enrolled as your-agent, drive reachable. Key matches the public log. Last mirror 11:27, in step." When something needs you it says so in the same place, in plain words, and the status item in Hermes Desktop turns to Drive needs you.

The check confirms four things. The Signet app is the published one, by Apple's signature, our Team ID and a byte tie through the manifest in our public hashes repository. Your agent is enrolled. Its key is in the public transparency log, by the app's own check. The drive answers and your agent's access is authorized.


## Settings

Under `plugins.entries.signet-drive.settings` in the profile's Hermes config.

| Setting | Default | What it does |
|---|---|---|
| `keep_days` | 9 | The one retention number. One restore point per day is kept, a point older than this many days is removed, and the newest is always kept. |
| `prune` | true | The mirror removes its own older points by the rule above. Off, it only counts what waits. |
| `include_skills` | true | Copy the agent's own skills as well as its two memory files. |
| `root_name` | Memory Mirror | The folder that holds the copies. |
| `files_folder` | Files | The folder the file tools use. |
| `handle` | set by setup | The agent's Signet handle, named on every call. |
| `server_url` | the app's default | The Signet Drive server. |


## What the plugin reaches on the network

- The Signet Drive server the profile names, `drive.mysignet.ca` by default, through the Signet app, for every drive call.
- `raw.githubusercontent.com`, the public hashes repository `prsnex/signet-drive-public-hashes`, once per app version, to check the installed app against the published release.
- Through the app's own check, the public transparency log's anchor files for that server, also on `raw.githubusercontent.com`, `prsnex/signet-drive-transparency-log` for production.

Nothing else.


## For developers

| File | What it does |
|---|---|
| `plugin.yaml` | The manifest. Four tools, three hooks, the settings. |
| `__init__.py` | Registration. The hooks at session start, after each memory or skill write and at session end, the tools, the `/signet` command and `hermes signet-drive setup`. All work runs off the agent's thread, with the profile's context carried onto every thread the plugin starts. |
| `signet_cli.py` | Runs the Signet app and reads its JSON output and exit codes. |
| `sd_status.py` | The check at every start and the status line. |
| `sd_mirror.py` | The Memory Mirror. One file per restore point, one point per day, nine days back, the undo point before a restore, the mirror removing only its own retired points by exact name. |
| `sd_tools.py`, `sd_schemas.py` | Save, fetch, list and restore. |
| `dashboard/` | Two read-only routes for the Desktop part. |
| `desktop/plugin.js` | The status bar item and the view it opens. |
| `tests/` | A fake Signet app with a JSON drive, and the suites. |

`hermes plugins validate` and `hermes plugins doctor` pass on this tree. The tests run as scripts, `python3 tests/test_mirror.py`, `test_tools.py` and `test_status.py`.


## Licence and trademarks

The code in this repository is under the MIT licence. "Signet Drive" and "Signet" are trademarks of PRSN EX Inc. The licence grants no trademark rights. See `TRADEMARKS.md` in `prsnex/signet-drive`.
