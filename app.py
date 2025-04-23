from flask import Flask, Response
import glob
import time
from collections import defaultdict
from rapidfuzz import process, fuzz
from stream_checker import StreamChecker
import threading

FUZZY_MATCH_THRESHOLD = 85  # similarity threshold

def auto_reload_m3u(interval=3600):  # every hour
    while True:
        new_config = parse_m3u_files("input/")
        checker.config = new_config
        print("[M3U Reloaded]")
        time.sleep(interval)

def parse_m3u_files(m3u_folder="input/"):
    raw_entries = []  # List of (channel_name, url)

    for m3u_file in glob.glob(f"{m3u_folder}/*.m3u"):
        with open(m3u_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        channel_name = None
        for line in lines:
            line = line.strip()
            if line.startswith("#EXTINF"):
                parts = line.split(",", 1)
                if len(parts) > 1:
                    channel_name = parts[1].strip()
            elif line.startswith("http") and channel_name:
                raw_entries.append((channel_name, line))
                channel_name = None

    # Fuzzy group similar channel names
    grouped = {}
    for name, url in raw_entries:
        result = process.extractOne(name, grouped.keys(), scorer=fuzz.token_sort_ratio)
        if result:
            match, score, match_index = result
            if score >= FUZZY_MATCH_THRESHOLD:
                grouped[match].append(url)
            else:
                grouped[name] = [url]
        else:
            grouped[name] = [url]

    return {"channels": grouped}

# Load and group streams
config = parse_m3u_files("input/")
checker = StreamChecker(config)
checker.start_background_check()

app = Flask(__name__)

@app.route("/playlist.m3u")
def playlist():
    m3u = "#EXTM3U\n"
    for channel, url in checker.active_streams.items():
        if url:
            m3u += f"#EXTINF:-1,{channel}\n{url}\n"
    return Response(m3u, mimetype="application/x-mpegURL")

if __name__ == "__main__":
    threading.Thread(target=auto_reload_m3u, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
