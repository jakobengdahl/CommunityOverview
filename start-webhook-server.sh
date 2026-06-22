#!/bin/bash
#
# Webhook Dev Server
#
# Starts a local HTTP server that logs all incoming requests — URL path,
# method, headers and body — to the console.
#
# Use this during development to inspect EventSubscription webhook deliveries:
# create a subscription with webhook_url pointing to http://localhost:PORT/
# and watch live events as nodes are created, updated or deleted in the graph.
#
# Usage:
#   ./start-webhook-server.sh [port]
#
# Default port: 9000

PORT="${1:-9000}"

BOLD='\033[1m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
DIM='\033[2m'
NC='\033[0m'

echo -e "${BOLD}${BLUE}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Community Knowledge Graph              ║"
echo "  ║   Webhook Dev Server                     ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  Listening on ${BOLD}http://localhost:${PORT}${NC}"
echo -e "  ${DIM}Any path, any method — all requests are logged below.${NC}"
echo -e "  ${DIM}Point your EventSubscription webhook_url to http://localhost:${PORT}/your-path${NC}"
echo ""
echo -e "  ${YELLOW}Press Ctrl+C to stop.${NC}"
echo ""

# Prefer Python 3 (available everywhere)
if command -v python3 &>/dev/null; then
    python3 -u - "$PORT" <<'PYEOF'
import sys
import json
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(sys.argv[1])

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[0;36m"
GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
RED    = "\033[0;31m"

def colour_method(method):
    colours = {"POST": GREEN, "GET": CYAN, "PUT": YELLOW, "DELETE": RED, "PATCH": YELLOW}
    c = colours.get(method, BOLD)
    return f"{c}{BOLD}{method}{RESET}"

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress default access log

    def _handle(self):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length > 0 else b""

        print(f"\n{DIM}{'─' * 60}{RESET}")
        print(f"  {DIM}{ts}{RESET}  {colour_method(self.command)}  {BOLD}{self.path}{RESET}")

        for h in ("Content-Type", "User-Agent", "X-Event-Type", "X-Graph-Origin"):
            v = self.headers.get(h)
            if v:
                print(f"  {DIM}{h}:{RESET} {v}")

        if raw_body:
            ct = self.headers.get("Content-Type", "")
            if "json" in ct:
                try:
                    parsed = json.loads(raw_body)
                    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
                    lines = pretty.splitlines()
                    print(f"\n  {BOLD}Body:{RESET}")
                    for line in lines[:60]:
                        print(f"    {line}")
                    if len(lines) > 60:
                        print(f"    {DIM}… ({len(lines) - 60} more lines){RESET}")
                except Exception:
                    print(f"\n  {BOLD}Body:{RESET} {raw_body[:500]}")
            else:
                snippet = raw_body[:500].decode("utf-8", errors="replace")
                print(f"\n  {BOLD}Body:{RESET} {snippet}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"received"}')

    def do_GET(self):    self._handle()
    def do_POST(self):   self._handle()
    def do_PUT(self):    self._handle()
    def do_PATCH(self):  self._handle()
    def do_DELETE(self): self._handle()

server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\n\n  Webhook server stopped.")
PYEOF

elif command -v node &>/dev/null; then
    node - "$PORT" <<'JSEOF'
const http = require('http');
const PORT = parseInt(process.argv[2]);

const RESET = '\x1b[0m', BOLD = '\x1b[1m', DIM = '\x1b[2m';
const CYAN = '\x1b[36m', GREEN = '\x1b[32m', YELLOW = '\x1b[33m', RED = '\x1b[31m';

function colourMethod(m) {
  const c = {POST: GREEN, GET: CYAN, PUT: YELLOW, DELETE: RED, PATCH: YELLOW}[m] || BOLD;
  return `${c}${BOLD}${m}${RESET}`;
}

http.createServer((req, res) => {
  const ts = new Date().toTimeString().slice(0, 12);
  let body = '';
  req.on('data', d => body += d);
  req.on('end', () => {
    console.log(`\n${DIM}${'─'.repeat(60)}${RESET}`);
    console.log(`  ${DIM}${ts}${RESET}  ${colourMethod(req.method)}  ${BOLD}${req.url}${RESET}`);
    ['content-type','user-agent','x-event-type','x-graph-origin'].forEach(h => {
      if (req.headers[h]) console.log(`  ${DIM}${h}:${RESET} ${req.headers[h]}`);
    });
    if (body) {
      try {
        const parsed = JSON.parse(body);
        const lines = JSON.stringify(parsed, null, 2).split('\n');
        console.log(`\n  ${BOLD}Body:${RESET}`);
        lines.slice(0, 60).forEach(l => console.log(`    ${l}`));
        if (lines.length > 60) console.log(`    ${DIM}… (${lines.length - 60} more lines)${RESET}`);
      } catch {
        console.log(`\n  ${BOLD}Body:${RESET} ${body.slice(0, 500)}`);
      }
    }
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end('{"status":"received"}');
  });
}).listen(PORT, () => {});
JSEOF

else
    echo "Error: neither python3 nor node is available."
    exit 1
fi
