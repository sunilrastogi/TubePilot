# yt-channel-lab

A lightweight, local research-and-production tool for your YouTube channel. It runs on your own computer, has a browser UI, and uses only the Python standard library for the server (Pillow and a bundled ffmpeg are the only pip packages).

**What it does**

| Tab | What you get |
|---|---|
| **My channel** | Subscribers, median views, views/day, engagement, upload cadence, best weekday and video length, top and weakest videos, title keywords that move your views, and a table of all videos. |
| **Video analysis** | Paste any video URL, or click *Analyze* on one of yours. It finds similar successful videos from other channels and compares tags, title, description and length. Claude also compares thumbnails and suggests keywords, tags, titles, a description rewrite and prioritized fixes. |
| **Next 3 videos** | Combines your channel data with niche trends to propose three ideas. Pick one, add optional inputs, and get a full package: editable script with timestamps, trending keywords, tags, title options, description, and thumbnail concepts based on the trending thumbnails. Thumbnails can be generated with OpenAI and downloaded. |
| **Niche trends** | Rising topics, small channels beating their size, and momentum (views/day) for your niche keywords. |
| **Quick script** | A one-shot script with timestamps for any topic. |
| **Workspace** | Builds `style.md` (your editing style) from reference videos, indexes your reusable assets into `master_assets.md`, manages one folder per video, and writes an edit plan for each. |
| **Publish** | Uploads the finished video from its folder to YouTube with the title, description, tags and thumbnail you prepared. |

**Multiple channels:** manage as many channels as you like. Pick the active one from the menu at the top. Each channel has its own handle, niche, Google sign-in and files (style guide, master assets, edit folders, thumbnails, drafts), so nothing mixes between channels.

Everything stays on your machine. The app only talks to YouTube/Google, Claude (or Anthropic) and OpenAI, using your own keys.

---

## 1. Requirements

- **Windows 10/11** (the setup script is a `.bat`; the Python code itself is cross-platform)
- **Python 3.9 or newer** from [python.org](https://www.python.org/downloads/). Tick **"Add python.exe to PATH"** in the installer.
- A Google account that owns your YouTube channel
- Optional: **ffmpeg + ffprobe** on PATH for the `edit.py` cutting helper (see [Video editing helper](#6-video-editing-helper-editpy))

## 2. Quick start

1. Download this repo (green **Code** button, then **Download ZIP**, then extract), or `git clone` it.
2. **Double-click `setup.bat`.** It checks Python, installs the packages, creates the full folder structure (see [section 4](#4-folder-structure)) and creates your `.env`.
3. Add your credentials (next section). You can paste them into the app's **Settings** tab, or edit `.env`. Then add your channel(s) under **Settings → Channels**: a name, the `@handle` and the niche keywords. (If you put `CHANNEL=@yourhandle` in `.env` before running `setup.bat`, your first channel is created for you.)
4. **Double-click `run.bat`.** The app opens at <http://127.0.0.1:8791>.

To stop it, close the black console window.

## 3. Credentials

You need **one** to get started (the YouTube key); the rest unlock more features.

| Credential | Needed for | Required? |
|---|---|---|
| YouTube Data API key | Channel stats, trends, video comparison | **Yes** |
| Claude login **or** Anthropic API key | Analysis, ideas, scripts, edit plans, style guide | For all the AI features |
| OpenAI API key | Thumbnail generation, `edit.py` transcription | Optional |
| Google OAuth client | Publishing to YouTube | Optional |

### 3.1 YouTube Data API key

1. Go to <https://console.cloud.google.com> and create a project (or pick one).
2. **APIs & Services → Library → "YouTube Data API v3" → Enable.**
3. **APIs & Services → Credentials → Create credentials → API key.** Copy it.
4. Paste it into **Settings → YouTube Data API v3 key** and click **Save** (or put it in `.env` as `YOUTUBE_API_KEY=`).

The free quota is 10,000 units/day. A trend search costs about 100 units per keyword, a video comparison about 210, and an upload 1,600. Results are cached for an hour.

### 3.2 Claude (pick one)

**Option A: use your Claude subscription (no API key).**
1. Install Claude Code from <https://claude.com/claude-code>.
2. Open a terminal, run `claude`, then type `/login` and sign in.
3. Leave the Anthropic key blank in the app. The top bar shows **Claude (login) ✓**.

Calls go through the `claude` command-line tool and count against your plan's usage limits.

**Option B: Anthropic API key.**
1. Create a key at <https://console.anthropic.com/settings/keys>.
2. Paste it into **Settings → Anthropic API key** (or `ANTHROPIC_API_KEY=` in `.env`). If set, it takes priority over the CLI.

### 3.3 OpenAI API key (thumbnails)

1. Create a key at <https://platform.openai.com/api-keys> and add billing credit.
2. Paste it into **Settings → OpenAI API key** (or `OPENAI_API_KEY=` in `.env`).

Thumbnails use `gpt-image-1`, which may require organization verification in your OpenAI account. High quality costs roughly $0.17 per image; medium is about $0.04. Claude writes the art-directed prompt from your video's idea, so each image is context-aware.

### 3.4 Google OAuth (publishing to YouTube)

You only need this if you want the **Publish** tab. Use the same Google Cloud project as the API key.

1. **Consent screen.** Open **APIs & Services → OAuth consent screen** (may be labelled *Google Auth Platform*).
   - User type: **External**. Fill in an app name (anything) and your email.
   - Leave the publishing status as **Testing**.
   - **Audience → Test users → Add users:** add the Google account that owns your YouTube channel.
   - **Data access / Scopes → add:**
     `https://www.googleapis.com/auth/youtube.upload` and
     `https://www.googleapis.com/auth/youtube.readonly`
2. **Create the client.** **Credentials → Create credentials → OAuth client ID.**
   - Application type: **Desktop app** (not "Web application").
   - Click **Create**, then copy the **Client ID** and **Client secret**.
3. **Add them to the app.** In **Settings**, paste the Client ID and Client secret and click **Save** (or set `GOOGLE_CLIENT_ID=` and `GOOGLE_CLIENT_SECRET=` in `.env`).
4. **Connect.** Select the channel in the top menu, go to the **Publish** tab and click **Connect Google account**. Each channel signs in separately, so repeat this for every channel you publish to.
   - Choose the Google account that owns that YouTube channel.
   - On "Google hasn't verified this app", click **Advanced → Go to … (unsafe)**. This is expected for your own app in Testing mode.
   - Allow the permissions. You return to the app and the Publish tab shows the connected channel name.

Notes:
- While the consent screen is in **Testing**, Google expires the sign-in after **7 days**. Click **Reconnect** in Publish when that happens.
- Uploads from unverified API projects may be forced to **Private** by YouTube. Check the video in YouTube Studio after your first upload.
- The app must run on port **8791** (the default) because the sign-in redirect uses it.

### `.env` reference

`setup.bat` creates `.env` from [`.env.example`](.env.example). Never commit it (it is in `.gitignore`).

```
YOUTUBE_API_KEY=
ANTHROPIC_API_KEY=        # blank = use the claude CLI login
OPENAI_API_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
CHANNEL=@yourchannel      # only seeds your FIRST channel on first run; manage channels in the app afterwards
NICHE=                    # same: seeds the first channel's niche keywords
# CLAUDE_CLI_MODEL=sonnet
# OPENAI_IMAGE_MODEL=gpt-image-1
```

## 4. Folder structure

`setup.bat` creates all of this (via `python server.py --init`). Everything under `data/` is your personal content and is **git-ignored**.

```
data/
├── channels.json                 Your channels: id, name, handle, niche (edit in Settings → Channels)
└── channels/
    └── <channel-id>/             One complete workspace per channel
        ├── style_reference/          Videos whose editing style you like (1-5). Workspace → "Build style.md"
        │   └── style.md              Generated style guide (add your own notes below the marker line)
        ├── master_assets/            Reusable pictures/logos/assets for any video of this channel
        │   └── master_assets.md      Generated index: what each file is and what it suits
        ├── edits/
        │   └── <video-name>/         One folder per video you are making (template: sample_video)
        │       ├── raw_video/            Your raw footage
        │       ├── claude_assets/        Assets generated by Claude / the tools
        │       ├── user_assets/          Assets you provide for this video
        │       ├── thumbnail and metadata/   thumbnail.jpg + metadata.txt (title, description, tags, keywords, chapters)
        │       ├── final_edit/           The finished video that gets published
        │       ├── script.md             Saved from "Next 3 videos → Save to edit folder"
        │       └── edit_plan.md          Generated by Workspace → "Make edit plan"
        ├── thumbs/                   Thumbnails generated with OpenAI
        ├── projects/                 Saved drafts from "Next 3 videos"
        └── tokens.json               This channel's Google sign-in (created by "Connect Google account")
```

Removing a channel in Settings only removes it from the list. Its folder under `data/channels/` stays on disk.

**Upgrading from an older single-channel version?** On the next start (or `setup.bat`) your existing `data/style_reference`, `master_assets`, `edits`, `thumbs`, `projects` and `tokens.json` are moved into your first channel automatically.

### Suggested workflow

0. Choose the channel in the top menu (add channels in **Settings → Channels**).
1. **My channel** → understand what works for you. Click *Analyze* on any video to compare it with winners.
2. **Next 3 videos** → pick an idea, answer the optional questions, review and edit the generated package, generate/select a thumbnail, then **Save to edit folder**.
3. Put footage in that folder's `raw_video/` and any of your own assets in `user_assets/`.
4. **Workspace** → *Make edit plan* (uses `style.md`, `master_assets.md` and the folder contents).
5. Edit the video in your editor and export it to `final_edit/`.
6. **Publish** → choose the folder, check the details, tick the confirmation box, upload.

## 5. Costs and limits at a glance

| Action | YouTube quota | Other cost |
|---|---|---|
| Channel analysis | ~5 units | none |
| Video comparison | ~210 units | Claude call |
| Niche trends | ~100 units per keyword | none |
| Next 3 ideas | ~200 units | Claude call |
| Full package | ~100-200 units | Claude call |
| Upload | 1,600 units (about 6/day) | none |
| Thumbnail | none | OpenAI image (~$0.04-0.17) |

## 6. Video editing helper (`edit.py`)

A command-line helper that works on the same folders. It needs `ffmpeg` **and** `ffprobe` on PATH (`winget install Gyan.FFmpeg`, then reopen the terminal) and an `OPENAI_API_KEY` (Whisper transcription).

```
python edit.py analyze  <video_folder>                       transcribe and propose cuts for long pauses
python edit.py add-cut  <video_folder> START END "reason"    add a manual cut (repetition, fumble)
python edit.py cut      <video_folder>                       apply approved cuts -> claude_assets/rough_cut.mp4
python edit.py render   <video_folder>                       add user_assets overlays -> final_edit/final.mp4
```

`<video_folder>` is a folder name under the channel's `edits` folder (e.g. `sample_video`). It works on your first channel by default; add `--channel <id>` (ids are in `data/channels.json`) to use another one, e.g. `python edit.py --channel cooking-corner cut my-video`. Overlay files in `user_assets` are named `overlay_MM_SS[_LEN].ext`, for example `overlay_05_25_20.png` shows full-frame at 5:25 for 20 seconds.

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `python` not recognised | Reinstall Python and tick **Add python.exe to PATH**, then rerun `setup.bat`. |
| Page doesn't load / `Address already in use` | Another program uses port 8791. Close it, or start with `set PORT=8800 && python server.py` (publishing's sign-in then needs a matching redirect, so keep 8791 if you can). |
| "Claude CLI isn't logged in" | Open a terminal, run `claude`, type `/login`. Or add an Anthropic API key. |
| "Add your YOUTUBE_API_KEY" | Save the key in Settings, then retry. |
| `quotaExceeded` | You used the daily 10,000 units. It resets at midnight Pacific time. |
| `redirect_uri_mismatch` | The OAuth client must be of type **Desktop app**, and the app must run on port 8791. |
| `access_denied` on connect | Add your Google account under **Test users** on the consent screen. |
| Upload is private | Expected for unverified projects; change visibility in YouTube Studio. |
| "Channel '@x' not found" | Check the handle in **Settings → Channels** (it needs the `@`, or use the `UC…` channel ID). |
| Publish says the wrong channel / account | Each channel signs in separately. Select the right channel in the top menu, then **Reconnect** in Publish and pick the Google account that owns it. |
| My files seem to be missing after upgrading | They moved to `data/channels/<first-channel-id>/`. Nothing is deleted; see the upgrade note in section 4. |
| Style analysis says ffmpeg missing | Run `python -m pip install imageio-ffmpeg` (setup.bat does this). |
| Thumbnail generation fails with a verification error | Verify your organization in the OpenAI dashboard to use `gpt-image-1`. |

## 8. Privacy and security

- The server listens on `127.0.0.1` only and rejects requests from other sites or hostnames.
- Keys live in `.env`; each channel's Google sign-in lives in `data/channels/<id>/tokens.json`. Both are git-ignored. Don't share them.
- Data sent out: YouTube metadata and thumbnails (to Claude for analysis), prompts (to OpenAI), and, only when you publish, your video, title, description, tags and thumbnail (to YouTube).
- Uploading always requires an explicit confirmation tick and defaults to **Private**.

## 9. License

[MIT](LICENSE)
