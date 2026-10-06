#!/usr/bin/env python3
"""
Lightweight local web server for the Felix 持倉儀表板 PWA.

Serves the dashboard files and exposes a token-gated endpoint that
triggers a headless Claude Code run of the /portfolio-check workflow,
so the dashboard can be refreshed by tapping a button on your phone
(reached over Tailscale) instead of opening an interactive session.

Zero third-party dependencies — stdlib only, so nothing to `pip install`.
"""
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # serenity-skill/
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")
OAUTH_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude_oauth_token")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run.log")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_state.json")
COMMAND_FILE = os.path.join(ROOT, ".claude", "commands", "portfolio-check.md")
PORT = int(os.environ.get("PORTFOLIO_SERVER_PORT", "8765"))


def load_oauth_token():
    """Long-lived token from `claude setup-token`, used for headless auth
    (env var CLAUDE_CODE_OAUTH_TOKEN) since there's no TTY for /login here."""
    if os.path.exists(OAUTH_TOKEN_FILE):
        with open(OAUTH_TOKEN_FILE) as f:
            return f.read().strip()
    return None

PUBLIC_FILES = {"/manifest.json", "/sw.js", "/icon-192.png", "/icon-512.png"}
DASHBOARD_FILES = {
    "/": "home.html",
    "/home.html": "home.html",
    "/portfolio-dashboard.html": "portfolio-dashboard.html",
    "/portfolio-dashboard-archive.html": "portfolio-dashboard-archive.html",
    "/ranking": "ranking-dashboard.html",
    "/ranking-dashboard.html": "ranking-dashboard.html",
}
RANKING_DATA = "data/ranking-data.js"
PAGE_DATA_FILES = {RANKING_DATA, "data/portfolio.json", "data/trades.json"}  # token-gated data read by the pages
RANKING_SCRIPT = os.path.join(ROOT, "scripts", "momentum_ranker.py")
ranking_lock = threading.Lock()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".js": "application/javascript",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}


def get_or_create_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    tok = secrets.token_urlsafe(24)
    with open(TOKEN_FILE, "w") as f:
        f.write(tok)
    os.chmod(TOKEN_FILE, 0o600)
    return tok


TOKEN = get_or_create_token()


def find_claude_binary():
    env_override = os.environ.get("PORTFOLIO_CLAUDE_BIN")
    if env_override and os.path.exists(env_override):
        return env_override
    p = shutil.which("claude")
    if p:
        return p
    for candidate in (
        os.path.expanduser("~/.claude/local/claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


SCOPES = {
    "": "完整複查",
    "full": "完整複查",
    "market": "大盤",
    "holdings": "持倉",
    "watchlist": "觀察名單",
}

PROGRESS_MAX = 40
STAGE_TOTAL = 4


def load_saved_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        return {
            "last_success_at": saved.get("last_success_at"),
            "last_summary": saved.get("last_summary"),
            "completed_sections": saved.get("completed_sections"),
            "alert_count": saved.get("alert_count"),
        }
    except (OSError, ValueError, TypeError):
        return {}


def save_success_state():
    payload = {
        "last_success_at": job_state.get("last_success_at"),
        "last_summary": job_state.get("last_summary"),
        "completed_sections": job_state.get("completed_sections"),
        "alert_count": job_state.get("alert_count"),
    }
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp_path, STATE_FILE)


def count_stop_alerts():
    try:
        with open(os.path.join(ROOT, "portfolio-dashboard.html"), encoding="utf-8") as f:
            return len(re.findall(r'class="stop-chip danger"', f.read()))
    except OSError:
        return 0


# --- concurrent-write protection -------------------------------------------
# The headless agent spawned below has full Edit/Write access to the same files
# an interactive Claude Code session may be editing right now. Nothing in the
# OS stops the second writer, so guard it here: snapshot first, then refuse to
# start if the files look like they are being actively edited.

DATA_FILES = ("portfolio-dashboard.html", "portfolio-tracker.md", "watchlist.md")
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
BACKUP_KEEP = 30
# If a data file changed more recently than this, assume a human/session is
# mid-edit and don't let the headless run stomp on it.
ACTIVE_EDIT_WINDOW_SEC = int(os.environ.get("PORTFOLIO_EDIT_WINDOW_SEC", "600"))


def snapshot_data_files():
    """Timestamped copy of every data file before the agent touches them.
    Returns the snapshot directory, or None if nothing was copied."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, stamp)
    copied = 0
    for name in DATA_FILES:
        src = os.path.join(ROOT, name)
        if not os.path.exists(src):
            continue
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(src, os.path.join(dest, name))
        copied += 1
    if not copied:
        return None
    # prune oldest snapshots
    try:
        snaps = sorted(
            d for d in os.listdir(BACKUP_DIR)
            if os.path.isdir(os.path.join(BACKUP_DIR, d))
        )
        for old in snaps[:-BACKUP_KEEP]:
            shutil.rmtree(os.path.join(BACKUP_DIR, old), ignore_errors=True)
    except OSError:
        pass
    return dest


def recently_edited_files():
    """Data files modified inside the active-edit window, newest first."""
    now = time.time()
    hits = []
    for name in DATA_FILES:
        path = os.path.join(ROOT, name)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age < ACTIVE_EDIT_WINDOW_SEC:
            hits.append((name, int(age)))
    return sorted(hits, key=lambda x: x[1])


def set_stage(index, label):
    with job_lock:
        if index >= job_state.get("stage_index", 0):
            job_state["stage_index"] = index
            job_state["current_stage"] = label


def stage_for_tool(tool_name):
    name = (tool_name or "").lower()
    if any(word in name for word in ("edit", "write", "patch")):
        return 4, "寫入 Dashboard"
    if any(word in name for word in ("web", "browser", "chrome", "fetch", "search")):
        return 2, "取得市場資料"
    return 3, "分析與核對"


def load_check_prompt(scope_arg):
    with open(COMMAND_FILE, encoding="utf-8") as f:
        content = f.read()
    # strip YAML frontmatter (--- ... ---) if present
    m = re.match(r"^---\n.*?\n---\n(.*)$", content, re.S)
    body = m.group(1).strip() if m else content.strip()
    return body.replace("$ARGUMENTS", scope_arg or "")


def _summarize_tool_input(name, tool_input):
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "url", "query", "file_path", "prompt", "pattern"):
        if key in tool_input:
            val = str(tool_input[key])
            return val[:70] + ("…" if len(val) > 70 else "")
    return ""


job_lock = threading.Lock()
job_state = {
    "running": False,
    "scope": None,
    "started_at": None,
    "finished_at": None,
    "last_status": None,   # "ok" | "error" | None
    "last_error": None,
    "last_output_tail": None,
    "progress": [],   # list of short strings, most recent last
    "current_stage": None,
    "stage_index": 0,
    "stage_total": STAGE_TOTAL,
    "last_success_at": None,
    "last_summary": None,
    "completed_sections": None,
    "alert_count": None,
}
job_state.update(load_saved_state())


def _push_progress(text):
    with job_lock:
        job_state["progress"].append(text)
        if len(job_state["progress"]) > PROGRESS_MAX:
            job_state["progress"] = job_state["progress"][-PROGRESS_MAX:]


def run_check_job(scope_key, force=False):
    """Returns True if started, or a reason string if refused."""
    if not force:
        busy = recently_edited_files()
        if busy:
            names = "、".join(f"{n}({a}秒前)" for n, a in busy)
            return (
                f"偵測到 {names} 剛被修改,可能有對話或另一個複查正在編輯。"
                f"為避免覆蓋未儲存的內容,本次複查已取消。"
                f"確認沒人在編輯後,可加上 force=1 重試。"
            )
    with job_lock:
        if job_state["running"]:
            return False
        job_state["running"] = True
        job_state["scope"] = SCOPES.get(scope_key, scope_key or "完整複查")
        job_state["started_at"] = time.time()
        job_state["last_status"] = None
        job_state["last_error"] = None
        job_state["progress"] = []
        job_state["current_stage"] = "準備更新"
        job_state["stage_index"] = 1

    def worker():
        claude_bin = find_claude_binary()
        full_output_lines = []
        try:
            if not claude_bin:
                raise RuntimeError(
                    "找不到 claude 執行檔,請確認 Claude Code CLI 已安裝並在 PATH 或常見安裝路徑中"
                )
            oauth_token = load_oauth_token()
            if not oauth_token:
                raise RuntimeError(
                    "找不到長期 OAuth token,請執行 `claude setup-token` 並將輸出的 token 存到 "
                    "server/.claude_oauth_token"
                )
            scope_arg = SCOPES.get(scope_key, scope_key or "")
            prompt = load_check_prompt(scope_arg)
            env = dict(os.environ)
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

            _push_progress(f"🚀 開始複查({SCOPES.get(scope_key, scope_key or '完整複查')})")

            snap = snapshot_data_files()
            if snap:
                _push_progress(f"🛟 已備份至 backups/{os.path.basename(snap)}")

            proc = subprocess.Popen(
                [
                    claude_bin, "-p", prompt,
                    "--dangerously-skip-permissions",
                    "--output-format", "stream-json",
                    "--verbose",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )

            for line in proc.stdout:
                full_output_lines.append(line)
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                etype = evt.get("type")
                if etype == "assistant":
                    for block in evt.get("message", {}).get("content", []):
                        btype = block.get("type")
                        if btype == "tool_use":
                            stage_index, stage_label = stage_for_tool(block.get("name"))
                            set_stage(stage_index, stage_label)
                            summary = _summarize_tool_input(block.get("name"), block.get("input"))
                            label = block.get("name", "工具")
                            _push_progress(f"🔧 {label}" + (f": {summary}" if summary else ""))
                        elif btype == "text" and block.get("text", "").strip():
                            set_stage(3, "分析與核對")
                            snippet = block["text"].strip().replace("\n", " ")
                            _push_progress("💬 " + snippet[:100] + ("…" if len(snippet) > 100 else ""))
                elif etype == "result":
                    set_stage(4, "完成寫入")
                    subtype = evt.get("subtype", "")
                    _push_progress(f"🏁 完成({subtype})" if subtype else "🏁 完成")

            proc.wait(timeout=900)
            output = "".join(full_output_lines)
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(output)
            with job_lock:
                job_state["last_status"] = "ok" if proc.returncode == 0 else "error"
                job_state["last_output_tail"] = output[-2000:]
                if proc.returncode == 0:
                    completed = 4 if scope_key in ("", "full") else 1
                    alerts = count_stop_alerts()
                    job_state["last_success_at"] = time.time()
                    job_state["completed_sections"] = completed
                    job_state["alert_count"] = alerts
                    job_state["last_summary"] = (
                        f"{completed} 個區塊已更新，發現 {alerts} 項止損警示"
                    )
                    save_success_state()
                else:
                    job_state["last_error"] = f"claude 執行結束,exit code {proc.returncode}"
        except Exception as e:  # noqa: BLE001
            with job_lock:
                job_state["last_status"] = "error"
                job_state["last_error"] = str(e)
            _push_progress(f"❌ 錯誤:{e}")
        finally:
            with job_lock:
                job_state["running"] = False
                job_state["finished_at"] = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PortfolioServer/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel_path, content_type=None):
        abs_path = os.path.join(ROOT, rel_path)
        if not os.path.isfile(abs_path):
            self.send_error(404, "Not found")
            return
        ext = os.path.splitext(abs_path)[1]
        ctype = content_type or MIME.get(ext, "application/octet-stream")
        with open(abs_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _has_valid_token(self, parsed_qs):
        tok = None
        if "token" in parsed_qs:
            tok = parsed_qs["token"][0]
        if not tok:
            cookie = self.headers.get("Cookie", "")
            m = re.search(r"pf_token=([^;]+)", cookie)
            if m:
                tok = m.group(1)
        return tok == TOKEN

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path in PUBLIC_FILES:
            self._send_file(path.lstrip("/"))
            return

        if path == "/api/status":
            if not self._has_valid_token(qs):
                self.send_error(403, "Forbidden")
                return
            with job_lock:
                self._send_json(dict(job_state))
            return

        if path.lstrip("/") in PAGE_DATA_FILES:
            if not self._has_valid_token(qs):
                self.send_error(403, "Forbidden")
                return
            self._send_file(path.lstrip("/"))
            return

        if path in DASHBOARD_FILES:
            if not self._has_valid_token(qs):
                self._send_json(
                    {"error": "需要有效的 token,請用伺服器啟動時印出的完整網址(含 ?token=)開啟"},
                    status=403,
                )
                return
            self.send_response(200)
            fpath = os.path.join(ROOT, DASHBOARD_FILES[path])
            with open(fpath, "rb") as f:
                data = f.read()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Set-Cookie", f"pf_token={TOKEN}; Path=/; Max-Age=31536000")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/check":
            if not self._has_valid_token(qs):
                self.send_error(403, "Forbidden")
                return
            with job_lock:
                already_running = job_state["running"]
            if already_running:
                self._send_json({"accepted": False, "reason": "already_running"}, status=409)
                return
            scope_key = qs.get("scope", [""])[0]
            if scope_key not in SCOPES:
                self._send_json({"accepted": False, "reason": "invalid_scope"}, status=400)
                return
            force = qs.get("force", ["0"])[0] in ("1", "true", "yes")
            started = run_check_job(scope_key, force=force)
            if isinstance(started, str):
                self._send_json(
                    {"accepted": False, "reason": "recently_edited", "detail": started},
                    status=409,
                )
                return
            self._send_json({"accepted": True, "scope": SCOPES.get(scope_key, "完整複查")})
            return

        if parsed.path == "/api/ranking/refresh":
            if not self._has_valid_token(qs):
                self.send_error(403, "Forbidden")
                return
            if not ranking_lock.acquire(blocking=False):
                self._send_json({"ok": False, "reason": "already_running"}, status=409)
                return
            try:
                proc = subprocess.run([sys.executable, RANKING_SCRIPT], cwd=ROOT,
                                      capture_output=True, text=True, timeout=300)
            finally:
                ranking_lock.release()
            self._send_json({"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-2000:]},
                            status=200 if proc.returncode == 0 else 500)
            return

        self.send_error(404, "Not found")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Portfolio server running on http://0.0.0.0:{PORT}")
    print(f"Local URL:  http://localhost:{PORT}/?token={TOKEN}")
    print(f"Token file: {TOKEN_FILE}")
    claude_bin = find_claude_binary()
    print(f"claude binary: {claude_bin or 'NOT FOUND — /api/check will fail until this is fixed'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
