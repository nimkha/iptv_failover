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

FUZZY_MATCH_THRESHOLD = 70  # similarity threshold

def auto_reload_m3u(interval=172800):  # every 48 hour
    while True:
        new_config = parse_m3u_files("input/")
        checker.config = new_config
        print("[M3U Reloaded]")
        time.sleep(interval)

def normalize_name(name):
    """Robust normalization with proper regex escaping"""
    if not name:
        return "Unknown", "unknown.uk"
    
    # Remove unwanted prefixes/suffixes - properly escaped regex
    name = re.sub(r'(?i)\s*\([^)]*\)|\s*\[[^]]*\]', '', name)  # Remove () and []
    name = re.sub(r'(?i)\b(?:hd|fhd|uhd|4k|sd)\s*\d*\b', '', name)  # Remove quality
    name = re.sub(r'(?i)^(?:uk\s*:|dstv\s*:|epl\s*:|ss\s*:)\s*', '', name)  # Remove prefixes
    
    # Clean up and capitalize
    name = re.sub(r'[^\w\s]', '', name)  # Remove punctuation
    name = re.sub(r'\s+', ' ', name).strip()
    
    # Handle channel numbers (preserve main number)
    name = re.sub(r'(?i)\b(?:bt|tnt|sky|ss|supersport)\s+(?:sports?\s+)?(\d+).*', r'\1', name)
    
    display_name = " ".join(word.capitalize() for word in name.split())
    tvg_id = re.sub(r'\s+', '', name.lower()) + '.uk'
    
    return display_name, tvg_id

def load_epg_map(epg_path):
    """Create mapping from EPG data: {display-name: tvg-id}"""
    epg_map = {}
    try:
        tree = ET.parse(epg_path)
        for channel in tree.findall(".//channel"):
            tvg_id = channel.get("id")
            if not tvg_id:
                continue
            for name in channel.findall("display-name"):
                if name.text:
                    epg_map[name.text.strip()] = tvg_id
    except Exception as e:
        print(f"EPG parsing error: {e}")
    return epg_map

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

def parse_m3u_entry(extinf_line):
    """Parse EXTINF line with all attributes"""
    entry = {
        'tvg-id': '',
        'tvg-name': '',
        'tvg-logo': '',
        'group-title': '',
        'name': '',
        'extinf': extinf_line
    }
    
    # Extract attributes
    attr_match = re.search(r'#EXTINF:-1\s+(.*?),', extinf_line)
    if attr_match:
        attrs = attr_match.group(1)
        for attr in attrs.split(' '):
            if '=' in attr:
                key, val = attr.split('=', 1)
                val = val.strip('"')
                if key in entry:
                    entry[key] = val
    
    # Extract display name (after last comma)
    name_match = extinf_line.rsplit(',', 1)
    if len(name_match) > 1:
        entry['name'] = name_match[1].strip()
    
    return entry

def parse_m3u_files(m3u_folder="input/"):
    """Parse M3U files with EPG integration and dynamic channel mapping"""
    entries = []
    
    # Load EPG data first
    epg_map = load_epg_map(os.path.join(m3u_folder, "guide.xml"))
    
    # Load and parse channels.txt
    channel_variations = defaultdict(list)
    try:
        with open(os.path.join(m3u_folder, "channels.txt"), "r") as f:
            current_group = "General"
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                if line.startswith("#"):
                    current_group = line[1:].strip()
                    continue
                
                # Find matching EPG ID if available
                epg_id = None
                epg_name = None
                for epg_key in epg_map.keys():
                    if fuzz.ratio(line.lower(), epg_key.lower()) > 85:
                        epg_id = epg_map[epg_key]
                        epg_name = epg_key
                        break
                
                # Store with EPG data if found
                if epg_id:
                    channel_variations[line] = {
                        'canonical': line,
                        'epg_id': epg_id,
                        'epg_name': epg_name,
                        'variations': [line]
                    }
                else:
                    channel_variations[line] = {
                        'canonical': line,
                        'epg_id': None,
                        'variations': [line]
                    }
                
                # Generate common variations
                no_prefix = re.sub(r'^(UK|US|DSTV|EPL)\s*[:]?\s*', '', line, flags=re.I)
                if no_prefix != line:
                    channel_variations[line]['variations'].append(no_prefix)
                
                if 'Sky Sports' in line:
                    channel_variations[line]['variations'].append(line.replace('Sky Sports', 'SS'))
                
                if re.search(r'\d+$', line):
                    channel_variations[line]['variations'].append(re.sub(r'\s*\d+$', '', line))
                    
    except FileNotFoundError:
        print("Note: channels.txt not found - using EPG and basic normalization only")
    except Exception as e:
        print(f"Error reading channels.txt: {e}")

    # Parse M3U files
    for m3u_file in glob.glob(f"{m3u_folder}/*.m3u"):
        try:
            with open(m3u_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"Error reading {m3u_file}: {e}")
            continue

        current_entry = None
        for line in lines:
            line = line.strip()
            if line.startswith("#EXTINF"):
                current_entry = parse_m3u_entry(line)
            elif line.startswith("http") and current_entry:
                current_entry['url'] = line
                entries.append(current_entry)
                current_entry = None

    grouped = defaultdict(list)
    for entry in entries:
        try:
            original_name = entry['name']
            matched_channel = None
            
            # Try to match against all known channel variations
            for channel_data in channel_variations.values():
                for variant in channel_data['variations']:
                    if fuzz.partial_ratio(original_name.lower(), variant.lower()) > 80:
                        matched_channel = channel_data
                        break
                if matched_channel:
                    break
            
            if matched_channel:
                # Use EPG data if available
                if matched_channel['epg_id']:
                    display_name = matched_channel['epg_name']
                    tvg_id = matched_channel['epg_id']
                else:
                    display_name = matched_channel['canonical']
                    tvg_id = generate_tvg_id(display_name)
            else:
                # Fallback to EPG matching
                epg_match = None
                for epg_name, epg_id in epg_map.items():
                    if fuzz.partial_ratio(original_name.lower(), epg_name.lower()) > 75:
                        epg_match = (epg_name, epg_id)
                        break
                
                if epg_match:
                    display_name, tvg_id = epg_match
                else:
                    display_name, tvg_id = normalize_name(original_name)
            
            entry.update({
                'canonical_name': display_name,
                'tvg-id': entry.get('tvg-id', tvg_id),
                'tvg-name': display_name,
                'group-title': display_name
            })
            
            grouped[display_name].append(entry)
            
        except Exception as e:
            print(f"Error processing entry {original_name}: {e}")
            continue
    
    return {"channels": grouped}

def fuzzy_match(channel_name, pattern):
    """Check if channel name matches a pattern with fuzzy matching"""
    channel_clean = re.sub(r'[^a-z0-9]', '', channel_name.lower())
    pattern_clean = re.sub(r'[^a-z0-9]', '', pattern.lower())
    return fuzz.partial_ratio(channel_clean, pattern_clean) > 80

def generate_tvg_id(channel_name):
    """Fallback tvg-id generation when no EPG match exists"""
    base_id = re.sub(r'[^a-z0-9]', '', channel_name.lower())
    return f"{base_id}.fallback"

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
        active_streams = checker.get_active_streams()
        
        for channel, entry in active_streams.items():
            if not entry:
                continue
                
            extinf = (f'#EXTINF:-1 tvg-id="{entry["tvg-id"]}" '
                    f'tvg-name="{entry["tvg-name"]}" '
                    f'tvg-logo="{entry.get("tvg-logo", "")}" '
                    f'group-title="{entry["group-title"]}",'
                    f'{entry["canonical_name"]}')
            
            m3u += f"{extinf}\n{entry['url']}\n"
        
        return Response(m3u, mimetype="application/x-mpegURL")

    @flask_app.route("/failover/<channel>")
    def failover_channel(channel):
        checker.mark_stream_failed(channel)
        return f"Failover triggered for channel: {channel}\n"
    
    @flask_app.route("/epg.xml")
    def serve_modified_epg():
        """Serve modified EPG with normalized display names"""
        try:
            tree = ET.parse("input/guide.xml")
            root = tree.getroot()
            
            # Create mapping of tvg-id to canonical names from our config
            name_map = {
                entry['tvg-id']: entry['canonical_name']
                for entries in checker.config["channels"].values()
                for entry in entries
                if 'tvg-id' in entry
            }
            
            # Update display names in EPG
            for channel in root.findall(".//channel"):
                tvg_id = channel.get("id")
                if tvg_id in name_map:
                    for display_name in channel.findall("display-name"):
                        display_name.text = name_map[tvg_id]
            
            # Return modified EPG
            epg_data = ET.tostring(root, encoding='utf-8', method='xml')
            return Response(epg_data, mimetype="application/xml")
            
        except Exception as e:
            print(f"EPG modification error: {e}")
            return Response("# Error processing EPG", status=500)

    return flask_app


# Used by Gunicorn
app = create_app()

if __name__ == "__main__":
    threading.Thread(target=auto_reload_m3u, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
