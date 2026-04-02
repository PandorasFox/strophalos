# strophalos

Automatic disc ripper in a headless Docker container. Insert a disc, walk away, come back to MKV files (video) or FLAC files (audio CD).

uses makemkv (req. product key for >30d of use) for video discs and whipper for audio CDs.

UHD blu-ray ripping requires a LibreDrive compatible drive. go check out the makemkv forums :)

## Features

- **Headless** -- no GUI, no VNC, no browser. Just a polling loop and `makemkvcon`.
- **Smart title selection** -- classifies discs as movie or TV and skips play-all playlists, menus, and junk titles automatically.
- **Audio CD ripping** via [whipper](https://github.com/whipper-team/whipper) with MusicBrainz lookup, multi-disc detection, and accurate ripping.
- **UHD / 4K Blu-ray support** -- full `disc:0` scan for AACS2 handshake on UHD media.
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

## Volumes

| Mount point | Description |
|---|---|
| `/config` | Persistent configuration. Contains MakeMKV `settings.conf`, whipper config (`~/.config/whipper/whipper.conf`), and hook scripts (`hooks/`). Seeded with defaults on first run. |
| `/output` | Video disc rip output. MKV files are written here, organized by disc label. The default `disc_rip_terminated` hook further sorts into `dvd/`, `bluray/`, and `uhd/` subdirectories. |
| `/output-cd` | Audio CD rip output. FLAC files are written here by whipper, organized as `Artist - Album/Track. Title.flac`. Multi-disc releases get a `Disc N/` subdirectory. |

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

4. **Smart title classification** (video discs) -- `rip-video.py` analyzes all titles on the disc and classifies it:
   - **Movie**: one title >= 1 hour, everything else less than half its length. Only the main feature is ripped; extras are skipped.
   - **TV**: a cluster of 3+ titles with similar durations (within 50-200% of the median, >= 10 min each). If the longest title's duration approximately equals the sum of the cluster (within 10%), it is identified as a play-all playlist and excluded.
   - **Fallback**: if neither pattern matches, all titles >= 2 minutes are ripped.

5. **Audio CD ripping** -- `rip-cd.sh` queries MusicBrainz via `discid` to identify the disc, detects multi-disc releases (adjusting the output path template to include `Disc N/`), and hands off to `whipper cd rip` for accurate, bit-perfect extraction to FLAC.

6. **Post-rip hooks** -- After ripping completes, hooks in `/config/hooks/` are executed with disc metadata as arguments.

7. **Eject** -- If `EJECT_ON_COMPLETE=1`, the disc is ejected and the loop resets, ready for the next disc.

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

The default implementation sorts rips into `/output/dvd/`, `/output/bluray/`, or `/output/uhd/` based on the drive's MMC profile (via `sg_get_config`), using output size to distinguish UHD from standard Blu-ray (>= 50 GB = UHD).

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
