import time
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
import socketserver

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

class MockHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Simulate network latency
        time.sleep(0.1)
        payload = {
            "nodes": [
                {"id": "n1", "type": "Actor", "name": "A"}
            ],
            "edges": []
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, format, *args):
        pass

def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server

def run_benchmark():
    server = start_server()
    port = server.server_address[1]

    graphs = []
    for i in range(20):
        graphs.append({
            "graph_id": f"graph-{i}",
            "display_name": f"Graph {i}",
            "enabled": True,
            "endpoints": {
                "graph_json_url": f"http://127.0.0.1:{port}/graph.json"
            }
        })

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "graphs": graphs,
        }
    })

    manager = FederationManager(config)

    print("Starting sync_all...")
    start_t = time.perf_counter()
    import asyncio

    # Handle both sync and async manager.sync_all()
    if asyncio.iscoroutinefunction(manager.sync_all):
        result = asyncio.run(manager.sync_all())
    else:
        result = manager.sync_all()

    end_t = time.perf_counter()

    server.shutdown()
    server.server_close()

    print(f"Time taken: {end_t - start_t:.3f} seconds")
    print(f"Success: {result.get('success')}")

if __name__ == "__main__":
    run_benchmark()
