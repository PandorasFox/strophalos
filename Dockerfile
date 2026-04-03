# strophalos — automatic disc ripper
# Debian base, makemkv built from source, whipper for audio CDs.
# No GUI — just rips.

ARG MAKEMKV_VERSION=1.18.3

FROM debian:bookworm-slim AS makemkv-build

ARG MAKEMKV_VERSION

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

# --- Runtime image ---
FROM debian:bookworm-slim

# Copy makemkv from build stage
COPY --from=makemkv-build /usr/bin/makemkvcon /usr/bin/
COPY --from=makemkv-build /usr/lib/lib*.so* /usr/lib/
COPY --from=makemkv-build /usr/share/MakeMKV /usr/share/MakeMKV

# whipper patch
COPY patches/ambiguous-release.patch /tmp/ambiguous-release.patch

# All deps in one layer: runtime + build whipper + clean up
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
        mkvtoolnix \
        tesseract-ocr \
        tesseract-ocr-eng \
        libavcodec59 \
        libssl3 \
        gcc \
        git \
        patch \
    && pip3 install --break-system-packages discid pgsrip apprise \
    && git clone --depth 1 https://github.com/whipper-team/whipper.git /tmp/whipper \
    && cd /tmp/whipper \
    && patch -p1 < /tmp/ambiguous-release.patch \
    && pip3 install --break-system-packages --no-deps . \
    && cd / \
    && rm -rf /tmp/whipper /tmp/ambiguous-release.patch \
    && ln -s /usr/bin/cdparanoia /usr/bin/cd-paranoia \
    && apt-get purge -y python3-dev python3-pip gcc git patch libsndfile1-dev \
    && apt-get autoremove --purge -y \
    && rm -rf /var/lib/apt/lists/*

# Scripts, config, and default hooks
COPY config/whipper.conf /defaults/whipper.conf
COPY hooks/ /defaults/hooks/
COPY scripts/ /usr/local/bin/
RUN chmod +x /usr/local/bin/*

ENV HOME=/config

VOLUME ["/config", "/media", "/output-cd"]

ENTRYPOINT ["/usr/local/bin/auto-rip.sh"]
