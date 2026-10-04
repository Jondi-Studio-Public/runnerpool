# Third-party notices

runnerpool itself is MIT licensed (see `LICENSE`). Nothing from other projects is vendored in this
repository (no copied source, no `.claude/` skills, no bundled binaries). The sections below list what the
project depends on, downloads or ships in a build output, and design inspiration. Entries marked **verify**
are from memory of upstream's licence and should be confirmed against the upstream repository before a release.

## Shipped in a build output

### Tailscale (BSD-3-Clause)

The Mac installer package (`git-runner-mac.pkg`) contains the `tailscale` and `tailscaled` binaries, built
from source with `go install tailscale.com/cmd/{tailscale,tailscaled}@<version>` at package build time. They
are redistributed, so their licence text ships with the package: both builds (`mac/build-local.sh` and `.github/workflows/build.yml`)
download the `LICENSE` file of the exact Tailscale version they build and install it, with this notice, under
`/usr/local/mac-runners/LICENSES/` (`tailscale.txt`, `THIRD_PARTY_NOTICES.md`). The builds fail if the text is missing.

- Source: https://github.com/tailscale/tailscale (the Go module `tailscale.com`)
- Licence: BSD 3-Clause (**verify** the exact copyright line against `LICENSE` in the version you build)

```
BSD 3-Clause License

Copyright (c) 2020 Tailscale Inc & AUTHORS.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

The Go modules Tailscale itself depends on carry their own licences (mostly BSD, MIT and Apache-2.0); when you
distribute a built package, also include the notices `go-licenses` reports for the Tailscale version you build
(**verify**; not automated here).

### PyJWT and cryptography (dashboard and watchdog container image)

`dashboard/requirements.txt` installs `PyJWT[crypto]`, which pulls in `cryptography`, into the container image.

- PyJWT: MIT. https://github.com/jpadilla/pyjwt
- cryptography: Apache-2.0 OR BSD-3-Clause (dual). https://github.com/pyca/cryptography
  Its wheels also bundle OpenSSL (Apache-2.0 for OpenSSL 3.x; **verify** the version in the wheel you install).
  If you publish the image, include these licence texts (the wheels carry them in their `.dist-info/`).

## Downloaded or installed at run time (not redistributed by this repository)

| Component | Licence | Used for |
| --- | --- | --- |
| GitHub Actions runner (https://github.com/actions/runner) | MIT | Downloaded from GitHub's releases at install time and run as the CI runner on every device |
| GitHub CLI `gh` (https://github.com/cli/cli) | MIT | Used by `runner` and the on-machine tools for API calls |
| zonky embedded-postgres-binaries (https://github.com/zonkyio/embedded-postgres-binaries) | Apache-2.0 (the repository); the jars it publishes bundle PostgreSQL, under the PostgreSQL Licence | `runner postgres` downloads a pinned, sha256-checked Apple Silicon PostgreSQL 16 build from Maven Central into `/opt/postgres16`. The PostgreSQL licence file travels inside the downloaded jar and stays with the install. |
| Colima and Lima (https://github.com/abiosoft/colima, https://github.com/lima-vm/lima) | MIT; Apache-2.0 | Downloaded as pinned release binaries for the Mac Linux VM |
| Docker Engine, buildx, compose (Docker's apt repository) | Apache-2.0 | Optional `docker` label on the WSL runners |
| Ubuntu, Python, uv (https://github.com/astral-sh/uv), git | various (GPL, PSF, MIT/Apache-2.0) | Installed by `linux/linux-provision.sh` inside the Linux boxes; not redistributed here |
| ntfy (https://ntfy.sh) | Apache-2.0 / GPL-2.0 dual (**verify**) | Optional phone alerts from the watchdog; used as a service over HTTP |

## Development and CI tools (not shipped)

| Tool | Licence |
| --- | --- |
| ruff (https://github.com/astral-sh/ruff) | MIT |
| pytest | MIT |
| ShellCheck (https://github.com/koalaman/shellcheck) | GPL-3.0 (run as a separate program; no code is incorporated) |
| actionlint (https://github.com/rhysd/actionlint) | MIT |
| PowerShell (`pwsh`) | MIT |
| mkbom / bomutils, xar (built from source to assemble the Mac pkg on Linux) | BSD-style; **verify** exact licences of the versions you build |

## GitHub Actions used in workflows

Referenced by `uses:` and run on the runners; not copied into this repository. Pin versions as the workflows do.

| Action | Licence |
| --- | --- |
| actions/checkout | MIT |
| actions/setup-go | MIT |
| actions/create-github-app-token | MIT |
| astral-sh/setup-uv | MIT (**verify**) |
| dorny/paths-filter | MIT |

## Ideas from other projects (no code copied)

- **MonolithProjects/ansible-github_actions_runner** (MIT, https://github.com/MonolithProjects/ansible-github_actions_runner):
  an idea only, namely automating runner registration and re-registration across many hosts. No code was copied.
- **Actions Runner Controller**: the pattern of minting short-lived GitHub App installation tokens instead of storing a
  long-lived PAT. A pattern, not code.

## Design inspiration: GARM web UI (no code copied)

The dashboard's status pills with a subtle ring and pulse for transitional states, the filter bar with status counts,
sortable column headers, the per-runner detail panel and the shared empty and error states were inspired by the
pool and instance views of the GARM web app.

- Source: https://github.com/cloudbase/garm (directory `webapp/`)
- Commit looked at: c58e7375f457062399c2f56bd8e8d195e04b24ba
- Licence: Apache License 2.0 (root `LICENSE` of that repository,
  https://github.com/cloudbase/garm/blob/main/LICENSE, text at https://www.apache.org/licenses/LICENSE-2.0)

Inspiration only: every line in `dashboard/index.html` was written for this repository in plain JavaScript and CSS.
No GARM source, markup, icons, logos or assets were copied, and no SvelteKit or Tailwind code is included, so no
licence text is bundled under `LICENSES/`. The GARM name and logo are not used. If code from that project is ever
copied here, keep its Apache-2.0 header, note the change, add the full licence text to `LICENSES/Apache-2.0.txt`
and update this file.

Licence notes from the review: `webapp/` has no licence file of its own; its `package.json` says `ISC`, while the
repository root is Apache-2.0. `webapp/static/assets/` holds GARM logos and favicons plus the GitHub mark and a Gitea
logo (third-party trademarks); none of these are used here.
