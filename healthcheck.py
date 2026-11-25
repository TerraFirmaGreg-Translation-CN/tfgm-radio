"""Container healthcheck: GET /nowplaying on the port the radio listens on."""
import os
import urllib.request

port = os.environ.get("RADIO_LISTEN", "0.0.0.0:8080").rpartition(":")[2]
token = os.environ.get("RADIO_TOKEN", "")
url = f"http://127.0.0.1:{port}/nowplaying" + (f"?k={token}" if token else "")
urllib.request.urlopen(url, timeout=4).read(1)
