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

# Runtime deps + whipper build
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-gi \
        python3-musicbrainzngs \
        python3-mutagen \
        python3-pil \
        python3-ruamel.yaml \
        flac \
        libsndfile1 \
        libdiscid0 \
        cdrdao \
        cdparanoia \
        eject \
        sg3-utils \
        libavcodec59 \
        libssl3 \
        # build deps for whipper
        python3-dev \
        python3-setuptools \
        gcc \
        libdiscid-dev \
        libsndfile1-dev \
        swig \
        git \
        patch \
        pkg-config \
    && pip3 install --break-system-packages \
        discid \
        pycdio \
    && git clone --depth 1 https://github.com/whipper-team/whipper.git /tmp/whipper \
    && cd /tmp/whipper \
    && patch -p1 < /tmp/ambiguous-release.patch \
    && pip3 install --break-system-packages . \
    && cd / \
    && rm -rf /tmp/whipper /tmp/ambiguous-release.patch \
    # Clean up build deps
    && apt-get purge -y python3-dev gcc swig git patch pkg-config libdiscid-dev libsndfile1-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/cd-paranoia /usr/bin/cd-paranoia 2>/dev/null || true

# Scripts, config, and default hooks
COPY config/whipper.conf /defaults/whipper.conf
COPY hooks/ /defaults/hooks/
COPY scripts/ /usr/local/bin/
RUN chmod +x /usr/local/bin/*

ENV HOME=/config

VOLUME ["/config", "/output", "/output-cd"]

ENTRYPOINT ["/usr/local/bin/auto-rip.sh"]
