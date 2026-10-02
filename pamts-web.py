#!/usr/bin/env python3
"""PAMTS dashboard: current state and history, over HTTP.

    pamts-web.py --config /etc/pamts/pamts.toml --listen 127.0.0.1:8622

Serves a JSON API and one static page. The page is deliberately dumb -- it asks for
JSON and renders whatever arrives -- so the interesting extension point is
pamts_dash.SOURCES, not this file. Adding a source makes it appear on the page with
no change here.

    GET /                      the dashboard
    GET /api/state             every source's current view, plus per-source errors
    GET /api/history?since=&limit=&labels=PLAY,FETCH
    GET /api/events?window=&buckets=&limit=
                               promotion/demotion history, throughput and
                               utilisation, bucketed for charting
    GET /api/health            PAMTS's own status: its services, stores, collector
                               and its two plugins. Nothing about the media servers
                               themselves -- see the HealthSource docstring.
    GET /api/sources           what is registered and whether it is available
    GET /health

There is NO authentication. It is strictly read-only -- no endpoint changes
anything -- but the data includes file paths and client addresses, so decide
deliberately who can reach it.

The default binds to localhost. `--listen 0.0.0.0:8622` exposes it to your network,
which is reasonable on a trusted LAN and is what you want if the dashboard is only
useful from another machine. Put a reverse proxy in front of it if it ever needs to
leave that network.
"""
import argparse
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pamts_dash                                               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web", "index.html")


class BoundedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a ceiling on concurrent connections.

    The stock class spawns a thread per connection with no limit, so a handful of
    clients that open sockets and never send anything will consume threads until the
    process dies. This is a read-only telemetry page on a LAN, not a hardened service,
    but "someone left a dashboard tab open on a flaky wifi link" is enough to want a
    bound. Requests beyond the limit wait rather than being refused.
    """
    max_workers = 16
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._slots = threading.BoundedSemaphore(self.max_workers)

    def process_request_thread(self, request, client_address):
        with self._slots:
            super().process_request_thread(request, client_address)


def make_handler(sources, page_path, auth_token=None):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "pamts-web"

        def _send(self, body, ctype="application/json", code=200):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # The JSON endpoints return file paths and client addresses; stop a
            # browser deciding for itself that any of it is HTML.
            self.send_header("X-Content-Type-Options", "nosniff")
            # The page polls; never let a proxy or browser serve stale state.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self):
            """No token configured -> open, as before. With one, require it.

            Checked with compare_digest so a wrong token cannot be found a character at
            a time. Accepted as a header or a query parameter: a browser can only
            manage the latter, and this page is opened in a browser.
            """
            if not auth_token:
                return True
            given = (self.headers.get("X-PAMTS-Token")
                     or parse_qs(urlparse(self.path).query).get("token", [""])[0])
            return hmac.compare_digest(str(given), str(auth_token))

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authorised():
                self._send({"error": "unauthorised"}, code=401)
                return
            try:
                if u.path in ("/", "/index.html"):
                    try:
                        with open(page_path, "rb") as f:
                            self._send(f.read(), "text/html; charset=utf-8")
                    except OSError:
                        self._send("dashboard page not found at "
                                   f"{page_path}", "text/plain", 500)
                elif u.path == "/health":
                    self._send({"ok": True})
                elif u.path == "/api/health":
                    # PAMTS's own health. Deliberately its own endpoint rather than
                    # part of /api/state: it is the one thing worth polling on its own
                    # when something looks wrong, and it must stay answerable even if
                    # an expensive source (a tier scan) is slow.
                    hs = next((x for x in sources if x.name == "health"), None)
                    if hs is None:
                        self._send({"error": "health source not enabled"}, code=503)
                    else:
                        self._send(hs.get(force=q.get("force", ["0"])[0]
                                          not in ("0", "", "false")))
                elif u.path == "/api/sources":
                    self._send({"sources": [
                        {"name": s.name, "available": s.available(), "ttl": s.ttl}
                        for s in sources]})
                elif u.path == "/api/state":
                    force = q.get("force", ["0"])[0] not in ("0", "", "false")
                    data, errors = pamts_dash.collect_all(sources, force=force)
                    self._send({"generated": time.time(), "data": data,
                                "errors": errors})
                elif u.path == "/api/history":
                    since = float(q.get("since", ["0"])[0])
                    limit = int(q.get("limit", ["200"])[0])
                    # labels= restricts the verdicts returned. Omit it for all of
                    # them; labels=demand is shorthand for whatever the observer
                    # counts as demand, so the page need not hardcode the set.
                    raw = ",".join(q.get("labels", []))
                    labels = [x.strip().upper() for x in raw.split(",") if x.strip()]
                    if labels == ["DEMAND"]:
                        labels = list(pamts_dash.DEMAND_LABELS)
                    hist = next((s for s in sources if s.name == "history"), None)
                    if hist is None or not hist.available():
                        self._send({"error": "history source unavailable"}, code=503)
                    else:
                        self._send({"generated": time.time(),
                                    **hist.collect(since=since, limit=limit,
                                                   labels=labels or None)})
                elif u.path == "/api/events":
                    # window= seconds of history, buckets= how many points to return.
                    # Bucketing happens in SQL: a night of eviction is ten thousand
                    # rows and the page draws a few hundred pixels.
                    # Clamped, and NaN-proof: float("nan") passes any comparison
                    # test you write the obvious way, so compare the value against
                    # itself first. An hour is the shortest useful window; ten years
                    # is past the point where more data could mean anything.
                    window = float(q.get("window", [str(86400 * 7)])[0])
                    if not (window == window):          # NaN
                        window = 86400 * 7
                    window = max(3600.0, min(window, 86400 * 3650))
                    buckets = max(2, min(400, int(q.get("buckets", ["48"])[0])))
                    limit = max(1, min(2000, int(q.get("limit", ["200"])[0])))
                    ev = next((s for s in sources if s.name == "events"), None)
                    if ev is None or not ev.available():
                        self._send({"error": "no event history yet; it appears once "
                                             "PAMTS has moved something"}, code=503)
                    else:
                        self._send({"generated": time.time(),
                                    **ev.collect(limit=limit, buckets=buckets,
                                                 window=window)})
                else:
                    self._send({"error": "not found"}, code=404)
            except Exception as e:                              # noqa: BLE001
                self._send({"error": f"{type(e).__name__}: {e}"}, code=500)

        def log_message(self, *a):
            pass
    return H


def source_cfg(args):
    """One flat config the sources pick their own keys out of."""
    cfg = {
        "url": args.observer_url,
        "state_file": args.state_file,
        "config_file": args.config,
        "observer_db": args.observer_db,
        "events_db": args.events_db,
        "history_limit": args.history_limit,
        "watermarks_file": args.watermarks_file,
    }
    # The tier source needs to know which jobs are tier jobs and what the budget is.
    # Read it from the config rather than making the operator repeat it.
    if args.config and os.path.exists(args.config):
        try:
            import tomllib                                      # noqa: PLC0415
            with open(args.config, "rb") as f:
                raw = tomllib.load(f)
            cfg["tier_jobs"] = [j for j in (raw.get("jobs") or [])
                                if j.get("mode") == "tier"]
            cfg["budget_gb"] = (raw.get("tier") or {}).get("budget_gb")
            # Eviction evicts to budget MINUS this, so the dashboard needs it to show
            # the line that actually governs rather than the nominal budget.
            cfg["promote_headroom_gb"] = (raw.get("promote") or {}).get("headroom_gb", 0)
            # Nothing else is duplicated here: ConfigSource reads this same file and
            # returns the rest. Copying it into the source config as well just created
            # two places to keep in step.
            if not cfg.get("events_db"):
                cfg["events_db"] = (raw.get("paths") or {}).get("events_db")
        except Exception as e:                                  # noqa: BLE001
            print(f"warning: could not read {args.config}: {e}", file=sys.stderr)
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="PAMTS dashboard")
    ap.add_argument("--config", default=os.environ.get("PAMTS_CONFIG",
                                                       "/etc/pamts/pamts.toml"))
    ap.add_argument("--observer-url", default="http://127.0.0.1:8621")
    ap.add_argument("--observer-db", default="/var/lib/pamts/observer.db")
    ap.add_argument("--state-file", default="/var/lib/pamts/state.json")
    ap.add_argument("--watermarks-file", default="/var/lib/pamts/watermarks.json",
                    help="the play-cursor file. Its freshness is how the status panel "
                         "knows the promotion poller is actually running, independently "
                         "of what systemd reports.")
    ap.add_argument("--events-db", default="/var/lib/pamts/events.db",
                    help="the transfer/utilisation history pamts_events writes")
    ap.add_argument("--listen", default="127.0.0.1:8622",
                    help="host:port. Defaults to localhost because there is no "
                         "authentication; use 0.0.0.0:8622 to expose it on a "
                         "trusted network")
    ap.add_argument("--page", default=PAGE)
    ap.add_argument("--history-limit", type=int, default=200)
    ap.add_argument("--auth-token-file",
                    help="file holding a shared secret. When set, every request must "
                         "carry it as X-PAMTS-Token or ?token=. Unset means open, "
                         "which is fine on localhost and a choice anywhere else.")
    ap.add_argument("--sources", default="",
                    help="comma-separated subset of "
                         + ",".join(sorted(pamts_dash.SOURCES)))
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.sources.split(",") if n.strip()] or None
    sources = pamts_dash.build(names, cfg={n: source_cfg(args)
                                          for n in pamts_dash.SOURCES})
    print("sources: " + ", ".join(
        f"{s.name}{'' if s.available() else ' (unavailable)'}" for s in sources),
        file=sys.stderr)

    host, _, port = args.listen.rpartition(":")
    token = None
    if args.auth_token_file:
        try:
            token = open(args.auth_token_file).read().strip() or None
        except OSError as e:
            print(f"cannot read {args.auth_token_file}: {e}", file=sys.stderr)
            return 2
    if not token and (host not in ("127.0.0.1", "localhost", "::1")):
        print(f"warning: listening on {host} with no --auth-token-file; anyone on the "
              "network can read this dashboard", file=sys.stderr)
    httpd = BoundedHTTPServer((host or "127.0.0.1", int(port)),
                              make_handler(sources, args.page, token))
    print(f"serving on {args.listen}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
