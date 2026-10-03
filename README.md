# code-server-envbox

Container images for a single code-server workspace on Talos, using
[Coder Envbox](https://github.com/coder/envbox) and Sysbox instead of KVM.
This repository owns image sources and tests. Kubernetes, OIDC, DNS, CSI,
Tailnet policy and storage declarations belong to the deployment repository.

**Status: integration under development; production has not been switched.**

```
Web -> Envoy + OIDC -> Envbox -> inner code-server :8080
SSH -> native Tailnet SSH ACL -> restricted relay -> inner coder
SFTP -> native Tailnet SSH ACL -> shared home and /data

Envbox (privileged outer container)
  `- Sysbox workspace (unprivileged, user namespace)
       |- code-server, coder with sudo
       |- Docker daemon, Buildx, Compose
       |- /home/coder -> persistent workspace home
       `- /data       -> provider-managed S3 CSI mount
```

The outer container requires privilege and node user namespaces. It must not
receive a Kubernetes token, host runtime socket, host paths or application
credentials. Root in the inner workspace maps to outer UID 100000; coder maps
to 101000. This design does not offer a VM security boundary.

## Images

| Dockerfile | Purpose |
| --- | --- |
| `Dockerfile.outer` | Digest-pinned Envbox with the managed-mount patch and lifecycle helpers |
| `Dockerfile.workspace` | code-server, inner Docker, Compose, SSH and passwordless sudo |
| `Dockerfile.tailnet` | Native Tailscale SSH/SFTP sidecar and shell relay |

Users install, activate and update mise themselves in their persistent home.
The image does not preinstall mise or extra language toolchains and package
manager tools. Project/user configuration selects tool versions without rebuilding
the workspace image. User-installed tools and their configuration persist in
home.

System prerequisites include the native compiler and build utilities, plus
development libraries for TLS/FFI, compression, SQLite/PostgreSQL, readline,
ncurses, XML/ICU, fonts and images. Docker CLI/daemon, Buildx, Compose, SSH and
sudo are part of the workspace runtime. Basic file and network utilities are
also included. Only the workspace image contains these system dependencies.

The mount patch adds `:managed` and `:managed-ro`: Envbox leaves provider-owned
mount permissions unchanged. Sysbox checks remain enabled. S3 CSI must present
UID/GID 100000, directories 0770 and files 0660. The inner coder belongs to
group 0, mapped to outer group 100000. The Tailnet sidecar has supplementary
group 100000. No object ownership metadata is rewritten.

The shell relay uses a Pod-local ephemeral key and pins the inner SSH host key.
Tailnet still authorizes login as `coder`; it does not authorize root login.
Native SFTP sees shared home and `/data`, but its system directories belong to
the sidecar. The sidecar never receives the outer Docker socket.

## Build and test

Requires Linux amd64 Docker with privileged containers and enabled user
namespaces, plus Python with `docker` (`uv` can install it transiently).

```sh
bash scripts/build.sh
uv run --with docker tests/envbox-integration.py
```

Tests use a disposable Headscale instance, synthetic enrollment keys and a
local anonymous registry; they need no real credentials. They verify native
SSH authorization, inner Docker, exact exit codes, TTY, SFTP and a recoverable
home migration. Temporary containers, volumes and networks are removed.

`migrate-home.py` copies an existing home into `.envbox/home` and shifts the
copy's ownership. It preserves the original home for rollback. Docker and
Sysbox state live under `.envbox/`. Unix sockets are recreated by applications.
System package changes can be reproduced by a user-managed
`~/.config/workspace/startup.sh`, executed as root inside the workspace.

## Publishing

The GitHub Actions build workflow builds and tests images without publishing.
The separate manual publish workflow publishes revision-tagged GHCR images
only from `main`, after the same checks pass. Deploy by digest, not `latest`.

New GHCR packages default to private even for public source repositories.
An owner must make each package public in its package settings before the
cluster can pull anonymously. Verify anonymous pulls before deployment.

## License and upstream

The Envbox-derived patch is AGPL-3.0, matching upstream; the license is included.
Upstream source is pinned to `b2944061961598353a6a12d9ee01af662c9ab6cf` and
verified with SHA-256 during the build. The runtime also includes separately
licensed Sysbox, Docker, Tailscale, OpenSSH and code-server components.
