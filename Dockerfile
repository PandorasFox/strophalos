# strophalos — automatic disc ripper
# Debian base, makemkv built from source, whipper for audio CDs.
# No GUI — just rips.

ARG MAKEMKV_VERSION=1.18.3
ARG LIBDVDCSS_VERSION=1.4.3

# --- Build stage: makemkv + libdvdcss ---
FROM debian:bookworm-slim AS build

ARG MAKEMKV_VERSION
ARG LIBDVDCSS_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libc6-dev \
        libssl-dev \
        libexpat1-dev \
        libavcodec-dev \
        zlib1g-dev \
        wget \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Build makemkv
RUN wget -q "https://www.makemkv.com/download/makemkv-oss-${MAKEMKV_VERSION}.tar.gz" \
    && wget -q "https://www.makemkv.com/download/makemkv-bin-${MAKEMKV_VERSION}.tar.gz" \
    && tar xzf "makemkv-oss-${MAKEMKV_VERSION}.tar.gz" \
    && tar xzf "makemkv-bin-${MAKEMKV_VERSION}.tar.gz" \
    && cd "makemkv-oss-${MAKEMKV_VERSION}" \
    && ./configure --disable-gui \
    && make -j$(nproc) \
    && make install \
    && cd "../makemkv-bin-${MAKEMKV_VERSION}" \
    && mkdir tmp && touch tmp/eula_accepted \
    && make install

# Build libdvdcss (CSS decryption for DVD ripping without SCSI/makemkvcon)
RUN wget -q "https://download.videolan.org/pub/libdvdcss/${LIBDVDCSS_VERSION}/libdvdcss-${LIBDVDCSS_VERSION}.tar.bz2" \
    && tar xjf "libdvdcss-${LIBDVDCSS_VERSION}.tar.bz2" \
    && cd "libdvdcss-${LIBDVDCSS_VERSION}" \
    && ./configure --prefix=/usr \
    && make -j$(nproc) \
    && make install

# --- Runtime image ---
FROM debian:bookworm-slim

# Copy makemkv + libdvdcss from build stage
COPY --from=build /usr/bin/makemkvcon /usr/bin/
COPY --from=build /usr/lib/lib*.so* /usr/lib/
COPY --from=build /usr/share/MakeMKV /usr/share/MakeMKV

RUN ldconfig

# whipper patch + strophalos package source
COPY patches/ambiguous-release.patch /tmp/ambiguous-release.patch
COPY pyproject.toml /tmp/strophalos/pyproject.toml
COPY src/ /tmp/strophalos/src/

# All deps in one layer: runtime + build whipper + install strophalos + clean up
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-gi \
        python3-musicbrainzngs \
        python3-mutagen \
        python3-pil \
        python3-ruamel.yaml \
        python3-setuptools \
        python3-packaging \
        python3-libdiscid \
        python3-cdio \
        python3-dev \
        flac \
        libsndfile1 \
        libsndfile1-dev \
        libdiscid0 \
        cdrdao \
        cdparanoia \
        sox \
        eject \
        sg3-utils \
        dvdbackup \
        lsdvd \
        util-linux \
        mkvtoolnix \
        tesseract-ocr \
        tesseract-ocr-eng \
        libavcodec59 \
        libssl3 \
        gcc \
        git \
        patch \
    && pip3 install --break-system-packages pgsrip \
    && git clone --depth 1 https://github.com/whipper-team/whipper.git /tmp/whipper \
    && cd /tmp/whipper \
    && patch -p1 < /tmp/ambiguous-release.patch \
    && pip3 install --break-system-packages --no-deps . \
    && cd / \
    && pip3 install --break-system-packages /tmp/strophalos \
    && rm -rf /tmp/whipper /tmp/ambiguous-release.patch /tmp/strophalos \
    && ln -s /usr/bin/cdparanoia /usr/bin/cd-paranoia \
    && apt-get purge -y python3-dev python3-pip gcc git patch libsndfile1-dev \
    && apt-get autoremove --purge -y \
    && rm -rf /var/lib/apt/lists/*

# Shell orchestrator, config, and default hooks
COPY config/whipper.conf /defaults/whipper.conf
COPY hooks/ /defaults/hooks/
COPY scripts/auto-rip.sh /usr/local/bin/auto-rip.sh
RUN chmod +x /usr/local/bin/auto-rip.sh

ENV HOME=/config

VOLUME ["/config", "/media", "/output-cd"]
# /media is the library root: archive/tv/rips, archive/movies/rips, library/tv, library/movies

CMD ["/usr/local/bin/auto-rip.sh"]
