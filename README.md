# makemkv-1.17.6-flash

Tooling branch for flashing the ASUS BW-16D1HT with LibreDrive (WM01601) firmware on Linux over SATA.

## The Problem

The BW-16D1HT's stock 3.10 firmware has a broken TEST UNIT READY implementation — it always returns `Good` status regardless of whether a disc is present. This causes `makemkvcon f rawflash` to refuse to flash, believing a disc is inserted when the tray is empty.

Additionally, `makemkvcon` versions newer than 1.17.7 have a bug on Linux where sdftool operations (dump, rawflash) hang at 100% CPU with zero I/O after the initial SCSI handshake.

## What Didn't Work

- **`makemkvcon` 1.18.3** (system package): hangs at 100% CPU during any `f dump` or `f rawflash` operation. Known Linux bug in versions >= 1.17.8.
- **Building 1.17.6 natively on Arch**: fails to compile against current ffmpeg (7.x) and Qt5 headers. `FF_PROFILE_UNKNOWN` renamed to `AV_PROFILE_UNKNOWN`, `avcodec_close` removed entirely, Qt deprecation errors.
- **`sg_write_buffer --mode=7`**: drive reports buffer capacity of 0. Firmware is only accessible through vendor-specific SCSI commands (READ/WRITE BUFFER mode 6), not standard SCSI microcode download.
- **`sg_read_buffer --mode=2`**: same issue — zero buffer capacity reported.
- **Passing `-f /tmp/sdf.bin`** to makemkvcon: doesn't fix the 1.18.3 hang (the hang is in userspace code, not drive communication) and doesn't fix the TUR false positive on 1.17.6.
- **Power cycling the drive**: doesn't reset the broken TUR behavior.
- **Ejecting/closing tray**: no effect on TUR response.
- **`--all-yes` flag**: the "disc in drive" check is a hard abort, not a prompt.
- **Using `/dev/sg0` instead of `/dev/sr0`**: makemkvcon says "Drive not found" when given the sg device.

## What Worked

1. **Dockerfile pinned to makemkv 1.17.6** (built on `debian:bookworm-slim` where ffmpeg/Qt are old enough to compile cleanly).
2. **LD_PRELOAD shim (`tur_patch.c`)** that intercepts `ioctl(SG_IO)` and patches TEST UNIT READY responses from `Good` to `CHECK CONDITION / NOT READY / MEDIUM NOT PRESENT`.

### Firmware Dump (backup)

```bash
# Download SDF definition file
curl -o /tmp/sdf.bin https://www.makemkv.com/sdf.bin

docker build --network=host -t strophalos:flash .

docker run --rm -it \
  --device /dev/sr0 --device /dev/sg0 \
  -v /tmp/sdf.bin:/tmp/sdf.bin:ro \
  -v /tmp/fw_backup:/out \
  strophalos:flash \
  makemkvcon f -f /tmp/sdf.bin -d /dev/sr0 dump -o /out
```

### Firmware Flash

```bash
# Compile the TUR patch shim
gcc -shared -fPIC -o /tmp/tur_patch.so tur_patch.c -ldl

docker run --rm -it \
  --device /dev/sr0 --device /dev/sg0 \
  -v /tmp/sdf.bin:/tmp/sdf.bin:ro \
  -v /tmp/tur_patch.so:/tmp/tur_patch.so:ro \
  -v /path/to/ASUS-BW-16D1HT-3.10-WM01601-211901041014.bin:/tmp/firmware.bin:ro \
  strophalos:flash \
  env LD_PRELOAD=/tmp/tur_patch.so makemkvcon f -f /tmp/sdf.bin -d /dev/sr0 rawflash enc -i /tmp/firmware.bin
```

## Drive Details

- **Model**: ASUS BW-16D1HT
- **Chipset**: MediaTek MT1959
- **Serial**: YOUR_SERIAL
- **Connection**: SATA (direct, not USB)
- **Stock firmware**: 3.10 (build 211901041014)
- **Target firmware**: 3.10 WM01601 (LibreDrive patched, same build date)

## SCSI Quirks

The stock firmware's SCSI implementation is janky:

- **TEST UNIT READY**: always returns `Good`, even with tray open or no disc
- **ATA IDENTIFY**: returns mostly zeroed data (no model string, no serial, no features)
- **READ BUFFER mode 3** (descriptor): reports buffer capacity of 0
- **Vendor opcode 0xFF**: returns 512 bytes of zeros (but doesn't hang)
- **Standard commands** (INQUIRY, GET CONFIGURATION, READ BUFFER mode 6): work correctly

All vendor-specific SCSI commands pass through SATA/libata fine on this drive (it's ATAPI, so CDBs go through ATA PACKET, not SAT translation). The makemkvcon 1.18.x hang is a software bug, not a kernel/transport issue.

## udev Rules

See `99-strophalos.rules` — adapted for SATA (uses `ENV{ID_SERIAL_SHORT}` instead of `ATTRS{serial}` since SATA doesn't expose serial in the device attribute hierarchy). Requires `modprobe sg` for the sg device.

## Files

- `Dockerfile` — pinned to makemkv 1.17.6, downloads from `/old/` archive
- `tur_patch.c` — LD_PRELOAD shim to fix broken TUR responses
- `99-strophalos.rules` — udev rules for stable `/dev/strophalos/` symlinks
