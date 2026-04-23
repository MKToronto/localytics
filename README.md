# localytics

A self-hosted code-progress dashboard. A local FastAPI server analyses a
codebase on your machine (commit activity, cyclomatic complexity, file-type
mix) and pushes the results to a small cloud dashboard you can hit from
anywhere.

## Layout

```
localytics/
├── server/      Local FastAPI server that scans your code and serves metrics
├── dashboard/   Cloud FastAPI app that caches and displays the metrics
└── helpers/     Config template + macOS launchers (LaunchAgent / .command)
```

## Quick start

1. **Configure.** The server loads its config from **outside** the repo so
   secrets never land in the working tree. Default location is
   `~/.config/localytics/config.json`; override with the `LOCALYTICS_CONFIG`
   env var.
   ```bash
   mkdir -p ~/.config/localytics
   cp helpers/config.example.json ~/.config/localytics/config.json
   ```
   At minimum set `LOCAL_API_KEY`, `CLOUD_API_KEY` (matching random hex strings
   shared with the dashboard), `CODE_PATH`, and `REPO_PATH`.

2. **Install dependencies.** Pick one:

   **uv** (fast, recommended):
   ```bash
   uv venv
   source .venv/bin/activate
   uv pip install -r server/requirements.txt
   ```

   **conda** (matches the macOS launchers in `helpers/`):
   ```bash
   conda env create -f server/environment.yaml
   conda activate localytics
   ```

3. **Run the local server:**
   ```bash
   python server/local_server.py
   ```
   Default port is `51515`. TLS is on if `SSL_KEYFILE` / `SSL_CERTFILE` in
   `config.json` point at a valid cert pair; otherwise it runs HTTP.

4. **Deploy the dashboard** (optional). `dashboard/` is a standard FastAPI
   app meant for Render (`dashboard/render.yaml`). It needs these env vars:
   `LOCAL_API_KEY`, `CLOUD_API_KEY`, `LOCAL_SERVER_PORT`, `REDIS_URL`.

## Workflow: local codebase vs. GitHub repo

The main use case is keeping the code on your machine — nothing leaves except
aggregated metrics — but localytics will also analyse a public GitHub repo
if you'd rather not keep the code locally.

**Local code** — set `REPO_PATH` to an absolute path on your machine and
`CODE_PATH` to the specific subtree to analyse (absolute path, normally
inside `REPO_PATH`).

```json
"REPO_PATH": "/Users/you/code/myproject",
"CODE_PATH": "/Users/you/code/myproject/src"
```

**GitHub repo** — set `REPO_PATH` to a git URL. On startup, the local server
clones the repo into `.cache/<repo-name>/` at the root of this project
(`git pull`ed on subsequent runs). `CODE_PATH` is a sub-path within the
clone, same as local mode — give it relative to the clone root.

```json
"REPO_PATH": "https://github.com/tiangolo/fastapi.git",
"CODE_PATH": "fastapi"
```

The `git pull` only happens when `local_server.py` starts. If you want the
tree refreshed on a schedule, run the server on a schedule — use the macOS
LaunchAgent / `.command` templates in `helpers/` (see below).

## macOS auto-start (optional)

Templates live in `helpers/`. Fill them in **outside** this repo so your
machine-specific paths never leak into the working tree:

```bash
# Double-clickable launcher
cp helpers/run_localytics.command.example ~/run_localytics.command
chmod +x ~/run_localytics.command
# then edit ~/run_localytics.command

# LaunchAgent for login-time startup
cp helpers/com.localytics.server.plist.example ~/Library/LaunchAgents/com.localytics.server.plist
# edit the absolute paths in that file, then:
launchctl load ~/Library/LaunchAgents/com.localytics.server.plist
```

## License

MIT — see [LICENSE](LICENSE).
