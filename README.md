# strophalos

Zero-touch disc-to-library pipeline. Insert a disc, walk away — ripped files are automatically identified via TMDb and hard-linked into a Jellyfin/Plex-ready library structure.

Raw rips are preserved in an immutable archive (`archive/{tv,movies}/rips/{medium}/{label}/disc{N}/`). The library (`tv/`, `movies/`) is derived via hard links — same bytes, no extra space, can be blown away and regenerated from archive at any time.

Uses [makemkv](https://www.makemkv.com/) (product key req. for >30d) for video discs and [whipper](https://github.com/whipper-team/whipper) for audio CDs. also uses uhhh TODO for the DVDs. 

UHD Blu-ray ripping requires a LibreDrive compatible drive — check the [makemkv forums](https://forum.makemkv.com/).

## overview & background

my blu-ray reader (ASUS BW-16D1HT) has some Quirks that made writing a proper pipeline challenging. I succeeded anyways. The gist:

* always says a disc is inserted, even when one isn't
  * proper check is to check `sgX` for the size it reports, and only interact with `srX` if sgX says the size != 0
  * otherwise, the drive firmware can get janky/wedged

for reference:

    ➜ sg_readcap /dev/sg0
        Read Capacity results:
        Last LBA=22051615 (0x1507b1f), Number of logical blocks=22051616
        Logical block length=2048 bytes
    Hence:
        Device size: 45161709568 bytes, 43069.6 MiB, 45.16 GB
    ➜ sg_readcap /dev/sg0
        Read Capacity results:
        Last LBA=0 (0x0), Number of logical blocks=1
        Logical block length=0 bytes
    Hence:
        Device size: 0 bytes, 0.0 MiB, 0.00 GB

is the gist of the heuristic here for gating operations.

the goal is to put in a disc (audio CD, dvd, blu-ray, uhd blu-ray, data dvd (old game installers), audio+data (cooler old game installers) and have it rip and tag automatically.

this is accomplished by:
- discID -> musicbrainz lookup for audio CDs
- TMDb lookups for dvds, blu-rays
  - TV show episodes usually rip with no metadata besides duration and subtitles
    - opensubtitles -> hungarian assignment via subtitle fuzzy matching (and elimination-packing remainders against TMDb physical release listings) has had 100% success in my testing so far :)
  - Movies tend to tag just fine
- blu-rays that don't Seem To Be Movies also get checked against Musicbrainz to see if they're an audio CD (e.g. FFXIV blu-ray soundtracks)

which stages run is configurable. this is because i rip at my desktop, output over 2.5Gb NFS to my NAS, and have my NAS do the ID'ing and hard-link deploying into my jellyfin/navidrome libraries.

the rest of this readme/docs are mostly claude-written and are reasonably accurate

## Features

- **Headless** -- no GUI, no VNC, no browser. Python orchestrator daemon with configurable pipeline modes.
- **Pipeline modes** -- run the full pipeline or just the stages you need: `probe`, `scan`, `rip`, `identify`, or `full`. Supports split architectures where one machine rips and another identifies.
- **Manifest-driven handoff** -- rip status is tracked via `.rip-manifest.json` files. An `identify` mode instance can watch for completed rips on shared storage and run identification independently.
- **Smart title selection** -- scores discs as movie or TV simultaneously and picks the higher-scoring classification. Skips play-all playlists, menus, and junk titles automatically. TMDb runtime matching provides strong movie signal when disc extras look like TV episodes.
- **Automatic episode identification** -- TV disc rips are identified and hard-linked into the library as `SxxExx - Episode Title.mkv`. Uses OpenSubtitles hash lookup, AniDB hash lookup, TMDb episode groups (DVD/BD ordering), duration matching, and subtitle OCR as layered fallbacks. Runs asynchronously — the disc ejects immediately and the next rip starts while identification happens in the background.
- **Audio CD ripping** via [whipper](https://github.com/whipper-team/whipper) with MusicBrainz lookup, multi-disc detection, and accurate ripping.
- **Archive/library split** -- raw rips are preserved untouched in the archive; the library is derived via hard links with canonical TMDb names. Archive can be re-identified at any time.
- **Media type sorting** -- video rips are sorted into `dvd/`, `bd/`, and `uhd/` directories based on disc type detection.
- **UHD / 4K Blu-ray support** -- full `disc:0` scan for AACS2 handshake on UHD media.
- **Reliable disc detection** -- uses SCSI `sg_readcap` via the generic device to detect disc presence without triggering block layer I/O. Handles drives that report stale status (e.g. ASUS BW-16D1HT). Media-changed events gate probe attempts to avoid phantom triggers.
- **Post-rip hooks** -- shell scripts in `/config/hooks/` for custom sorting, notifications, or post-processing.
- **Auto-eject** on completion (configurable).
- **Whipper patch** for ambiguous MusicBrainz releases -- omits release-level tags when multiple releases match a disc ID, avoiding incorrect metadata.

## Quick Start

```yaml
services:
  strophalos:
    build: .
    container_name: strophalos
    restart: unless-stopped
    privileged: true  # needed for mount (probe stage)
    environment:
      # Pipeline mode: full|rip|scan|probe|identify
      - STROPHALOS_MODE=full
      # Block device path inside the container (must match the device mapping below)
      - DEVICE=/dev/sr0
      # Seconds between disc-presence checks (lightweight SCSI readcap, no spin-up)
      - POLL_INTERVAL=5
      # Eject the disc after ripping (set to 0 to leave it in the drive)
      - EJECT_ON_COMPLETE=1
      # MakeMKV registration key (optional-ish, 30d evaluation period)
      - MAKEMKV_KEY=
      # TMDb API key for title-based TV vs movie classification (optional, improves accuracy)
      - TMDB_API_KEY=
      # OpenSubtitles API key for hash-based episode identification (optional)
      - OPENSUBTITLES_API_KEY=
      # MusicBrainz server for audio disc identification (optional, defaults to musicbrainz.org)
      - MB_SERVER=
      # Output file ownership — UID:GID for all ripped files (default 1000:1000)
      - PUID=1000
      - PGID=1000
    volumes:
      # Persistent config: MakeMKV settings, whipper config, hooks
      - ./data:/config:rw
      # Library root: archive + library views
      - /path/to/library:/media:rw
      # Audio CD rip output (FLAC files)
      - /path/to/cd/rips:/output-cd:rw
    devices:
      # Block device (sr) -- the optical drive itself
      - /dev/sr0:/dev/sr0
      # SCSI generic device (sg) -- required for AACS/BD+ decryption + disc detection
      - /dev/sg0:/dev/sg0
```

## Pipeline Modes

Set `STROPHALOS_MODE` to control how far the pipeline runs:

| Mode | What it does |
|------|--------------|
| `full` | Two-pass (see below): first insertion plans + ejects, re-insertion rips per plan, then identifies. Default, all-in-one. |
| `rip` | Same two-pass flow, but no identification. Use when another machine handles ID. |
| `scan` | Probe -> scan/classify -> write/refresh the rip plan -> eject. Never rips, even for already-planned discs — useful for cataloging a stack. |
| `probe` | Detect disc type and label -> eject. Fastest, just identification of the media. |
| `identify` | No disc needed. Polls the archive for completed rips (`.rip-manifest.json` with `status: done`) and runs the appropriate identifier. Use on the machine with library access. |

## Two-Pass Ripping (Rip Plans)

Video discs (DVD/BD/UHD, including audio Blu-rays) rip in two passes:

1. **First insertion** — the daemon scans and classifies the disc, writes an
   editable `.rip-plan.json` into the prospective output directory (plus a
   `.rip-manifest.json` with `status: planned`), sends a notification with
   the plan path, and ejects. Nothing is ripped.
2. **Review (optional)** — while the disc is out, edit the plan:
   - `plan.titles_to_rip` / `plan.disc_type` — drive the rip.
   - `identify.url` — pin the whole disc to a TMDb or MusicBrainz URL.
   - `identify.matches` — map individual titles to entries by URL. This is
     how a dual-feature disc becomes two properly-named movies:

     ```json
     "identify": {
       "matches": [
         {"url": "https://www.themoviedb.org/movie/1498-teenage-mutant-ninja-turtles", "titles": [3]},
         {"url": "https://www.themoviedb.org/movie/8845-teenage-mutant-ninja-turtles-ii", "titles": [4]}
       ]
     }
     ```

     The first title in a match is that movie's main feature; any further
     titles become its extras. MusicBrainz `/release/<uuid>` URLs (from
     musicbrainz.org or a self-hosted server) pin audio-BD releases.
   - `rm` the plan file to reset to classifier defaults.
3. **Re-insertion** — the disc is recognized by its fingerprint
   (`disc_id`), the plan is validated (bad URLs, stale title ids, etc.
   fail loudly with a notification and an eject — nothing is wiped), and
   the rip runs per the plan into the same directory. Re-inserting an
   already-ripped disc re-rips it per its plan.

The plan file's existence is the "validated" signal — an untouched plan
rips exactly what the classifier picked. Check an edited plan before
re-inserting with `rip-plan --validate <disc-id-or-path>`; discs that
can't be fingerprinted (rare) fall back to the old single-pass rip.

### Split Architecture

Strophalos supports splitting rip and identify across machines sharing storage:

```
Desktop (STROPHALOS_MODE=rip)     Server (STROPHALOS_MODE=identify)
  Insert disc                       Polls /media/archive for
  -> probe -> rip                   .rip-manifest.json status: done
  -> write .rip-manifest.json       -> runs identify-episodes/movie/music
  -> eject                          -> hard-links to library
```

Both machines mount the same storage. The rip machine writes a `.rip-manifest.json` with `status: done` when a rip completes. The identify machine watches for these and runs identification.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `STROPHALOS_MODE` | Pipeline mode (`full`, `rip`, `scan`, `probe`, `identify`). | `full` |
| `DEVICE` | Block device path for the optical drive (e.g. `/dev/sr0`) | `/dev/sr1` |
| `POLL_INTERVAL` | Seconds between disc-presence polls | `5` |
| `EJECT_ON_COMPLETE` | Eject disc after ripping (`1` = yes, `0` = no) | `1` |
| `MAKEMKV_KEY` | MakeMKV registration key. Written to `/config/settings.conf` on startup. Required for Blu-ray and UHD decryption. | *(none)* |
| `TMDB_API_KEY` | [TMDb API key](https://www.themoviedb.org/settings/api) for title-based TV vs movie classification. When set, the disc label is searched on TMDb and the result biases the scoring toward the correct media type. Runtime matching provides strong movie signal for discs with many extras. | *(none)* |
| `OPENSUBTITLES_API_KEY` | [OpenSubtitles API key](https://www.opensubtitles.com/en/consumers) for hash-based episode identification. | *(none)* |
| `MB_SERVER` | MusicBrainz server hostname for audio disc identification. Supports self-hosted instances. | `musicbrainz.org` |
| `PUID` | UID for output file ownership. | `1000` |
| `PGID` | GID for output file ownership. | `1000` |
| `ASSUME_DISC_ORDER` | Assume discs are ripped in sequential order. Set to `false` if ripping out of order. | `true` |

### OpenSubtitles Setup

OpenSubtitles hash-based identification is the most reliable episode matching method when a match exists in the database. It uses a file hash (not content analysis) to definitively identify episodes.

1. Get an API key from [OpenSubtitles](https://www.opensubtitles.com/en/consumers)
2. Add `OPENSUBTITLES_API_KEY=your_key` to your compose environment
3. Recreate the container, then run the one-time login:

```sh
docker exec -it strophalos setup-opensubtitles
```

This prompts for your OpenSubtitles username and password and stores a JWT token in `/config/opensubtitles.json`.

## Volumes

| Mount point | Description |
|---|---|
| `/config` | Persistent configuration. MakeMKV settings, whipper config, hooks, identification logs, OpenSubtitles JWT. Seeded with defaults on first run. |
| `/media` | Library root. Contains `archive/{tv,movies,music}/rips/{medium}/{label}/disc{N}/` (raw rips) and `tv/`, `movies/`, `music/` (hard-linked library views). |
| `/output-cd` | Audio CD rip output. FLAC files organized as `Artist - Album/Track. Title.flac`. |

## Devices

Both the **sr** (block) and **sg** (SCSI generic) devices for your optical drive must be passed through to the container.

- The **sr** device (`/dev/sr0`, `/dev/sr1`, etc.) is the standard block device used for reading disc contents.
- The **sg** device (`/dev/sg0`, `/dev/sg1`, etc.) provides raw SCSI command passthrough. Used for disc presence detection (`sg_readcap`) and by MakeMKV for AACS/BD+ decryption.

### Stable device symlinks with udev

Device numbers can change when drives are replugged or the system reboots. A udev rule gives you stable symlinks based on the drive's serial number:

```
# /etc/udev/rules.d/99-strophalos.rules

# sr (block) device
SUBSYSTEM=="block", KERNEL=="sr*", ATTRS{serial}=="YOUR_DRIVE_SERIAL", SYMLINK+="strophalos/disc"

# sg (scsi_generic) device
SUBSYSTEM=="scsi_generic", KERNEL=="sg*", ATTRS{serial}=="YOUR_DRIVE_SERIAL", SYMLINK+="strophalos/sg"
```

Find your drive's serial with `udevadm info --query=all --name=/dev/sr0 | grep SERIAL`.

## How Auto-Rip Works

1. **Disc detection** -- The daemon polls for `CDROM_MEDIA_CHANGED` events every `POLL_INTERVAL` seconds. When media changes, disc presence is verified via SCSI `sg_readcap` through the generic device (`/dev/sgN`), which reads disc capacity without triggering block layer I/O. A 5-second spin-up grace period follows before probing.

2. **Probe** -- Fast disc type detection without SCSI or makemkvcon. Checks audio tracks (cdparanoia), video DVD (lsdvd via libdvdread), then mounts for Blu-ray (BDMV) or data disc detection. Labels are read via `blkid`.

3. **Disc type routing**:
   - `audio` -> whipper (MusicBrainz lookup, accurate FLAC extraction)
   - `dvd` -> lsdvd scan + dvdbackup + mkvmerge (no SCSI needed)
   - `bluray` / `unknown` -> makemkvcon (full SCSI scan, handles AACS/BD+/UHD)
   - `data` -> dd to ISO
   - `audio+data` -> dd to ISO + whipper

4. **Smart title classification** (video discs) -- Scores against both movie and TV patterns simultaneously:
   - **Movie signals**: dominant title, feature length, short extras, TMDb runtime match.
   - **TV signals**: uniform duration cluster, episode count, play-all detection, typical episode length.
   - **TMDb runtime matching**: when TMDb returns movie results, the movie's runtime is compared against title durations. A match within 5% provides a strong movie signal (+0.6), resolving cases where disc extras mimic TV episode patterns.

5. **Manifest** -- A `.rip-manifest.json` is written to the output directory on rip completion with `status: done`, enabling the identify stage to find and process completed rips.

6. **Library identification** (background) -- In `full` mode, identification runs in a background thread after ripping. In `identify` mode, the daemon polls the archive for completed rips. See Identification below.

7. **Post-rip hooks** -- Hooks in `/config/hooks/` are executed with disc metadata as arguments.

8. **Eject** -- If `EJECT_ON_COMPLETE=1`, the disc is ejected and the daemon waits for the next media-changed event.

## Identification

All identification runs asynchronously after rip — the disc ejects and the next rip can start immediately. Logs in `/config/logs/identify-*.log`, manifests in each archive disc directory.

### Movies

Disc label is searched on TMDb, canonical title + year is resolved, main feature (largest file) is hard-linked to `movies/{Title} ({year})/`. Extras are linked alongside.

### Audio CDs

Whipper queries MusicBrainz using the disc's TOC hash — this is a deterministic match, not a heuristic. Multi-disc releases are detected and organized into `Disc N/` subdirectories.

### TV Shows

Discs have no episode metadata — just numbered titles. Matching layers, in order:

1. **OpenSubtitles hash** -- file hash lookup against the OpenSubtitles database. Definitive when a match exists.
2. **AniDB hash** -- ed2k hash lookup for anime series.
3. **OpenSubtitles text matching** -- download reference SRTs, OCR the disc's PGS subtitles via pgsrip, compare dialog text.
4. **TMDb episode data** -- DVD/BD episode group ordering when available, duration matching narrows candidates.
5. **Forward-order fallback** -- when `ASSUME_DISC_ORDER=true`, assumes sequential disc insertion and assigns the next batch of episodes in order.

If TMDb doesn't have an entry for your disc, you'll get an error notification. The raw rip is safe in the archive — add the TMDb entry and re-run:

```sh
docker exec strophalos identify-episodes --dir /media/archive/tv/rips/bd/LABEL/disc1 --label LABEL
```

## Hooks

Hook scripts live in `/config/hooks/` and are seeded from defaults on first run.

### `disc_rip_terminated.sh`

Called after a video disc rip completes (success or failure).

```
Arguments:
  $1  drive_id     MakeMKV drive index (e.g. "0")
  $2  disc_label   Disc volume label
  $3  output_dir   Path where MKV files were written
  $4  status       "SUCCESS" or "FAILURE"
```

### `disc_rip_skipped.sh`

Called when a disc is skipped.

```
Arguments:
  $1  drive_id     MakeMKV drive index
  $2  disc_label   Disc volume label
  $3  reason       One of: ALREADY_PROCESSED, NOT_VIDEO_DISC, SERVICE_FIRST_RUN
```

## Building

```sh
docker build -t strophalos .
```

To pin a specific MakeMKV version:

```sh
docker build --build-arg MAKEMKV_VERSION=1.18.3 -t strophalos .
```
