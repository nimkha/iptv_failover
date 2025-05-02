import requests
import time
import threading

STREAM_TIMEOUT = 5  # seconds
CHECK_INTERVAL = 60  # seconds

class StreamChecker:
    def __init__(self, config):
        self.config = config
        self.active_streams = {}

    def is_stream_working(self, url):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            }
            response = requests.get(url, headers=headers, timeout=5, stream=True, allow_redirects=True)
            return response.status_code in [200, 301, 302]
        except Exception as e:
            print(f"[CHECK FAILED] {url} → {e}")
            return False


    def update_streams(self):
        while True:
            for channel, urls in self.config["channels"].items():
                for url in urls:
                    if self.is_stream_working(url):
                        self.active_streams[channel] = url
                        break
                else:
                    self.active_streams[channel] = None
            time.sleep(CHECK_INTERVAL)

    def start_background_check(self):
        thread = threading.Thread(target=self.update_streams, daemon=True)
        thread.start()
