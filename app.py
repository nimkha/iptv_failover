from flask import Flask, Response
import glob
import time
from collections import defaultdict
from rapidfuzz import process, fuzz
from stream_checker import StreamChecker
import threading
import re
import xml.etree.ElementTree as ET
import os

FUZZY_MATCH_THRESHOLD = 80  # similarity threshold

def auto_reload_m3u(interval=3600):  # every hour
    while True:
        new_config = parse_m3u_files("input/")
        checker.config = new_config
        print("[M3U Reloaded]")
        time.sleep(interval)

def normalize_name(name):
    """Clean and normalize channel names for fuzzy comparison."""
    name = name.lower()
    name = re.sub(r'^(uk:|dstv:|epl\s?:|ss:|us:|pt:|ca:|es:|tr:|lb:)', '', name)
    name = re.sub(r'[^\w\s]', '', name)  # remove punctuation
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def load_epg_display_names(epg_path="input/guide.xml"):
    """Extract a list of display names from EPG XML."""
    try:
        tree = ET.parse(epg_path)
        root = tree.getroot()
        display_names = set()

        for channel in root.findall("channel"):
            for name in channel.findall("display-name"):
                if name.text:
                    display_names.add(name.text.strip())

        return list(display_names)
    except Exception as e:
        print(f"WARNING: Failed to parse EPG: {e}")
        return []

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

    # Load EPG names for matching
    epg_names = load_epg_display_names(os.path.join(m3u_folder, "guide.xml"))
    print(f"EPG loaded with {len(epg_names)} names.")

    grouped = {}
    for m3u_name, url in raw_entries:
        norm_m3u = normalize_name(m3u_name)

        # Match to EPG names if available
        best_match = None
        if epg_names:
            result = process.extractOne(norm_m3u, [normalize_name(n) for n in epg_names], scorer=fuzz.token_sort_ratio)
            if result:
                best_match, score, _ = result
                if score < FUZZY_MATCH_THRESHOLD:
                    best_match = None

        key = best_match if best_match else m3u_name  # fallback to original name
        grouped.setdefault(key, []).append(url)

    return {"channels": grouped}

# Load and group streams
config = parse_m3u_files("input/")
checker = StreamChecker(config)
checker.start_background_check()

print("Loaded channels:")
for name, urls in config["channels"].items():
    print(f"- {name}: {len(urls)} stream(s)")

app = Flask(__name__)

@app.route("/playlist.m3u")
def playlist():
    m3u = "#EXTM3U\n"

    print("DEBUG: Active streams:")
    for channel, url in checker.active_streams.items():
        print(f"- {channel} → {url}")
        if url:
            m3u += f"#EXTINF:-1,{channel}\n{url}\n"
            
    for channel, url in checker.active_streams.items():
        if url:
            m3u += f"#EXTINF:-1,{channel}\n{url}\n"
    return Response(m3u, mimetype="application/x-mpegURL")


# Kick off the periodic reload regardless of how the app is run
checker.config = parse_m3u_files("input/")
threading.Thread(target=auto_reload_m3u, daemon=True).start()
# === End auto‑reload setup ===

if __name__ == "__main__":
    threading.Thread(target=auto_reload_m3u, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
