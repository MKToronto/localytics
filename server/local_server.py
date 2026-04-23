import httpx
import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from fastapi import FastAPI, Header, HTTPException, Request, Depends
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import git
# import lizard
import traceback
import secrets
import time
from radon.complexity import cc_visit
import asyncio
import pandas as pd
import uvicorn
import sys
import subprocess
import signal
import logging
import ipaddress

# Register signal handler
SERVER_PORT = 51515

app = FastAPI()
shared_state = {"should_stop": False}
# Config lives OUTSIDE the repo so secrets never touch the working tree.
# Override location with the LOCALYTICS_CONFIG env var; otherwise defaults to
# ~/.config/localytics/config.json.
CONFIG_FILE = Path(
    os.environ.get("LOCALYTICS_CONFIG")
    or Path.home() / ".config" / "localytics" / "config.json"
)
CSV_FILE = Path(__file__).resolve().parent / "progress_history.csv"
update_metrics_finished = asyncio.Event()  # Create an event to track completion

# Track the number of times each route is accessed
route_activity = {
    "progress": False,
    "complexity_warnings": False,
    "heatmap": False
}


LOG_FILE = Path(__file__).resolve().parent / "process.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# Store the latest calculated metrics
latest_progress_data = {}

def load_config():
    """Loads API key and code path from credentials.json."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    else:
        raise FileNotFoundError(f"Missing credentials file: {CONFIG_FILE}")

config = load_config()
# Load API Key & CODE_PATH
try:
    # Check for API key and fail if missing
    if "LOCAL_API_KEY" not in config or not config["LOCAL_API_KEY"]:
        raise ValueError("LOCAL_API_KEY is missing from configuration")
    
    default_key = secrets.token_urlsafe(32)
    CLOUD_API_KEY = config.get("CLOUD_API_KEY", default_key)
    LOCAL_API_KEY = config.get("LOCAL_API_KEY", default_key)

    CODE_PATH = Path(config.get("CODE_PATH", ".")).resolve()
    REPO_PATH = Path(config.get("REPO_PATH", ".")).resolve()
    FILE_EXTENSIONS = tuple(config.get("filters", {}).get("file_extensions", [".py"]))  # Default to .py if missing
    ALLOWED_IPS_RAW = config.get("ALLOWED_IPS", [])

    # Split into exact IPs vs CIDR ranges
    ALLOWED_IP_STRINGS = {ip for ip in ALLOWED_IPS_RAW if "/" not in ip}
    ALLOWED_NETWORKS = [
        ipaddress.ip_network(net)
        for net in ALLOWED_IPS_RAW
        if "/" in net
]
    ALLOWED_ORIGINS = config.get("ALLOWED_ORIGINS", ["*"])  # Default to allow all origins
    CLOUD_SERVER_URL = config.get("CLOUD_SERVER_URL", "https://your-cloud-server.com")
    SSL_CERTFILE = config.get("SSL_CERTFILE")
    SSL_KEYFILE = config.get("SSL_KEYFILE")
    EXCLUDED_AGGREGATED_FILES = config.get("EXCLUDED_AGGREGATED_FILES", [])
    INCLUDE_FILES = set(config.get("filters", {}).get("include_files", []))  # Default to empty set if missing
    EXCLUDE_FILES = set(config.get("filters", {}).get("exclude_files", []))  # Default to empty set if missing
    EXCLUDE_FOLDERS = set(config.get("filters", {}).get("exclude_folders", []))  # Default to empty set if missing
    
except FileNotFoundError as e:
    print(f"Error: {e}")
    LOCAL_API_KEY = "default_key"
    CODE_PATH = Path(".")
    
app.add_middleware(
    CORSMiddleware,
    # allow_origin_regex=CORS_REGEX,  # ✅ Allow all localhost & local network IPs dynamically
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else [],  # ✅ Allow all origins
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],  # ✅ Explicitly allow OPTIONS
    allow_headers=["x_local_api_key", "Content-Type"],  # ✅ Restrict allowed headers
)

@app.middleware("http")
async def check_ip(request: Request, call_next):
    client_ip_str = request.client.host

    # STRICT: If config has *no* allowed IPs or networks → block everything
    if not ALLOWED_IP_STRINGS and not ALLOWED_NETWORKS:
        print(f"🚨 No allowed IPs configured. Blocking {client_ip_str}")
        raise HTTPException(status_code=403, detail="Access Denied")

    # Parse client IP
    try:
        client_ip = ipaddress.ip_address(client_ip_str)
    except ValueError:
        print(f"🚨 Invalid IP address: {client_ip_str}")
        raise HTTPException(status_code=403, detail=f"Access Denied for {client_ip_str}")

    # 1) Check exact match
    if client_ip_str in ALLOWED_IP_STRINGS:
        print(f"✅ Authorized Access: {client_ip_str}")
        return await call_next(request)

    # 2) Check CIDR network match
    for network in ALLOWED_NETWORKS:
        if client_ip in network:
            print(f"✅ Authorized Access via CIDR {network}: {client_ip_str}")
            return await call_next(request)

    # 3) Block anything else
    print(f"🚨 Unauthorized Access Attempt: {client_ip_str}")
    raise HTTPException(status_code=403, detail=f"Access Denied for {client_ip_str}")


def verify_local_api_key(x_local_api_key: str = Header(None)):
    """Dependency to enforce API key verification in routes."""
    if not x_local_api_key:
        raise HTTPException(status_code=403, detail="API Key is missing")
    if x_local_api_key != LOCAL_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return x_local_api_key


# Simple in-memory rate limiter
class RateLimiter:
    def __init__(self, requests_per_minute=60):
        self.requests_per_minute = requests_per_minute
        self.request_history = {}
        
    def check(self, client_ip):
        current_time = time.time()
        if client_ip not in self.request_history:
            self.request_history[client_ip] = []
            
        # Clean old requests
        self.request_history[client_ip] = [t for t in self.request_history[client_ip] 
                                          if current_time - t < 60]
        
        # Check rate limit
        if len(self.request_history[client_ip]) >= self.requests_per_minute:
            return False
            
        # Add new request
        self.request_history[client_ip].append(current_time)
        return True

rate_limiter = RateLimiter()

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host
    if client_ip in ["127.0.0.1", "localhost"]:
        return await call_next(request)  # Skip rate limiting for internal requests
    if not rate_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests")
    return await call_next(request)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


async def push_metrics_to_cloud(payload: dict) -> bool:
    """Push latest metrics payload to the cloud server for caching."""
    try:
        print(f"📤 Pushing metrics to cloud: {CLOUD_SERVER_URL}/ingest")
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{CLOUD_SERVER_URL}/ingest",
                headers={
                    "x-cloud-api-key": CLOUD_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            print(f"✅ Successfully pushed metrics to cloud")
            return True
    except httpx.HTTPStatusError as e:
        print(f"🚨 Cloud ingest HTTP {e.response.status_code}: {e.response.text[:500]}")
    except httpx.RequestError as e:
        print(f"🚨 Cloud ingest request failed: {e}")
    except Exception as e:
        print(f"🚨 Cloud ingest unexpected error: {type(e).__name__}: {e}")
    return False


async def build_and_push_all_data():
    """Build complete payload by calling existing route functions directly, then push to cloud."""

    # Call existing route functions directly (bypasses HTTP, reuses exact same logic)
    progress_data = await get_code_progress(x_local_api_key=LOCAL_API_KEY)
    complexity_data = await get_complexity_warnings(x_local_api_key=LOCAL_API_KEY)

    heatmap_weekly = await get_heatmap_data(x_local_api_key=LOCAL_API_KEY, period="weekly")
    heatmap_monthly = await get_heatmap_data(x_local_api_key=LOCAL_API_KEY, period="monthly")
    heatmap_yearly = await get_heatmap_data(x_local_api_key=LOCAL_API_KEY, period="yearly")

    payload = {
        "progress": progress_data,
        "complexity_warnings": complexity_data,
        "heatmap_data_weekly": heatmap_weekly,
        "heatmap_data_monthly": heatmap_monthly,
        "heatmap_data_yearly": heatmap_yearly,
    }

    success = await push_metrics_to_cloud(payload)
    return success


async def get_external_ip():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://api.ipify.org?format=json", timeout=5)
            print("Received response:", response.status_code)
            response.raise_for_status()
            print("Raised for status")
            json_data = response.json()  # Add await here
            print(json_data)
            external_ip = json_data.get("ip")
            return external_ip
    except httpx.RequestError as e:
        print(f"🚨 Error: {e}")
        return None
    
# async def get_external_ip():
#     print("Getting external IP...")
#     try:
#         # Try a different service that returns plain text
#         async with httpx.AsyncClient(verify=False, timeout=3) as client:
#             response = await client.get("https://ifconfig.me/ip")
#             ip = await response.text()
#             ip = ip.strip()
#             print(f"external_ip {ip}")
#             return ip
#     except Exception as e:
#         print(f"🚨 Error getting IP: {type(e).__name__}: {e}")
#         return None
    
# def request_cloud_to_retrieve_data():
#     """Requests the cloud server to retrieve the latest data from this local server."""
#     try:
#         response = requests.get(
#             f"{CLOUD_SERVER_URL}/retrieve_data_from_local",
#             headers={"x-cloud-api-key": CLOUD_API_KEY,
#                      'accept': 'application/json'},
#                      timeout=120


                     
#         )
#         # response.raise_for_status()  # ✅ Raises exception for HTTP errors
#         print(response.json())  # Log response
#     except Exception as e:
#         print(f"🚨 Failed to request cloud retrieval: {e}")

# def request_cloud_to_retrieve_data():
#     """Sends a POST request to the cloud server including the current external IP."""
#     external_ip = get_external_ip()
#     if external_ip is None:
#         print("🚨 Could not retrieve external IP.")
#         return

#     try:
#         response = requests.post(
#             f"{CLOUD_SERVER_URL}/retrieve_data_from_local",
#             headers={
#                 "x-cloud-api-key": CLOUD_API_KEY,
#                 "Content-Type": "application/json",
#                 "accept": "application/json"
#             },
#             json={"external_ip": external_ip},
#             timeout=120
#         )
#         print(response.json())  # Log response
#     except Exception as e:
#         print(f"🚨 Failed to request cloud retrieval: {e}")
async def request_cloud_to_retrieve_data():
    """Sends a POST request to the cloud server including the current external IP."""
    print("Getting external IP...")
    external_ip = await get_external_ip()
    print("external_ip", external_ip)
    if external_ip is None:
        print("🚨 Could not retrieve external IP.")
        return

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{CLOUD_SERVER_URL}/retrieve_data_from_local",
                headers={
                    "x-cloud-api-key": CLOUD_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={"external_ip": external_ip},
            )

            # Always log status + a short body preview
            ct = resp.headers.get("content-type", "")
            body_preview = (resp.text[:300] if resp.text else "")
            print(f"Cloud response: {resp.status_code} {ct}\n{body_preview}")

            # Raise for non-2xx; this will go to the except block below
            resp.raise_for_status()

            # Only parse JSON if content-type looks like JSON and body is non-empty
            if "application/json" in ct.lower() and resp.text.strip():
                data = resp.json()
                print("✅ Cloud JSON:", data)
            else:
                print("ℹ️ Cloud returned non-JSON (which is fine if expected).")

    except httpx.HTTPStatusError as e:
        # HTTP error with body that likely isn't JSON; show the text to diagnose
        print(f"🚨 HTTP error {e.response.status_code} during cloud retrieval.")
        print(f"Response body:\n{e.response.text[:500]}")
    except httpx.RequestError as e:
        # Network/SSL/DNS errors
        print(f"🚨 Request to cloud failed: {e}")
    except Exception as e:
        # Anything else (e.g., JSON decode when body isn't JSON)
        print(f"🚨 Unexpected error during cloud retrieval: {type(e).__name__}: {e}")







def get_last_modified_date_by_blame(filepath: Path, function_name:str, lineno: int, endline: int) -> str | None:
    """
    Uses git blame to get the most recent modification date of a line range in a file.
    Returns the date and time in ISO format: YYYY-MM-DD HH:MM:SS.
    """
    try:
        output = subprocess.check_output(
            ["git", "blame", f"-L{lineno},{endline}", "--date=iso", str(filepath)],
            cwd=str(filepath.parent),
            text=True,
            stderr=subprocess.DEVNULL
        )
        # Extract ISO timestamps (format: YYYY-MM-DD HH:MM:SS ±TZ)
        dates = []
        for line in output.splitlines():
            if "20" in line:  # crude filter to ensure a year appears
                parts = line.split()
                for i, part in enumerate(parts):
                    if part.count("-") == 2 and i + 1 < len(parts) and ":" in parts[i + 1]:
                        date_str = f"{part} {parts[i + 1]}"
                        try:
                            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                            dates.append(dt)
                        except ValueError:
                            continue
        if dates:
            print(f"Function: {function_name}, Max date found: { max(dates).strftime('%Y-%m-%d %H:%M:%S')}")
            return max(dates).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        print(f"⚠️ git blame failed for {filepath}:{lineno}-{endline} — {e}")
    return None



def run_radon():
    """Runs Radon analysis on selected files and extracts high-complexity functions (CCN > 10)."""
    
    candidate_files = [
    str(f) for f in Path(CODE_PATH).rglob("*")
    if f.is_file()
    and f.suffix in FILE_EXTENSIONS
    and not any(folder in f.parts for folder in EXCLUDE_FOLDERS)
    and (not INCLUDE_FILES or f.name in INCLUDE_FILES)
    and f.name not in EXCLUDE_FILES
]





    code_files = [f for f in candidate_files if f.endswith(".py")]
    for code_file in code_files:
        print(f"✅ CODE FILE Included: {code_file}")
    # print(f"Printing all code files found: {code_files}")
    print(f"🔍 Analyzing {len(code_files)} files with Radon (Includes: {INCLUDE_FILES}, Excludes: {EXCLUDE_FILES})")

    if not code_files:
        print("🚫 No code files found for Radon analysis.")
        return 0, 0, []

    try:
        unique_high_ccn = {}
        total_ccn = 0
        total_functions = 0

        for file in code_files:
            file_path = Path(file)
            try:
                with open(file, "r", encoding="utf-8") as f:
                    source_code = f.read()

                results = cc_visit(source_code)
            except Exception as e:
                print(f"🚨 Radon failed on file: {file_path}")
                print(f"Error: {e}")
                continue  # Skip to the next file

            for func in results:
                function_name = func.name
                function_ccn = func.complexity
                start_line = func.lineno
                end_line = func.endline

                total_ccn += function_ccn
                total_functions += 1

                if function_ccn > 10:
                    last_modified = get_last_modified_date_by_blame(file_path, function_name, start_line, end_line)

                    key = (file_path.name, function_name)
                    if key not in unique_high_ccn or function_ccn > unique_high_ccn[key]["ccn"]:
                        unique_high_ccn[key] = {
                            "filename": file_path.name,
                            "function": function_name,
                            "ccn": function_ccn,
                            "last_modified": last_modified or "unknown"
                        }

        high_ccn_functions = sorted(unique_high_ccn.values(), key=lambda x: x["ccn"], reverse=True)
        avg_ccn = total_ccn / total_functions if total_functions else 0

        print(f"📊 Radon High Complexity Functions Found: {len(high_ccn_functions)}")
        print(f"📈 Total Functions: {total_functions}, Total CCN: {total_ccn}, Avg CCN: {avg_ccn}")

        return total_functions, avg_ccn, high_ccn_functions

    except Exception as e:
        print(f"🚨 Radon analysis failed: {e}")
        return 0, 0, 
    

from collections import defaultdict
latest_loc_stats = {}

def count_nonblank_loc_stats(max_top_files: int = 30) -> dict:
    """
    Return non-blank LOC stats across the repo, respecting all filters you already use.
    Breakdown includes:
      - total_nonblank
      - by_extension: [{"ext": ".py", "lines": N}, ...] (desc)
      - by_top_folder: [{"folder": "src", "lines": N}, ...] (desc)
      - top_files: [{"file": "path/relative.py", "lines": N}, ...] (desc, capped)
    """
    total_nonblank = 0
    by_ext = defaultdict(int)
    by_top = defaultdict(int)
    per_file = []

    for f in Path(CODE_PATH).rglob("*"):
        p = Path(f)
        if not p.is_file():
            continue
        if p.suffix == ".csv":
            continue
        if p.name in EXCLUDED_AGGREGATED_FILES:
            continue
        if any(folder in p.parts for folder in EXCLUDE_FOLDERS):
            continue
        if FILE_EXTENSIONS and p.suffix not in FILE_EXTENSIONS:
            continue
        if INCLUDE_FILES and p.name not in INCLUDE_FILES:
            continue
        if p.name in EXCLUDE_FILES:
            continue

        count = 0
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.strip():
                        count += 1
        except Exception as e:
            print(f"⚠️ Skipping unreadable file {p}: {e}")
            continue

        if count:
            total_nonblank += count
            ext = p.suffix or ""
            by_ext[ext] += count
            try:
                rel = p.relative_to(CODE_PATH)
            except ValueError:
                rel = p
            parts = rel.parts
            top = parts[0] if len(parts) > 1 else "(root)"
            by_top[top] += count
            per_file.append((str(rel), count))

    by_extension = sorted(
        [{"ext": k, "lines": v} for k, v in by_ext.items()],
        key=lambda x: x["lines"], reverse=True
    )
    by_top_folder = sorted(
        [{"folder": k, "lines": v} for k, v in by_top.items()],
        key=lambda x: x["lines"], reverse=True
    )
    top_files = sorted(
        [{"file": f, "lines": n} for f, n in per_file],
        key=lambda x: x["lines"], reverse=True
    )[:max_top_files]

    print(f"📏 LOC Stats: Total Non-Blank Lines: {total_nonblank}")
    print(f"📊 By Extension: {by_extension}")
    print(f"📂 By Top Folder: {by_top_folder}")
    print(f"📄 Top {max_top_files} Files: {top_files}")
    print(f"✅ LOC stats calculation complete.")
    return {
        "total_nonblank": total_nonblank,
        "by_extension": by_extension,
        "by_top_folder": by_top_folder,
        "top_files": top_files,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_git_repo():
    """Loads the Git repository using GitPython."""
    try:
        print("Git repository found.")
        print("CODE_PATH", REPO_PATH)
        return git.Repo(REPO_PATH)
    except git.exc.InvalidGitRepositoryError:
        print("Invalid Git repository found.")
        return None
    

def get_git_commit_history(since=None, until=None):
    """Fetches the total number of commits, manually filtering by date if needed.
    NEW: when a date window is used, only count commits that touch at least one relevant file
    (suffix in FILE_EXTENSIONS) and skip any files listed in EXCLUDED_AGGREGATED_FILES.
    """
    repo = get_git_repo()
    if not repo:
        print("No Git repository found.")
        return 0
    
    try:
        # Get all commits
        all_commits = list(repo.iter_commits("--all"))
        if not all_commits:
            print("No commits found.")
            return 0
        
        # If no date filters, preserve original behavior
        if not since and not until:
            return len(all_commits)
        
        # Convert date strings to datetime objects
        since_date = datetime.strptime(since, "%Y-%m-%d") if since else None
        until_date = datetime.strptime(until, "%Y-%m-%d") if until else None
        # Add one day to until_date to make it inclusive
        if until_date:
            until_date = until_date + timedelta(days=1)
        
        # Manually filter commits by date
        filtered_commits = []
        for commit in all_commits:
            commit_date = datetime.fromtimestamp(commit.committed_date)
            if (not since_date or commit_date >= since_date) and \
               (not until_date or commit_date <= until_date):
                filtered_commits.append(commit)

        # NEW: count only commits that touch a relevant (non-aggregated, allowed-suffix) file
        relevant_count = 0
        for commit in filtered_commits:
            for file_path in commit.stats.files.keys():
                p = Path(file_path)
                if  p.suffix == ".csv":
                    continue
                
                if p.name in EXCLUDED_AGGREGATED_FILES:
                    print(f"🗑️ [Commit History] Skipping excluded file: {file_path}")
                    continue
                if any(folder in p.parts for folder in EXCLUDE_FOLDERS):
                    continue
                if p.suffix in FILE_EXTENSIONS:
                    relevant_count += 1
                    break  # this commit qualifies; move to next

        return relevant_count

    except Exception as e:
        print(f"Error counting commits: {e}")
        print(traceback.format_exc())
        return 0

    
    

def get_git_line_changes(since=None, until=None):
    """Fetches line changes, manually filtering by date if needed."""
    repo = get_git_repo()
    if not repo:
        return {"lines_added": 0, "lines_removed": 0, "net_lines_added": 0}
    
    try:
        # Get all commits
        all_commits = list(repo.iter_commits("--all"))
        print(f"🔍 Total commits in repo: {len(all_commits)}, Since:{since}, Until: {until}")
        
        # Convert date strings to datetime objects
        since_date = datetime.strptime(since, "%Y-%m-%d") if since else None
        until_date = datetime.strptime(until, "%Y-%m-%d") if until else None
        # Add one day to until_date to make it inclusive
        if until_date:
            until_date = until_date + timedelta(days=1)

        print(f"🗓️ Filtering commits from: {since_date} to {until_date}")

        
        # Manually filter commits by date and calculate stats
        lines_added = 0
        lines_removed = 0
        
        for commit in all_commits:
            commit_date = datetime.fromtimestamp(commit.committed_date)
            commit_str = commit_date.strftime("%Y-%m-%d %H:%M:%S")

            if (not since_date or commit_date >= since_date) and \
               (not until_date or commit_date <= until_date):
                # stats = commit.stats.total
                # lines_added += stats["insertions"]
                    # lines_removed += stats["deletions"]
                # print(f"✅ Counting commit {commit.hexsha[:7]} on {commit_str}: +{stats['insertions']} -{stats['deletions']}")
                for file_path, file_stat in commit.stats.files.items():
                    p = Path(file_path)
                    if  p.suffix == ".csv":
                        continue
                    
                    if p.name in EXCLUDED_AGGREGATED_FILES:
                        print(f"🗑️ [Line Changes] Skipping excluded file: {file_path}")
                        continue
                    if any(folder in p.parts for folder in EXCLUDE_FOLDERS):
                        print(f"🗑️ [Line Changes] Skipping folder excluded by config: {file_path}")
                        continue
                    # else:
                    #     print(f"📁 Examining file: {file_path}")
                    if p.suffix in FILE_EXTENSIONS:
                        print(f"📄 Including file: {file_path} | +{file_stat['insertions']} -{file_stat['deletions']}")
                        lines_added += file_stat["insertions"]
                        lines_removed += file_stat["deletions"]
                    else:
                        print(f"🗑️ Skipping file (not in extensions): {file_path}")
                    # 
            # else:
                # print(f"⏭️ Skipping commit {commit.hexsha[:7]} on {commit_str}")
        
        print(f"📈 Final line stats — Added: {lines_added}, Removed: {lines_removed}, Net: {lines_added - lines_removed}")

        return {
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "net_lines_added": lines_added - lines_removed
        }
    except Exception as e:
        print(f"Error calculating line changes: {e}")
        print(traceback.format_exc())
        return {"lines_added": 0, "lines_removed": 0, "net_lines_added": 0}

def save_to_csv(data):
    print("Saving data to CSV...")
    """Adds new data to historical records without removing older entries."""
    df_new = pd.DataFrame([data])  # Convert the new data to a DataFrame
    
    # If file exists, load the existing data
    if CSV_FILE.exists():
        df = pd.read_csv(CSV_FILE, on_bad_lines="skip")
        # Append new data
        df = pd.concat([df, df_new], ignore_index=True)
    else:
        # First recording, just use the new data
        df = df_new
    
    # Save the combined data (preserving all historical records)
    df.to_csv(CSV_FILE, index=False)

def backfill_historical_data():
    """
    Backfills historical data as if calculations were done on each date,
    using the same rolling period logic as the current calculations.
    """

    # Delete the CSV file if it exists
    if CSV_FILE.exists():
        CSV_FILE.unlink()
        print(f"Deleted existing CSV file: {CSV_FILE}")

    print("Starting historical data backfill...")
    repo = get_git_repo()
    if not repo:
        print("No Git repository found.")
        return False
        
    try:
        # Get all commits
        all_commits = list(repo.iter_commits("--all"))
        if not all_commits:
            print("No commits found.")
            return False
        
        # Find earliest and latest commit dates
        earliest_commit = min(all_commits, key=lambda c: c.committed_date)
        latest_commit = max(all_commits, key=lambda c: c.committed_date)
        
        earliest_date = datetime.fromtimestamp(earliest_commit.committed_date)
        latest_date = datetime.fromtimestamp(latest_commit.committed_date)
        
        print(f"Repository has {len(all_commits)} commits")
        print(f"Earliest commit: {earliest_date.strftime('%Y-%m-%d')}")
        print(f"Latest commit: {latest_date.strftime('%Y-%m-%d')}")
        
        # Index all commits by date for faster lookups
        commits_by_date = {}
        for commit in all_commits:
            commit_date = datetime.fromtimestamp(commit.committed_date).strftime("%Y-%m-%d")
            if commit_date not in commits_by_date:
                commits_by_date[commit_date] = []
            commits_by_date[commit_date].append(commit)
        
        # Get all unique dates where commits happened
        commit_dates = sorted(commits_by_date.keys())
        print(f"Found commits on {len(commit_dates)} unique dates")
        
        # Pre-compute sets for O(1) lookups instead of O(n) list searches
        excluded_files_set = set(EXCLUDED_AGGREGATED_FILES)
        excluded_folders_set = set(EXCLUDE_FOLDERS)
        file_extensions_set = set(FILE_EXTENSIONS)

        # Pre-process all commits once to filter and extract relevant data
        print("Pre-processing commits for faster lookups...")
        processed_commits = {}
        for date_str, commits in commits_by_date.items():
            date_commits = []
            for commit in commits:
                commit_files = []
                for file_path, file_stat in commit.stats.files.items():
                    p = Path(file_path)
                    
                    if p.suffix == ".csv":
                        continue
                        
                    if p.name in excluded_files_set:
                        print(f"🗑️ [Backfill] Skipping excluded file: {file_path}")
                        continue
                        
                    if excluded_folders_set.intersection(p.parts):
                        print(f"🗑️ [Backfill] Skipping folder excluded by config: {file_path}")
                        continue
                        
                    if p.suffix in file_extensions_set:
                        commit_files.append({
                            'insertions': file_stat["insertions"],
                            'deletions': file_stat["deletions"]
                        })
                
                if commit_files:
                    date_commits.append({
                        'commit': commit,
                        'files': commit_files
                    })
            
            if date_commits:
                processed_commits[date_str] = date_commits
        
        # Load existing data to avoid duplicating entries
        # existing_timestamps = set()
        # if CSV_FILE.exists():
        #     df = pd.read_csv(CSV_FILE, on_bad_lines="skip")
        #     if "timestamp" in df.columns:
        #         existing_timestamps = set(df["timestamp"].dropna().unique())
        
        # For each commit date, calculate the metrics as if the code was running on that date
        backfilled_data = []
        
        for i, calc_date_str in enumerate(commit_dates):
            if i % 10 == 0:
                print(f"Processing date {i+1}/{len(commit_dates)}: {calc_date_str}")
            
            timestamp_str = f"{calc_date_str} 00:00:00"
            # if timestamp_str in existing_timestamps:
            #     print(f"Skipping existing date {calc_date_str}")
            #     continue  # Skip dates we already have data for
                
            calc_date = datetime.strptime(calc_date_str, "%Y-%m-%d")
            
            # Calculate the start dates for rolling periods from this calculation date
            week_start = (calc_date - timedelta(days=7)).strftime("%Y-%m-%d")
            month_start = (calc_date - timedelta(days=30)).strftime("%Y-%m-%d")
            year_start = (calc_date - timedelta(days=365)).strftime("%Y-%m-%d")
            
            # Get all commits between start and calculation date for each period
            weekly_commits = []
            weekly_insertions = 0
            weekly_deletions = 0
            
            monthly_commits = []
            monthly_insertions = 0
            monthly_deletions = 0
            
            yearly_commits = []
            yearly_insertions = 0
            yearly_deletions = 0
            
            # Scan through all commits in date order
            for date_str in commit_dates:
                date = datetime.strptime(date_str, "%Y-%m-%d")
                
                # Skip if the date is after our calculation date
                if date > calc_date:
                    continue
                
                # Skip if no processed commits for this date
                if date_str not in processed_commits:
                    continue
                    
                for commit_data in processed_commits[date_str]:
                    commit = commit_data['commit']
                    
                    for file_data in commit_data['files']:
                        if date_str >= week_start:
                            weekly_commits.append(commit)
                            weekly_insertions += file_data['insertions']
                            weekly_deletions += file_data['deletions']

                        if date_str >= month_start:
                            monthly_commits.append(commit)
                            monthly_insertions += file_data['insertions']
                            monthly_deletions += file_data['deletions']

                        if date_str >= year_start:
                            yearly_commits.append(commit)
                            yearly_insertions += file_data['insertions']
                            yearly_deletions += file_data['deletions']
            
            # Create record for this calculation date
            backfilled_data.append({
                "timestamp": timestamp_str,
                "period_end": calc_date_str,
                "period_type": "calculation_date_backfill",
                "week_start": week_start,
                "month_start": month_start,
                "year_start": year_start,
                
                # Weekly metrics
                "total_commits_this_week": len(weekly_commits),
                "lines_added_this_week": weekly_insertions,
                "lines_removed_this_week": weekly_deletions,
                "net_lines_added_this_week": weekly_insertions - weekly_deletions,
                
                # Monthly metrics
                "total_commits_this_month": len(monthly_commits),
                "lines_added_this_month": monthly_insertions,
                "lines_removed_this_month": monthly_deletions,
                "net_lines_added_this_month": monthly_insertions - monthly_deletions,
                
                # Yearly metrics
                "total_commits_this_year": len(yearly_commits),
                "lines_added_this_year": yearly_insertions,
                "lines_removed_this_year": yearly_deletions,
                "net_lines_added_this_year": yearly_insertions - yearly_deletions,
                
                # Placeholders for code complexity metrics
                # "avg_ccn_lizard": 0,
                "avg_ccn_radon": 0,
                # "total_functions_lizard": 0,
                "total_functions_radon": 0,
                # "high_complexity_functions_lizard": [],
                "high_complexity_functions_radon": []
            })
        
        # Save to CSV
        if backfilled_data:
            df_backfill = pd.DataFrame(backfilled_data)
            # if CSV_FILE.exists():
            #     # Load existing data and remove any previous backfill entries
            #     df_existing = pd.read_csv(CSV_FILE, on_bad_lines="skip")
            #     df_existing = df_existing[df_existing.get("period_type") != "calculation_date_backfill"]
            #     df_combined = pd.concat([df_existing, df_backfill], ignore_index=True)
            #     df_combined.to_csv(CSV_FILE, index=False)
            # else:
            # delete the CSV file if it exists
            df_backfill.to_csv(CSV_FILE, index=False)
            
            print(f"Saved {len(backfilled_data)} data points to {CSV_FILE}")
            print("Backfill last 5 dates:", df_backfill["period_end"].tail().tolist())
        
        return True
        
    except Exception as e:
        print(f"Error during backfill: {str(e)}")
        print(traceback.format_exc())
        return False
    
async def is_local_server_running():
    """Checks if FastAPI is running by making a GET request to the root endpoint."""
    retries = 3
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(verify=False) as client:
                response = await client.get(f"https://localhost:{SERVER_PORT}/", timeout=5)
                print(f"Response: {response}")
                return response.status_code == 200  # ✅ Server is running
        except httpx.RequestError:
            print(f"Server is not reachable, retrying... ({attempt + 1}/{retries})")
            await asyncio.sleep(2)
    return False




def find_process_using_port(port):
    """Find the process using the given port and return its PID."""
    try:
        result = subprocess.run(
            ["lsof", "-i", f":{port}", "-t"],
            capture_output=True,
            text=True,
            check=False
        )
        if result.stdout.strip():
            pid = int(result.stdout.strip())
            logger.info(f"Found process {pid} using port {port}")
            print(f"Found process {pid} using port {port}")
            return pid
    except ValueError as e:
        logger.error(f"Error finding process on port {port}: {e}")
        print(f"Error finding process on port {port}: {e}")
    return None

def find_screen_session():
    """Finds the screen session ID that is running the localytics server."""
    try:
        result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        screen_sessions = result.stdout.splitlines()
        logger.info(f"Screen sessions found: {screen_sessions}")

        for line in screen_sessions:
            line = line.strip()
            if "localytics_server" in line:  # Adjust based on actual session name
                session_id = line.split("\t")[0]  # Extract session ID properly
                logger.info(f"Found screen session: {session_id}")
                print(f"Found screen session: {session_id}")
                return session_id
    except Exception as e:
        logger.error(f"Error finding screen session: {e}")
        print(f"Error finding screen session: {e}")
    return None

async def schedule_shutdown(port):
    logging.info("Waiting for metrics update to finish...")
    await update_metrics_finished.wait()
    logging.info("Metrics update finished. Scheduling shutdown...")
    """Schedules the process and screen session to be shut down after script exit."""
    logger.info(f"Scheduling shutdown for process using port {port} in 5 seconds.")
        
    # Find the actual process ID using the correct function
    pid = find_process_using_port(port)
    
    if pid:
        logger.info(f"Found process {pid} using port {port}. Will terminate in 5 seconds.")
        # Kill process using the actual PID (not the port)
        subprocess.Popen(["python3", "-c", f"import time, subprocess; time.sleep(5); subprocess.run(['kill', '-9', '{pid}'], check=False)"])
    else:
        logger.info(f"No process found on port {port}. Skipping process termination.")

    # Find screen session before scheduling shutdown
    session_id = find_screen_session()
    
    if session_id:
        logger.info(f"Found screen session {session_id}. Scheduling shutdown in 10 seconds.")
        # Close screen session after script exits (10-second delay)
        subprocess.Popen(["python3", "-c", f"import time, subprocess; time.sleep(10); subprocess.run(['screen', '-S', '{session_id}', '-X', 'quit'], check=False)"])
    else:
        logger.info("No active screen session found. Skipping screen shutdown.")

    print("Old process and screen session will shut down after script exits.")
    logger.info("Shutdown scheduling complete.")
    await asyncio.sleep(20)  # Wait for the message to be printed
    # shared_state["should_stop"] = True

    

def stop_process_using_port(port, status):
    """Stops the process using the given port immediately."""
    pid = find_process_using_port(port)

    if pid:
        logger.info(f"Port {port} is in use. Stopping process {pid} on port {port}. Status: {status}")
        print(f"Port {port} is in use. Stopping existing process... {status}")
        subprocess.run(["kill", "-9", str(pid)], check=False)
        time.sleep(2)
    else:
        logger.info(f"Port {port} is not in use. Status: {status}")
        print(f"Port {port} is not in use. {status}")

async def wake_up_server():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(CLOUD_SERVER_URL, timeout=200)
        if response.status_code == 200:
            print("Server is awake!")
        else:
            print(f"Failed to wake the server. Status Code: {response.status_code}")
    except httpx.RequestError as e:
        print(f"Error: {e}")
    

async def update_metrics():

    print("Counting non-blank lines of code...")
    total_loc_nonblank = count_nonblank_loc_stats()["total_nonblank"]
    """Background task: every 15 minutes, recalculate and store all metrics."""
    global latest_progress_data
    
    while not shared_state["should_stop"]:
        timestamp = datetime.now()
        timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        print(f"Calculating metrics at {timestamp_str}...")
        
        # total_functions_lizard, avg_ccn_lizard, high_ccn_functions_lizard = run_lizard()
        print("Running Radon analysis...")
        total_functions_radon, avg_ccn_radon, high_ccn_functions_radon = run_radon()

        # Calculate trailing periods from current date
        week_ago = (timestamp - timedelta(days=7)).strftime("%Y-%m-%d")
        month_ago = (timestamp - timedelta(days=30)).strftime("%Y-%m-%d") 
        year_ago = (timestamp - timedelta(days=365)).strftime("%Y-%m-%d")

        # Get stats for each trailing period
        print("Calculating weekly metrics")
        weekly_commits = get_git_commit_history(since=week_ago)
        weekly_lines = get_git_line_changes(since=week_ago)
        print("Calculating monthly metrics")
        monthly_commits = get_git_commit_history(since=month_ago)
        monthly_lines = get_git_line_changes(since=month_ago)
        print("Calculating yearly metrics")
        yearly_commits = get_git_commit_history(since=year_ago)
        yearly_lines = get_git_line_changes(since=year_ago)
        print("Finished calculating metrics")
        latest_progress_data = {
            "timestamp": timestamp_str,
            "period_end": timestamp.strftime("%Y-%m-%d"),
            "period_type": "rolling",  # Indicate this is a rolling period
            "week_start": week_ago,    # Start of 7-day period
            "month_start": month_ago,  # Start of 30-day period 
            "year_start": year_ago,    # Start of 365-day period
            
            # # Code complexity metrics
            # "avg_ccn_lizard": avg_ccn_lizard,
            "avg_ccn_radon": avg_ccn_radon,
            # "high_complexity_functions_lizard": high_ccn_functions_lizard,
            "high_complexity_functions_radon": high_ccn_functions_radon,
            "total_functions_radon": total_functions_radon,
            # "total_functions_lizard": total_functions_lizard,
            
            # Weekly metrics (trailing 7 days)
            "total_commits_this_week": weekly_commits,
            "lines_added_this_week": weekly_lines["lines_added"],
            "lines_removed_this_week": weekly_lines["lines_removed"],
            "net_lines_added_this_week": weekly_lines["net_lines_added"],
            
            # Monthly metrics (trailing 30 days)
            "total_commits_this_month": monthly_commits,
            "lines_added_this_month": monthly_lines["lines_added"],
            "lines_removed_this_month": monthly_lines["lines_removed"],
            "net_lines_added_this_month": monthly_lines["net_lines_added"],
            
            # Yearly metrics (trailing 365 days)
            "total_commits_this_year": yearly_commits,
            "lines_added_this_year": yearly_lines["lines_added"],
            "lines_removed_this_year": yearly_lines["lines_removed"],
            "net_lines_added_this_year": yearly_lines["net_lines_added"],
             "total_loc_nonblank": total_loc_nonblank
        }

        # save_to_csv(latest_progress_data)
        print("Metrics updated. Waiting for next update...")

        # Wait until all routes have been accessed
        print("⏳ Waiting for all routes to be requested before shutdown...")
        # Send a request to the website to turn on the server
        # Wait for FastAPI to be fully up before running metrics
        await asyncio.sleep(2)
        while not await is_local_server_running():
            print("⏳ Waiting for FastAPI to be fully up...")
            await asyncio.sleep(5)  # Check every 5 seconds
        print("✅ FastAPI is fully running!")
        await wake_up_server()

        # Push data to cloud until all routes have been accessed
        while not all(route_activity.values()):
            print("Requests in progress:", route_activity)
            success = await build_and_push_all_data()
            if success:
                print("✅ Successfully pushed data to cloud!")
            else:
                print("⚠️ Push attempt failed, retrying in 15 seconds...")
            await asyncio.sleep(15)  # Check every 15 seconds

        print("✅ All required routes have been accessed!")

      
        shared_state["should_stop"] = True
        if shared_state["should_stop"]:
            print("🛑 Shutdown signal received. Stopping metrics update.")
            break
        

    
    print("🛑 All tasks complete. Shutting down local server...")
    asyncio.create_task(schedule_shutdown(SERVER_PORT)) # Run shutdown in the background
    update_metrics_finished.set()
    

        
        
        # sys.exit(0)  # Force the server to stop
        # await asyncio.wait_for(shutdown_event.wait(), timeout=900)  # 15 minutes wait
        # print("Finished waiting")




@app.get("/progress")
async def get_code_progress(x_local_api_key: str = Depends(verify_local_api_key)):
    """Returns the latest stored code progress data."""
    route_activity["progress"] = True
    print("progress called")
    return latest_progress_data


def get_initial_commit_date():
    """Returns the date of the first commit in the repository."""
    repo = get_git_repo()
    if not repo:
        return None
    first_commit = list(repo.iter_commits("--all"))[-1]  # Last item is the first commit
    return datetime.fromtimestamp(first_commit.committed_date)


@app.get("/heatmap/data")
async def get_heatmap_data(x_local_api_key: str = Depends(verify_local_api_key), period: str = "monthly", max_periods: int = 36):
    """Returns non-overlapping time-series data for commits and line changes."""


    if not CSV_FILE.exists():
        return {"message": "No historical data available."}

    try:
        df = pd.read_csv(CSV_FILE, on_bad_lines="skip")

        # Convert timestamps to datetime
        df['timestamp_dt'] = pd.to_datetime(df['timestamp'])
        # df['timestamp_dt'] = pd.to_datetime(df['timestamp'], errors="coerce")

        # Get the most recent calculation date
        if df.empty:
            return {"error": "No data available."}

        most_recent_date = df['timestamp_dt'].max()
        print("🔥 Heatmap most_recent_date from CSV:", most_recent_date)

        # Define period settings
        period_mapping = {
            "weekly": {
                "days": 7,
                "time_col": "week_start",
                "commits": "total_commits_this_week",
                "lines_added": "lines_added_this_week",
                "lines_removed": "lines_removed_this_week",
                "net_lines_added": "net_lines_added_this_week",
            },
            "monthly": {
                "days": 30,
                "time_col": "month_start",
                "commits": "total_commits_this_month",
                "lines_added": "lines_added_this_month",
                "lines_removed": "lines_removed_this_month",
                "net_lines_added": "net_lines_added_this_month",
            },
            "yearly": {
                "days": 365,
                "time_col": "year_start",
                "commits": "total_commits_this_year",
                "lines_added": "lines_added_this_year",
                "lines_removed": "lines_removed_this_year",
                "net_lines_added": "net_lines_added_this_year",
            }
        }

        if period not in period_mapping:
            return {"error": f"Invalid period '{period}'. Choose from: weekly, monthly, yearly."}

        mapping = period_mapping[period]
        period_days = mapping["days"]
        
        initial_commit_date = get_initial_commit_date()
        if initial_commit_date:
            initial_commit_date = initial_commit_date.replace(hour=0, minute=0, second=0, microsecond=0)

        # Generate non-overlapping periods going backward
        periods = []
        # period_end = most_recent_date  # Start from the most recent date
        period_end = (most_recent_date + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)  # Make end exclusive by adding one day and normalizing to midnight

        for _ in range(max_periods):
            period_start = period_end - timedelta(days=period_days)
            # Normalize to midnight to match DataFrame timestamps
            period_start = period_start.replace(hour=0, minute=0, second=0, microsecond=0)
            period_end = period_end.replace(hour=0, minute=0, second=0, microsecond=0)

            # print("Period Start", period_start)
            # print("Period End", period_end)

                    # **STOP if period_start goes before the first commit**
            if initial_commit_date and period_start < initial_commit_date:
                print("Period start is earlier than the first commit. Stopping.")
                break  # Exit loop if the period start is earlier than the first commit

            # Get records that belong **strictly within this period** (exclusive of period_end)
            # period_records = df[(df['timestamp_dt'] >= period_start) & (df['timestamp_dt'] < period_end)]
            # Get correct grouping column (e.g. "week_start")
            period_records = df[(df['timestamp_dt'] >= period_start) & (df['timestamp_dt'] < period_end)]

            # print("Period Records", period_records)
            if not period_records.empty:
                # Use the most recent record for this period
                record = period_records.sort_values('timestamp_dt', ascending=False).iloc[0]
                period_data = {
                    "period_start": period_start.strftime('%Y-%m-%d'),
                    "period_end": (period_end - timedelta(days=1)).strftime('%Y-%m-%d'),  # Make end date exclusive
                    mapping["time_col"]: period_start.strftime('%Y-%m-%d'),  # ✅ Fixed string formatting
                    mapping["commits"]: int(record[mapping["commits"]]),
                    mapping["lines_added"]: int(record[mapping["lines_added"]]),
                    mapping["lines_removed"]: int(record[mapping["lines_removed"]]),
                    mapping["net_lines_added"]: int(record[mapping["net_lines_added"]]),
                    "period_description": f"{period_days} days ending {(period_end - timedelta(days=1)).strftime('%Y-%m-%d')}"
                }
            else:
                # No data for this period, fill with zero values
                period_data = {
                    "period_start": period_start.strftime('%Y-%m-%d'),
                    "period_end": (period_end - timedelta(days=1)).strftime('%Y-%m-%d'),
                    mapping["time_col"]: period_start.strftime('%Y-%m-%d'),
                    mapping["commits"]: 0,
                    mapping["lines_added"]: 0,
                    mapping["lines_removed"]: 0,
                    mapping["net_lines_added"]: 0,
                    "period_description": f"{period_days} days ending {(period_end - timedelta(days=1)).strftime('%Y-%m-%d')}"
                }

            periods.append(period_data)
            period_end = period_start  # Move to the previous period

        # Sort periods in chronological order
        periods.sort(key=lambda x: x["period_start"])
        route_activity["heatmap"] = True
        print("Heatmap data generated")
        return {
            "period": period,
            "most_recent_calc_date": most_recent_date.strftime('%Y-%m-%d %H:%M:%S'),
            "data": periods
        }


        
    except Exception as e:
        print(f"Error: {str(e)}\n{traceback.format_exc()}")
        return {"error": "An internal server error occurred"}




@app.get("/")
async def read_root():
    return {"message": "Welcome to the Local Analytics Server!"}



@app.get("/complexity_warnings")
async def get_complexity_warnings(x_local_api_key: str = Depends(verify_local_api_key)):
    """Returns the latest high-complexity functions from run_lizard() and run_radon()."""

    route_activity["complexity_warnings"] = True
    print("complexity_warnings called")
    return {
        # "lizard": latest_progress_data.get("high_complexity_functions_lizard", []),
        "radon": latest_progress_data.get("high_complexity_functions_radon", [])
    }




@app.on_event("startup")
async def startup_event():
    """Start the background metrics updater on server startup and backfill historical data."""
    
    print("Starting server - backfilling historical rolling period data...")
    backfill_success = backfill_historical_data()
    if backfill_success:
        print("Historical data backfill completed successfully")
    else:
        print("Historical data backfill encountered errors")
    
    asyncio.create_task(update_metrics())
    print("Background task started. Press Ctrl+C to stop.")

@app.on_event("shutdown")
async def shutdown_event_handler():
    """Stops background tasks when shutting down."""
    print("Shutting down... Stopping background task.")
    
    await asyncio.sleep(0.1)
    print("Shutdown complete.")
    shared_state["should_stop"] = True
    print("Server stopped Shutdown.")

def handle_exit(*args):
    """Handles Ctrl+C (SIGINT) to gracefully shut down the server."""
    print("\n🛑 Server shutting down from Ctrl+C...")
    shared_state["should_stop"] = True
    sys.exit(0)  # Exit cleanly
    print("Server stopped.")

signal.signal(signal.SIGINT, handle_exit)

if __name__ == "__main__":
    try:
        stop_process_using_port(SERVER_PORT, "Preparing to start")
        
        if SSL_CERTFILE and SSL_KEYFILE:
            uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, ssl_keyfile=SSL_KEYFILE, ssl_certfile=SSL_CERTFILE)
        else:
            uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)  # Run without TLS if certs are missing

       
    

    except KeyboardInterrupt:
        handle_exit(None, None)





# def run_lizard():
#     """Runs Lizard analysis only on files with extensions specified in config.json."""
#     # code_files = [str(f) for f in Path(CODE_PATH).rglob("*") if f.suffix in FILE_EXTENSIONS]
#     include_files = set(config["filters"].get("include_files", []))
#     exclude_files = set(config["filters"].get("exclude_files", []))

#     # Get all matching files in the base directory
#     code_files = [
#         str(f) for f in Path(CODE_PATH).iterdir()
#         if f.is_file() and f.suffix in FILE_EXTENSIONS
#         and (not include_files or f.name in include_files)  # If include_files is set, only use those
#         and f.name not in exclude_files  # Always exclude files listed in exclude_files
#     ]
#     print("code_files", code_files)

#     if not code_files:
#         print("No code files found")
#         return 0, 0  # No matching files found, return 0 functions & CCN

#     print("Code files exist")
#     analyzer = list(lizard.analyze_files(code_files))  # Convert map object to list
#     print("Analyzer", analyzer)

#     if not analyzer:  # Ensure it's not empty before processing
#         print("Analyzer is empty")
#         return 0, 0

#     # Filter out files that have no function_list
#     analyzed_files = [file for file in analyzer if hasattr(file, "function_list") and file.function_list]

#     if not analyzed_files:
#         print("No functions found")
#         return 0, 0  # Avoid errors if no functions are found
#     for file in analyzed_files:
#         for func in file.function_list:
#             if func.cyclomatic_complexity > 10:
#                 print(f"⚠️ High Complexity Function: {func.name} in {file.filename} (CCN={func.cyclomatic_complexity})")

#     # Extract high-complexity functions (CCN > 10)
#     high_ccn_functions = sorted([
#     { "function": func.name,  "filename": Path(file.filename).name, "ccn": func.cyclomatic_complexity}
#     for file in analyzed_files for func in file.function_list if func.cyclomatic_complexity > 10
# ], key=lambda x: x["ccn"], reverse=True) # Sort by CCN in descending order
#     print("High CCN Functions", high_ccn_functions)
#     print("Number of high CCN functions", len(high_ccn_functions))

#     total_ccn = sum(func.cyclomatic_complexity for file in analyzed_files for func in file.function_list)
#     total_functions = sum(len(file.function_list) for file in analyzed_files)
#     avg_ccn = total_ccn / total_functions if total_functions else 0
#     print(f"Total Functions: {total_functions}, Total CCN: {total_ccn}, Avg CCN: {avg_ccn}")
#     return total_functions, avg_ccn, high_ccn_functions
