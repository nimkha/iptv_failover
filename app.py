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
    """Normalize channel names by removing resolution tags, region suffixes, punctuation, and excess spaces."""
    name = name.lower()
    name = re.sub(r'\s*\(.*?\)|\[.*?\]', '', name)  # Remove things in () or []
    name = re.sub(r'\b(hd|fhd|uhd|4k|sd|uk|us|ca|au|de|pt|fr)\d*\b', '', name)  # Remove quality + digit suffix
    name = re.sub(r'\b(hd|fhd|uhd|4k|sd|uk|us|ca|au|de|pt|fr)\b', '', name)  # Also remove any that don't have digits
    name = re.sub(r'^(uk:|dstv:|epl\s?:|ss:|us:|pt:|ca:|es:|tr:|lb:)', '', name)
    name = re.sub(r'[^\w\s]', '', name)  # Remove punctuation
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
    raw_entries = []

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

    # Load EPG names
    epg_names = load_epg_display_names(os.path.join(m3u_folder, "guide.xml"))
    norm_epg_names = {normalize_name(name): name for name in epg_names}
    print(f"EPG loaded with {len(epg_names)} names.")

    grouped = {}

    for m3u_name, url in raw_entries:
        norm_m3u = normalize_name(m3u_name)

        # Try to match to EPG
        match_key = None
        if norm_epg_names:
            result = process.extractOne(norm_m3u, norm_epg_names.keys(), scorer=fuzz.token_sort_ratio)
            if result:
                best_match, score, _ = result
                if score >= FUZZY_MATCH_THRESHOLD:
                    match_key = norm_epg_names[best_match]

        # Fallback to normalized m3u name
        if not match_key:
            match_key = norm_m3u

        grouped.setdefault(match_key, []).append(url)

    return {"channels": grouped}

def create_app():
    global checker
    config = parse_m3u_files("input/")
    print("Loaded channels:")
    for name, urls in config["channels"].items():
        print(f"- {name}: {len(urls)} stream(s)")

    checker = StreamChecker(config)

    # Start automatic health monitor
    threading.Thread(target=checker.background_monitor, daemon=True).start()

    flask_app = Flask(__name__)
    
    @flask_app.route("/playlist.m3u")
    def serve_playlist():
        m3u = "#EXTM3U\n"
        for channel, url in checker.get_active_streams().items():
            m3u += f"#EXTINF:-1,{channel}\n{url}\n"
        return Response(m3u, mimetype="application/x-mpegURL")

    @flask_app.route("/failover/<channel>")
    def failover_channel(channel):
        checker.mark_stream_failed(channel)
        return f"Failover triggered for channel: {channel}\n"

    return flask_app


# Used by Gunicorn
app = create_app()

if __name__ == "__main__":
    threading.Thread(target=auto_reload_m3u, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
