# strophalos

Zero-touch disc-to-library pipeline. Insert a disc, walk away — ripped files are automatically identified via TMDb and hard-linked into a Jellyfin/Plex-ready library structure.

Raw rips are preserved in an immutable archive (`archive/{tv,movies}/rips/{medium}/{label}/disc{N}/`). The library (`tv/`, `movies/`) is derived via hard links — same bytes, no extra space, can be blown away and regenerated from archive at any time.

**Constraint**: discs must be ripped in sequential order (disc 1 first, then disc 2, etc.) so that episodes can be packed against TMDb DVD/Blu-ray release orderings. If TMDb doesn't have an entry for your disc, you'll get a notification to go add it — the archive is safe and re-identification can be run at any time.

Uses [makemkv](https://www.makemkv.com/) (product key req. for >30d) for video discs and [whipper](https://github.com/whipper-team/whipper) for audio CDs. UHD Blu-ray ripping requires a LibreDrive compatible drive — check the [makemkv forums](https://forum.makemkv.com/).

## Features

- **Headless** -- no GUI, no VNC, no browser. Just a polling loop and `makemkvcon`.
- **Smart title selection** -- scores discs as movie or TV simultaneously and picks the higher-scoring classification. Skips play-all playlists, menus, and junk titles automatically.
- **Automatic episode identification** -- TV disc rips are identified and hard-linked into the library as `SxxExx - Episode Title.mkv`. Uses OpenSubtitles hash lookup, TMDb episode groups (DVD/BD ordering), duration matching, and subtitle OCR as layered fallbacks. Runs asynchronously — the disc ejects immediately and the next rip starts while identification happens in the background.
- **Audio CD ripping** via [whipper](https://github.com/whipper-team/whipper) with MusicBrainz lookup, multi-disc detection, and accurate ripping.
- **Archive/library split** -- raw rips are preserved untouched in the archive; the library is derived via hard links with canonical TMDb names. Archive can be re-identified at any time.
- **Media type sorting** -- video rips are sorted into `dvd/`, `bd/`, and `uhd/` directories based on disc type detection.
- **UHD / 4K Blu-ray support** -- full `disc:0` scan for AACS2 handshake on UHD media.
- **File ownership** -- all output files are written as `PUID:PGID` (default 1000:1000) via `setpriv`, not chowned after the fact.
- **Lightweight polling** -- uses a raw `ioctl(CDROM_DRIVE_STATUS)` check that does not spin up the drive.
- **Post-rip hooks** -- shell scripts in `/config/hooks/` for custom sorting, notifications, or post-processing.
- **Auto-eject** on completion (configurable).
- **Whipper patch** for ambiguous MusicBrainz releases -- omits release-level tags when multiple releases match a disc ID, avoiding incorrect metadata.
  - i use [music magic](https://github.com/PandorasFox/music-magic) as my next stage in the pipeline for handling multiple possible release matches + hard-link deployment of music

## Quick Start

```yaml
services:
  strophalos:
    build: .
    container_name: strophalos
    restart: unless-stopped
    environment:
      # Block device path inside the container (must match the device mapping below)
      - DEVICE=/dev/sr0
      # Seconds between disc-presence checks (lightweight ioctl, no spin-up)
      - POLL_INTERVAL=5
      # Eject the disc after ripping (set to 0 to leave it in the drive)
      - EJECT_ON_COMPLETE=1
      # MakeMKV registration key (optional-ish, 30d evaluation period)
      - MAKEMKV_KEY=
      # TMDb API key for title-based TV vs movie classification (optional, improves accuracy)
      - TMDB_API_KEY=
      # OpenSubtitles API key for hash-based episode identification (optional)
      - OPENSUBTITLES_API_KEY=
      # Output file ownership — UID:GID for all ripped files (default 1000:1000)
      - PUID=1000
      - PGID=1000
    volumes:
      # Persistent config: MakeMKV settings, whipper config, hooks
      - ./data:/config:rw
      # Video rip output (MKV files)
      - /mnt/media/video-rips:/output:rw
      # Audio CD rip output (FLAC files)
      - /mnt/media/cd-rips:/output-cd:rw
    devices:
      # Block device (sr) -- the optical drive itself
      - /dev/sr0:/dev/sr0
      # SCSI generic device (sg) -- required for AACS/BD+ decryption
      - /dev/sg0:/dev/sg0
```

## Configuration

| Variable | Description | Default |
|---|---|---|
| `DEVICE` | Block device path for the optical drive (e.g. `/dev/sr0`) | `/dev/sr1` |
| `POLL_INTERVAL` | Seconds between disc-presence polls | `5` |
| `EJECT_ON_COMPLETE` | Eject disc after ripping (`1` = yes, `0` = no) | `1` |
| `MAKEMKV_KEY` | MakeMKV registration key. Written to `/config/settings.conf` on startup. Required for Blu-ray and UHD decryption. | *(none)* |
| `TMDB_API_KEY` | [TMDb API key](https://www.themoviedb.org/settings/api) for title-based TV vs movie classification. When set, the disc label is searched on TMDb and the result biases the scoring toward the correct media type. Optional but recommended — the heuristic scoring works without it, but title search resolves ambiguous cases. | *(none)* |
| `OPENSUBTITLES_API_KEY` | [OpenSubtitles API key](https://www.opensubtitles.com/en/consumers) for hash-based episode identification. When configured, ripped MKV files are hashed and looked up against the OpenSubtitles database before falling back to duration/subtitle matching. Requires one-time authentication — see below. | *(none)* |
| `PUID` | UID for output file ownership (rip processes run as this user via `setpriv`). | `1000` |
| `PGID` | GID for output file ownership. | `1000` |
| `ASSUME_DISC_ORDER` | Assume discs are ripped in sequential order (disc 1 first, then disc 2, etc.). When subtitle/duration matching can't discriminate, falls back to assigning the next batch of episodes in order. Set to `false` if ripping discs out of order. | `true` |

### OpenSubtitles Setup

OpenSubtitles hash-based identification is the most reliable episode matching method when a match exists in the database. It uses a file hash (not content analysis) to definitively identify episodes.

1. Get an API key from [OpenSubtitles](https://www.opensubtitles.com/en/consumers)
2. Add `OPENSUBTITLES_API_KEY=your_key` to your compose environment
3. Recreate the container, then run the one-time login:

```sh
docker exec -it strophalos setup-opensubtitles.sh
```

This prompts for your OpenSubtitles username and password and stores a JWT token in `/config/opensubtitles.json`. The token expires after 24 hours but the script can be re-run at any time.

When configured, hash lookup runs as the first identification layer before duration/subtitle matching. If it identifies all files on a disc, the slower subtitle extraction is skipped entirely.

## Volumes

| Mount point | Description |
|---|---|
| `/config` | Persistent configuration. MakeMKV settings, whipper config, hooks, identification logs, OpenSubtitles JWT. Seeded with defaults on first run. |
| `/media` | Library root. Contains `archive/{tv,movies}/rips/{medium}/{label}/disc{N}/` (raw rips) and `tv/`, `movies/` (hard-linked library views). Mount your library root here. |
| `/output-cd` | Audio CD rip output. FLAC files organized as `Artist - Album/Track. Title.flac`. Multi-disc releases get a `Disc N/` subdirectory. |

## Devices

Both the **sr** (block) and **sg** (SCSI generic) devices for your optical drive must be passed through to the container.

- The **sr** device (`/dev/sr0`, `/dev/sr1`, etc.) is the standard block device used for reading disc contents.
- The **sg** device (`/dev/sg0`, `/dev/sg1`, etc.) provides raw SCSI command passthrough, which MakeMKV needs for AACS and BD+ decryption. Without it, encrypted Blu-ray and UHD discs will fail to rip.

### Stable device symlinks with udev

Device numbers can change when drives are replugged or the system reboots. A udev rule gives you stable symlinks based on the drive's serial number:

```
# /etc/udev/rules.d/99-strophalos.rules

# sr (block) device
SUBSYSTEM=="block", KERNEL=="sr*", ATTRS{serial}=="YOUR_DRIVE_SERIAL", SYMLINK+="strophalos/disc"

# sg (scsi_generic) device
SUBSYSTEM=="scsi_generic", KERNEL=="sg*", ATTRS{serial}=="YOUR_DRIVE_SERIAL", SYMLINK+="strophalos/sg"
```

Find your drive's serial with `udevadm info --query=all --name=/dev/sr0 | grep SERIAL`. Then pass `/dev/strophalos/disc` and `/dev/strophalos/sg` in your compose file instead of the raw device numbers.

## How Auto-Rip Works

1. **Disc detection** -- The main loop polls `DEVICE` every `POLL_INTERVAL` seconds using a Python `ioctl(CDROM_DRIVE_STATUS)` call (opcode `0x5326`). This returns the drive status without spinning up the disc or causing any drive activity.

2. **Full scan** -- Once a disc is detected (status = 4, disc OK), `makemkvcon -r info disc:0` runs a full scan. The `disc:0` mode (as opposed to `disc:9999` quick scan) is required because UHD discs need the complete AACS2 handshake to be identified and decrypted.

3. **Disc type routing** -- The scan returns drive flags: `0` means audio CD, anything else means video. Audio CDs are routed to whipper; video discs go to the smart ripper.

4. **Smart title classification** (video discs) -- `rip-video.py` scores the disc against both movie and TV patterns simultaneously and picks the higher-scoring classification:
   - **Movie signals**: one dominant title (high duration ratio vs second-longest), feature length (>= 1 hour), short extras, few non-trivial extras (movies typically have 0-3, not 9).
   - **TV signals**: cluster of similar-duration titles (low coefficient of variation), episode count (more = stronger), play-all detection (longest ≈ sum of cluster), typical episode length (20-65 min).
   - **Title search**: if `TMDB_API_KEY` is set, the disc label is searched on TMDb and the top results' media types bias the score (+0.3 to whichever type dominates).
   - **Fallback**: if scores are tied, all titles >= 2 minutes are ripped.

5. **Media type sorting** -- Output is pre-sorted into `dvd/`, `bd/`, or `uhd/` based on disc type from the makemkvcon scan (CINFO disc type string).

6. **Audio CD ripping** -- `rip-cd.sh` queries MusicBrainz via `discid` to identify the disc, detects multi-disc releases (adjusting the output path template to include `Disc N/`), and hands off to `whipper cd rip` for accurate, bit-perfect extraction to FLAC. Disc designations like `(Disc 1 of 3)` are stripped from the top-level directory so multi-disc releases cluster together.

7. **Library identification** (background) -- After ripping, the disc is identified and hard-linked into the library. The disc ejects immediately; identification runs in the background while the next disc starts ripping. See below.

8. **Post-rip hooks** -- Hooks in `/config/hooks/` are executed with disc metadata as arguments.

9. **Eject** -- If `EJECT_ON_COMPLETE=1`, the disc is ejected and the loop resets, ready for the next disc.

## Identification

All identification runs asynchronously after rip — the disc ejects and the next rip can start immediately. Logs in `/config/logs/identify-*.log`, manifests in each archive disc directory.

### Movies

Straightforward. Disc label is searched on TMDb, canonical title + year is resolved, main feature (largest file) is hard-linked to `movies/{Title} ({year})/`. Extras are linked alongside.

### Audio CDs

Straightforward. Whipper queries MusicBrainz using the disc's TOC hash — this is a deterministic match, not a heuristic. Multi-disc releases are detected and organized into `Disc N/` subdirectories.

### TV Shows

This is the hard one 🥴. Discs have no episode metadata — just numbered titles. Matching layers, in order:

1. **OpenSubtitles hash** -- file hash lookup against the OpenSubtitles database. Definitive when a match exists, but niche releases (anime BDs, indie media) are often missing. Requires [OpenSubtitles VIP](https://www.opensubtitles.com/en/consumers) ($20/yr) for subtitle downloads used in text matching.
2. **OpenSubtitles text matching** -- download reference SRTs for the series, OCR the disc's PGS subtitles via [pgsrip](https://github.com/ratoaq2/pgsrip), compare dialog text to identify episodes. Most reliable fallback when hashes miss.
3. **TMDb episode data** -- DVD/BD episode group ordering when available, standard season ordering as fallback. Duration matching narrows candidates.
4. **Forward-order fallback** -- when `ASSUME_DISC_ORDER=true` (default), assumes sequential disc insertion and assigns the next batch of episodes in order. Each disc picks up where the last left off.

**Constraint**: discs must be ripped in sequential order (disc 1, then 2, etc.) for the forward-order fallback to work. This is the default and handles the common case well.

If TMDb doesn't have an entry for your disc, you'll get an error notification. The raw rip is safe in the archive — add the TMDb entry and re-run:

```sh
docker exec strophalos identify-episodes.py --dir /media/archive/tv/rips/bd/LABEL/disc1 --label LABEL
```

## Hooks

Hook scripts live in `/config/hooks/` and are seeded from defaults on first run. Edit or replace them to customize behavior.

### `disc_rip_terminated.sh`

Called after a video disc rip completes (success or failure).

```
Arguments:
  $1  drive_id     MakeMKV drive index (e.g. "0")
  $2  disc_label   Disc volume label
  $3  output_dir   Path where MKV files were written
  $4  status       "SUCCESS" or "FAILURE"
```

The default implementation is a no-op — media type sorting (`dvd/`, `bd/`, `uhd/`) is handled at rip time by `rip-video.py`. Override this hook for custom post-processing like notifications or transcoding.

### `disc_rip_skipped.sh`

Called when a disc is skipped.

```
Arguments:
  $1  drive_id     MakeMKV drive index
  $2  disc_label   Disc volume label
  $3  reason       One of: ALREADY_PROCESSED, NOT_VIDEO_DISC, SERVICE_FIRST_RUN
```

## Companion: tinyMediaManager

[tinyMediaManager](https://www.tinymediamanager.org/) pairs well with strophalos for organizing rips. Point TMM at the output directories to scrape metadata from IMDB, TMDB, and TVDB, rename files to Plex/Jellyfin conventions, and download artwork.

## Building

```sh
docker build -t strophalos .
```

To pin a specific MakeMKV version:

```sh
docker build --build-arg MAKEMKV_VERSION=1.18.3 -t strophalos .
```
