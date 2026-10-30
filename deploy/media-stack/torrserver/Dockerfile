FROM golang:1.27.1-alpine@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414 AS builder
ARG UPSTREAM_COMMIT=d84f41968780744d4b8643f7a73b22a84d2c3779
ENV GOMAXPROCS=2 CGO_ENABLED=0
WORKDIR /src
RUN wget -qO /tmp/upstream.tar.gz "https://codeload.github.com/YouROK/TorrServer/tar.gz/${UPSTREAM_COMMIT}" \
 && tar -xzf /tmp/upstream.tar.gz --strip-components=1 -C /src \
 && rm /tmp/upstream.tar.gz
COPY patches/0001-index-saved-torrents.patch /tmp/fix.patch
RUN patch -p1 < /tmp/fix.patch
COPY tests/torrent_index_test.go /src/server/settings/torrent_index_test.go
WORKDIR /src/server
RUN go test ./settings -run TestIndexedTorrent -count=1 \
 && go build -p 2 -ldflags '-s -w -checklinkname=0 -X server/version.Version=MatriX.147.1-home-dbindex1' -o /tmp/torrserver ./cmd
FROM ghcr.io/yourok/torrserver@sha256:c965123d980d8a3ba2d3b56755110579e09e812a283fa4f8d2a86219bc00d159
COPY --from=builder /tmp/torrserver /usr/bin/torrserver
