# Isambard

Server-side downloader stack with:

- A FastAPI web UI for running, queued, and completed downloads
- A separate Guacamole stack (`guacamole` + `guacd`) in front of a remote Chromium desktop
- Embedded Guacamole browser access over a dedicated browser endpoint
- A preloaded local extension inside the remote Chromium browser that captures `.m3u8` streams on `https://yflix.to/watch/*`
- A queue backend that downloads selected streams through `yt-dlp`
- GitHub Actions packaging for downloadable Docker image archives
- A Helm chart for future k3s deployment

## Run With Docker Compose

`compose.yaml` in this repo now contains only the Isambard deployment stack:

- `isambard-app`
- `isambard-guacd`
- `isambard-guacamole`
- `isambard-browser`

Reverse proxy, DNS, Cloudflare tunnel, VPN, and other host-level services should stay outside this repo.

Copy the sample environment first:

```bash
cp .env.example .env
```

```bash
docker compose build
docker compose up
```

Then open:

- App UI: `http://localhost:8765`
- Guacamole: `http://localhost:${ISAMBARD_BROWSER_PORT:-8766}`

Default Guacamole login:

- User: `guac`
- Password: `guac`

## GitHub Actions Image Builds

`.github/workflows/build-images.yml` builds both Docker images for `linux/amd64`, bundles them into a single archive, and publishes:

- a workflow artifact named `isambard-images-linux-amd64`
- release assets on tagged builds

That gives `penzance` a simple pull path:

```bash
wget https://github.com/towerundersiege/isambard/releases/download/<tag>/isambard-images-linux-amd64.tar.gz
gunzip -c isambard-images-linux-amd64.tar.gz | docker load
```

## Helm

The chart lives in `charts/isambard` and packages the current stack for Kubernetes:

- `app`
- `browser`
- `guacd`
- `guacamole`
- optional `gluetun` sidecar in the same pod

Example:

```bash
helm upgrade --install isambard ./charts/isambard \
  --set ingress.app.host=isambard.example.com \
  --set ingress.browser.host=isambard-browser.example.com
```

## Notes

- The app service is reachable inside Docker as `http://app:8765`, which the extension uses when queueing streams.
- Downloads are written to `./downloads` on the host through the mounted volume.
- Guacamole official docs: https://guacamole.apache.org/doc/1.5.4/gug/guacamole-docker.html
