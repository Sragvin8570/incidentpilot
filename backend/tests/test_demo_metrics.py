import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from demo.app import Handler


def test_demo_exposes_prometheus_metrics() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/metrics") as response:
            metrics = response.read().decode()

        assert response.status == 200
        assert "incidentpilot_demo_up 1" in metrics
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

