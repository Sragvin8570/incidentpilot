from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/metrics":
            body = b"# HELP incidentpilot_demo_up Demo workload availability.\n# TYPE incidentpilot_demo_up gauge\nincidentpilot_demo_up 1\n"
            content_type = "text/plain; version=0.0.4"
        elif self.path == "/healthz":
            body = b"ok\n"
            content_type = "text/plain"
        else:
            body = b"IncidentPilot demo workload\n"
            content_type = "text/plain"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

