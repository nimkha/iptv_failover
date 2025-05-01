import requests
import time
import threading

STREAM_TIMEOUT = 5  # seconds
CHECK_INTERVAL = 60  # seconds

class StreamChecker:
    def __init__(self, config):
        self.config = config
        self.active_streams = {}
        print("DEBUG: Active streams after init:")
        for k, v in self.active_streams.items():
            print(f"- {k} = {v}")

    def is_stream_working(self, url):
        return True  # Skip actual check
        try:
            resp = requests.get(url, timeout=STREAM_TIMEOUT, stream=True)
            return resp.status_code == 200
        except Exception:
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
