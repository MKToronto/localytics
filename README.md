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

Requires [`uv`](https://docs.astral.sh/uv/) — that's the only tool you need;
Python, the venv, and every dependency are fetched on demand via the PEP 723
header at the top of `server/local_server.py`.

1. **Configure.** Copy the template and fill it in:
   ```bash
   cp helpers/config.example.json helpers/config.json
   ```
   At minimum set `LOCAL_API_KEY`, `CLOUD_API_KEY` (matching random hex strings
   shared with the dashboard), `CODE_PATH`, and `REPO_PATH`.
   `CLOUD_SERVER_URL` stays as the placeholder until step 3 is done.
   `helpers/config.json` is gitignored so your real keys never commit.

2. **Run the local server:**
   ```bash
   uv run server/local_server.py
   ```
   First run downloads Python 3.12 + deps into uv's cache (~30–60s), then
   starts on port `51515`. TLS is on if `SSL_KEYFILE` / `SSL_CERTFILE` in
   `config.json` point at a valid cert pair; otherwise plain HTTP.

3. **Deploy the cloud dashboard to Render.**

   1. Push this repo to GitHub (public or private — Render's GitHub App can
      read private repos you grant it access to).
   2. Sign up at https://render.com and connect your GitHub repo. (Or use
      Render's "Public Git repository" option — no GitHub App needed, but
      you lose auto-deploy on push.)
   3. **Render → + New → Key Value.** Name it, pick a region, Free plan.
      Once provisioned, open the service, copy the **Internal Redis URL**
      (starts with `redis://red-…`).
   4. **Render → + New → Web Service.** Pick the `localytics` repo, then:

      | Field | Value |
      |---|---|
      | Root Directory | `dashboard` |
      | Runtime | Python 3 |
      | Build Command | `pip install -r requirements.txt` |
      | Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
      | Plan | Free |

      Under **Environment Variables** add four entries:

      | Key | Value |
      |---|---|
      | `LOCAL_API_KEY` | same string as in your local `helpers/config.json` |
      | `CLOUD_API_KEY` | same string as in your local `helpers/config.json` |
      | `LOCAL_SERVER_PORT` | `51515` |
      | `REDIS_URL` | Internal Redis URL from step 3.3 |

   5. When the deploy log shows `Uvicorn running on …` and no tracebacks,
      open the public URL. You should see the dashboard UI with empty
      placeholders — correct empty state.

4. **Connect local → cloud.** Back in `helpers/config.json`:
   ```json
   "CLOUD_SERVER_URL": "https://your-dashboard.onrender.com"
   ```
   (no trailing slash). Restart `uv run server/local_server.py` — the
   backfill runs, the push loop fires, and the Render dashboard populates.

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

Three launcher options, all `uv`-based out of the box. If you prefer conda,
`server/environment.yaml` has the dep list and each launcher is a few lines
long — edit to taste.

**Detached background run** (screen session, survives closing the terminal):
```bash
./server/start_localytics.sh
screen -r localytics_server   # attach to view logs; Ctrl-A d to detach
```

**Double-clickable launcher.** Copy the template, make it executable, then
double-click it in Finder:
```bash
cp helpers/run_localytics.command.example ~/run_localytics.command
chmod +x ~/run_localytics.command
```

**LaunchAgent** (starts automatically at login):
```bash
cp helpers/com.localytics.server.plist.example ~/Library/LaunchAgents/com.localytics.server.plist
# edit the plist to point at the absolute path of this repo, then:
launchctl load ~/Library/LaunchAgents/com.localytics.server.plist
```

## License

MIT — see [LICENSE](LICENSE).
