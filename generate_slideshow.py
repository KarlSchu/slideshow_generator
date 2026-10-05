#!/usr/bin/env python3
"""
Builds/updates slides.md (a Markdown file: '# ' / '## ' lines are section
headings, '![alt](file)' lines are images/videos in slideshow order, and any
other line is ignored) from the media files in the 'slides' subfolder and
generates slideshow-gen.html (editable, next to slides.md) and
slides/index.html (read-only, next to the media). Serves the files over
HTTP so the editable slideshow's drag-to-reorder index can save the new
order/captions back into slides.md.

Supported media: png, apng, jpg, jpeg, jfif, gif, webp, avif, bmp, ico, svg,
mp4, pdf, md (except slides.md).

Usage:
    python3 generate_slideshow.py [--dir DIR] [--port PORT] [--no-serve] [--fix-videos]

    --dir DIR      Project directory containing the 'slides' folder (default: current directory)
    --port PORT    Port for the local editing server (default: 8000)
    --no-serve     Only (re)generate slides.md, slideshow-gen.html, and index.html; don't start the server
    --serve-only   Start the server using the existing slides.md/slideshow-gen.html without regenerating them
    --fix-videos   Re-encode any .mp4 with a non-H.264 video codec to H.264/AAC with faststart (requires ffmpeg/ffprobe)
"""

"""
Tasks:
- [x] make the image decription in the main pane multi line if to long or <br/> in the description. Make only one vertical scrollbar
- [x] use slideshow-gen.html for the editable view and index.html for the read-only and exported view
- [x] back up slides.md as slides_TIMESTAMP.md when saving if it already exists
- [ ] add support for video metadata (duration, codec, etc.)
- [x] video transformation `ffmpeg -y -i "036-dynamic-scuteniering-2924-09-20.mp4" -c:v libx264 -profile:v high -pix_fmt yuv420p -c:a aac -b:a 192k "036-dynamic-scuteniering-2924-09-20_h264.mp4"`
"""
import argparse
import datetime
import errno
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
import zipfile

IMAGE_EXTS = {".png", ".apng", ".jpg", ".jpeg", ".jfif", ".gif", ".webp", ".avif", ".bmp", ".ico", ".svg"}
VIDEO_EXTS = {".mp4"}
DOCUMENT_EXTS = {".pdf", ".md"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | DOCUMENT_EXTS
SLIDES_DIRNAME = "slides"
LIST_FILENAME = "slides.md"
EDITABLE_HTML_FILENAME = "index.html"
HTML_FILENAME = "slideshow.html"
IMAGE_LINE_RE = re.compile(r"^!\[(?P<alt>.*)\]\(\s*(?P<name>.+?)\s*\)\s*$")


def find_images(directory):
    names = []
    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        if not os.path.isfile(path):
            continue
        if entry.lower() != LIST_FILENAME.lower() and os.path.splitext(entry)[1].lower() in MEDIA_EXTS:
            names.append(entry)
    return sorted(names, key=str.lower)


def read_list(list_path):
    if not os.path.exists(list_path):
        return []
    with open(list_path, "r", encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def write_list(list_path, lines):
    with open(list_path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(f"{line}\n")


def get_video_codec(path):
    """Returns the video stream's codec name (e.g. 'h264', 'mpeg4') via ffprobe,
    or None if ffprobe is unavailable or the codec can't be determined."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def fix_video_codecs(directory, target_codec="h264"):
    """Re-encodes any .mp4 whose video stream isn't H.264 (e.g. old mpeg4/DivX-style
    encodes that browsers can't decode, leaving only audio playing) into H.264/AAC,
    replacing the file in place while keeping the original as a '.bak' backup."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ffmpeg/ffprobe not found on PATH; skipping video codec check.", file=sys.stderr)
        return
    for name in find_images(directory):
        if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
            continue
        path = os.path.join(directory, name)
        codec = get_video_codec(path)
        if codec is None or codec == target_codec:
            continue
        backup_path = path + ".bak"
        if os.path.exists(backup_path):
            print(f"Skipping {name}: backup {os.path.basename(backup_path)} already exists", file=sys.stderr)
            continue
        print(f"Re-encoding {name} (codec: {codec} -> {target_codec}) for browser compatibility...")
        fixed_path = path + ".fixing.mp4"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-profile:v", "high",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", fixed_path],
                capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError as e:
            print(f"  Failed to re-encode {name}: {e.stderr.strip()[-500:]}", file=sys.stderr)
            if os.path.exists(fixed_path):
                os.remove(fixed_path)
            continue
        os.rename(path, backup_path)
        os.rename(fixed_path, path)
        print(f"  Done. Original backed up as {os.path.basename(backup_path)}")


def is_heading(line):
    return line.startswith("#")


def parse_image_line(line):
    """Parses a Markdown image/video line '![alt](name)' into (name, alt),
    or returns None if the line isn't a recognized image line."""
    m = IMAGE_LINE_RE.match(line)
    if not m:
        return None
    return m.group("name").strip(), m.group("alt").strip()


def format_image_line(name, comment):
    return f"![{comment}]({name})"


def line_to_entry(line):
    """Converts a line to a slideshow entry dict, or None if the line should
    be ignored (anything that isn't a heading or a media line)."""
    if is_heading(line):
        stripped = line.lstrip("#")
        level = len(line) - len(stripped)
        return {"type": "subtitle", "level": level, "text": stripped.strip()}
    parsed = parse_image_line(line)
    if parsed is None:
        return None
    name, comment = parsed
    return {"type": "image", "name": name, "comment": comment}


def enrich_media_entries(directory, entries):
    try:
        from PIL import ExifTags, Image
    except ImportError:
        ExifTags = None
        Image = None

    enriched_entries = []
    for entry in entries:
        enriched = dict(entry)
        if entry["type"] == "image":
            path = os.path.join(directory, entry["name"])
            metadata = {"File": entry["name"]}
            if os.path.isfile(path):
                metadata["File size"] = f"{os.path.getsize(path):,} bytes"
                if os.path.splitext(entry["name"])[1].lower() == ".md":
                    with open(path, "r", encoding="utf-8") as markdown_file:
                        enriched["markdown"] = markdown_file.read()
                elif Image is not None:
                    try:
                        with Image.open(path) as image:
                            metadata["Format"] = image.format or "Unknown"
                            metadata["Dimensions"] = f"{image.width} x {image.height}"
                            for tag_id, value in image.getexif().items():
                                if isinstance(value, bytes):
                                    try:
                                        value = value.decode("utf-8").strip("\x00")
                                    except UnicodeDecodeError:
                                        continue
                                if value not in (None, ""):
                                    label = ExifTags.TAGS.get(tag_id, f"Tag {tag_id}")
                                    metadata[label] = str(value)
                    except (OSError, ValueError):
                        pass
            enriched["metadata"] = metadata
        enriched_entries.append(enriched)
    return enriched_entries


def entry_to_line(entry):
    if entry["type"] == "subtitle":
        prefix = "#" * entry["level"]
        return f"{prefix} {entry['text']}" if entry["text"] else prefix
    return format_image_line(entry["name"], entry["comment"])


def merge_save_lines(existing_lines, clean_entries):
    """Updates heading/media slots in slides.md and preserves other lines.
    Any extra entries are appended; unused recognized lines are removed."""
    result = []
    entry_index = 0
    for line in existing_lines:
        if is_heading(line) or parse_image_line(line) is not None:
            if entry_index < len(clean_entries):
                result.append(entry_to_line(clean_entries[entry_index]))
                entry_index += 1
        else:
            result.append(line)
    result.extend(entry_to_line(entry) for entry in clean_entries[entry_index:])
    return result


def update_list(directory, slides_dir):
    """Reads slides.md (if present), drops media lines for files that no
    longer exist in slides_dir, appends new media files found there, and
    rewrites the file - the line order determines the slideshow order.
    Headings and any other non-image lines are preserved as-is; unrecognized
    lines are kept in the file but ignored by the slideshow."""
    list_path = os.path.join(directory, LIST_FILENAME)
    on_disk = find_images(slides_dir)
    on_disk_set = set(on_disk)

    existing = []
    existing_set = set()
    for line in read_list(list_path):
        if is_heading(line):
            existing.append(line)
            continue
        parsed = parse_image_line(line)
        if parsed is None:
            existing.append(line)
            continue
        name, comment = parsed
        if name in on_disk_set:
            existing.append(format_image_line(name, comment))
            existing_set.add(name)

    new_files = [name for name in on_disk if name not in existing_set]
    ordered = existing + [format_image_line(name, "") for name in new_files]

    write_list(list_path, ordered)
    return ordered


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Slideshow</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #111; color: #eee; display: flex; height: 100vh; overflow: hidden; }
  #main { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; position: relative; min-width: 0; min-height: 0; }
  #viewer { display: flex; flex: 1 1 auto; align-items: center; justify-content: center; width: 95%; min-height: 0; overflow: hidden; touch-action: none; }
  #viewer img, #viewer video { display: block; max-width: 100%; max-height: 100%; object-fit: contain; box-shadow: 0 0 20px rgba(0,0,0,.6); transform-origin: center; }
  #viewer iframe { flex: 1; width: 100%; height: 100%; border: 0; background: #fff; }
  #caption { flex: 0 0 auto; max-width: 95%; margin: 10px 0; font-size: 14px; opacity: .8; overflow-wrap: anywhere; text-align: center; }
  .zoom-controls { position: absolute; top: 16px; right: 16px; z-index: 2; display: flex; align-items: center; gap: 6px; padding: 5px; background: rgba(17,17,17,.9); border: 1px solid #444; border-radius: 4px; }
  .zoom-controls[hidden] { display: none; }
  .zoom-controls button { min-width: 32px; height: 30px; background: #292929; border: 1px solid #444; border-radius: 3px; color: #fff; cursor: pointer; }
  .zoom-controls button:hover:not(:disabled) { background: #3a3a3a; }
  .zoom-controls button:disabled { opacity: .45; cursor: default; }
  #zoomLevel { min-width: 44px; font-size: 12px; text-align: center; }
  .metadata-dialog { position: fixed; top: 16px; right: 16px; left: auto; width: min(520px, calc(100vw - 32px)); max-height: 75vh; overflow: auto; margin: 0; padding: 20px; box-sizing: border-box; background: #1a1a1a; border: 1px solid #555; border-radius: 4px; color: #eee; }
  .metadata-dialog::backdrop { background: rgba(0,0,0,.7); }
  .metadata-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
  .metadata-header h2 { margin: 0; font-size: 18px; }
  .metadata-header button { width: 32px; height: 32px; background: #292929; border: 1px solid #444; border-radius: 3px; color: #fff; cursor: pointer; }
  .metadata-dialog dl { display: grid; grid-template-columns: minmax(110px, .4fr) minmax(0, 1fr); gap: 8px 16px; margin: 0; }
  .metadata-dialog dt { color: #aaa; overflow-wrap: anywhere; }
  .metadata-dialog dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }
  #metadataEmpty { margin: 16px 0 0; color: #aaa; font-size: 13px; }
  .nav-btn { position: absolute; top: 50%; transform: translateY(-50%); background: rgba(0,0,0,.4); border: none; color: #fff; font-size: 32px; padding: 10px 16px; cursor: pointer; border-radius: 4px; }
  .nav-btn:hover { background: rgba(0,0,0,.7); }
  #prevBtn { left: 20px; }
  #nextBtn { right: 20px; }
  #resizer { flex: 0 0 6px; width: 6px; cursor: col-resize; background: #222; }
  #resizer:hover, #resizer.resizing { background: #2d6cdf; }
  #index { flex: 0 0 auto; width: 220px; min-width: 180px; max-width: 50vw; min-height: 0; display: flex; flex-direction: column; overflow: hidden; padding: 10px; box-sizing: border-box; }
  #index h3 { margin: 0 0 8px; font-size: 14px; }
  #indexHeader { flex: 0 0 auto; background: #111; }
  #thumbs { flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden; }
  #indexButtons { display: flex; gap: 4px; margin-bottom: 6px; }
  #indexButtons button { flex: 1 1 0; min-width: 0; padding: 4px 2px; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; background: #3a3a3a; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
  #indexButtons button:hover { background: #505050; }
  #saveBtn { background: #2d6cdf !important; }
  #saveBtn:hover { background: #1e56b8 !important; }
  #saveStatus { font-size: 12px; min-height: 16px; margin-bottom: 8px; opacity: .8; }
  .thumb { display: flex; align-items: center; gap: 8px; padding: 4px; margin-bottom: 4px; background: #1c1c1c; border-radius: 4px; cursor: grab; border: 2px solid transparent; }
  .thumb.active { border-color: #2d6cdf; }
  .thumb.dragging { opacity: .4; }
  .thumb img, .thumb video { width: 48px; height: 36px; object-fit: cover; border-radius: 2px; flex-shrink: 0; }
  .thumb .thumb-icon { width: 48px; height: 36px; border-radius: 2px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; background: #262626; font-size: 18px; }
  .subtitle { padding: 10px 4px 4px; color: #8fb3e8; font-size: 12px; font-weight: 600; }
  .thumb .num { width: 20px; font-size: 11px; opacity: .6; flex-shrink: 0; }
  .thumb .name { flex: 1; min-width: 0; font-size: 11px; background: transparent; border: 1px solid transparent; border-radius: 2px; color: inherit; font-family: inherit; padding: 2px 4px; }
  .thumb .name:hover, .thumb .name:focus { border-color: #444; background: #262626; }
</style>
</head>
<body>
  <div id="main">
    <button id="prevBtn" class="nav-btn">&#8249;</button>
    <div id="viewer"></div>
    <div id="caption"></div>
    <div id="zoomControls" class="zoom-controls" aria-label="Image zoom" hidden>
      <button id="zoomOutBtn" type="button" aria-label="Zoom out" title="Zoom out">&minus;</button>
      <output id="zoomLevel" aria-live="polite">100%</output>
      <button id="zoomInBtn" type="button" aria-label="Zoom in" title="Zoom in">&plus;</button>
      <button id="zoomResetBtn" type="button" aria-label="Reset zoom" title="Reset zoom">Reset</button>
      <button id="metadataBtn" type="button" aria-label="Image metadata" title="Image metadata">&#9432;</button>
    </div>
    <button id="nextBtn" class="nav-btn">&#8250;</button>
  </div>
  <dialog id="metadataDialog" class="metadata-dialog">
    <div class="metadata-header">
      <h2>Image metadata</h2>
      <button id="metadataCloseBtn" type="button" aria-label="Close metadata">&times;</button>
    </div>
    <dl id="metadataList"></dl>
    <p id="metadataEmpty" hidden>No embedded EXIF metadata found.</p>
  </dialog>
  <div id="resizer"></div>
  <div id="index">
    <div id="indexHeader">
      <h3>Index (drag to reorder)</h3>
      <div id="indexButtons">
        <button id="reloadBtn" type="button" aria-label="Reload" title="Reload: rescan the slides folder; keeps current order and captions, drops missing files and appends newly added files (not saved until you click Save)">&#8635; Reload</button>
        <button id="saveBtn" type="button" aria-label="Save" title="Save: write the current order and captions to slides.md (a timestamped backup of the existing slides.md is made first)">&#128190; Save</button>
        <button id="exportBtn" type="button" aria-label="Export" title="Export: download a ZIP containing a standalone read-only index.html plus all referenced media files">&#11015; Export</button>
      </div>
      <div id="saveStatus"></div>
    </div>
    <div id="thumbs"></div>
  </div>

<script>
const MEDIA_BASE = __MEDIA_BASE__;
const VIDEO_EXTS = __VIDEO_EXTS__;
const isVideoName = (name) => VIDEO_EXTS.some((ext) => name.toLowerCase().endsWith(ext));
const isDocumentName = (name) => /\\.(pdf|md)$/i.test(name);
const mediaSrc = (name) => MEDIA_BASE + encodeURIComponent(name);
let entries = __IMAGES_JSON__;
let images = entries.filter(e => e.type === 'image');
let current = 0;
let dragSrcIndex = null;

const viewer = document.getElementById('viewer');
const caption = document.getElementById('caption');
const thumbsEl = document.getElementById('thumbs');
const zoomControls = document.getElementById('zoomControls');
const zoomLevel = document.getElementById('zoomLevel');
const zoomOutBtn = document.getElementById('zoomOutBtn');
const zoomInBtn = document.getElementById('zoomInBtn');
const metadataBtn = document.getElementById('metadataBtn');
const metadataDialog = document.getElementById('metadataDialog');
const metadataList = document.getElementById('metadataList');
const metadataEmpty = document.getElementById('metadataEmpty');
const saveStatus = document.getElementById('saveStatus');
let zoomTarget = null;
let zoomScale = 1;
let panX = 0;
let panY = 0;
let panPointerId = null;
let panStartX = 0;
let panStartY = 0;

function displayName(entry) {
  return entry.comment ? entry.comment : entry.name;
}

function markdownDocument(text) {
  const escaped = text.replace(/[&<>]/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;'
  })[char]);
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    body { margin: 0; padding: 16px; color: #000; background: #fff; font: 14px/1.5 system-ui, sans-serif; }
    pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
  </style></head><body><pre>${escaped}</pre></body></html>`;
}

function updateCaption(entry) {
  caption.replaceChildren(document.createTextNode(`${current + 1} / ${images.length} - `));
  displayName(entry).split(new RegExp('<br */?>', 'i')).forEach((part, index) => {
    if (index > 0) caption.appendChild(document.createElement('br'));
    caption.appendChild(document.createTextNode(part));
  });
}

function applyZoom() {
  if (!zoomTarget) return;
  const maxX = Math.max(0, (zoomTarget.offsetWidth * zoomScale - viewer.clientWidth) / 2);
  const maxY = Math.max(0, (zoomTarget.offsetHeight * zoomScale - viewer.clientHeight) / 2);
  panX = Math.max(-maxX, Math.min(maxX, panX));
  panY = Math.max(-maxY, Math.min(maxY, panY));
  zoomTarget.style.transform = `translate(${panX}px, ${panY}px) scale(${zoomScale})`;
  zoomTarget.style.cursor = zoomScale > 1 ? (panPointerId === null ? 'grab' : 'grabbing') : '';
  zoomLevel.textContent = `${Math.round(zoomScale * 100)}%`;
  zoomOutBtn.disabled = zoomScale <= 1;
  zoomInBtn.disabled = zoomScale >= 4;
}

function setZoom(nextScale, focusX = 0, focusY = 0) {
  if (!zoomTarget) return;
  const boundedScale = Math.max(1, Math.min(4, nextScale));
  const ratio = boundedScale / zoomScale;
  panX = focusX - (focusX - panX) * ratio;
  panY = focusY - (focusY - panY) * ratio;
  zoomScale = boundedScale;
  applyZoom();
}

function resetZoom() {
  zoomTarget = viewer.querySelector('img');
  zoomScale = 1;
  panX = 0;
  panY = 0;
  panPointerId = null;
  zoomControls.hidden = !zoomTarget;
  if (zoomTarget) {
    zoomTarget.draggable = false;
    applyZoom();
  } else {
    zoomLevel.textContent = '100%';
  }
}

function showMetadata(entry) {
  const metadata = { ...(entry.metadata || {}) };
  if (zoomTarget) metadata["Pixel dimensions"] = `${zoomTarget.naturalWidth} x ${zoomTarget.naturalHeight}`;
  metadataList.replaceChildren();
  Object.entries(metadata).forEach(([label, value]) => {
    const term = document.createElement('dt');
    term.textContent = label;
    const detail = document.createElement('dd');
    detail.textContent = value;
    metadataList.append(term, detail);
  });
  const baseFields = new Set(['File', 'File size', 'Format', 'Dimensions', 'Pixel dimensions']);
  metadataEmpty.hidden = !Object.keys(metadata).some((key) => !baseFields.has(key));
  metadataDialog.showModal();
  positionMetadataDialog();
}

function positionMetadataDialog() {
  const anchor = metadataBtn.getBoundingClientRect();
  const dialog = metadataDialog.getBoundingClientRect();
  const margin = 16;
  const left = Math.max(margin, Math.min(anchor.right - dialog.width, window.innerWidth - dialog.width - margin));
  const top = Math.max(margin, Math.min(anchor.bottom + 8, window.innerHeight - dialog.height - margin));
  metadataDialog.style.left = `${left}px`;
  metadataDialog.style.right = 'auto';
  metadataDialog.style.top = `${top}px`;
}

metadataBtn.addEventListener('click', () => showMetadata(images[current]));
document.getElementById('metadataCloseBtn').addEventListener('click', () => metadataDialog.close());
window.addEventListener('resize', () => {
  if (metadataDialog.open) positionMetadataDialog();
});
zoomOutBtn.addEventListener('click', () => setZoom(zoomScale / 1.25));
zoomInBtn.addEventListener('click', () => setZoom(zoomScale * 1.25));
document.getElementById('zoomResetBtn').addEventListener('click', () => {
  zoomScale = 1;
  panX = 0;
  panY = 0;
  applyZoom();
});
viewer.addEventListener('wheel', (e) => {
  if (!zoomTarget) return;
  e.preventDefault();
  const rect = viewer.getBoundingClientRect();
  const direction = e.deltaY < 0 ? 1.15 : 1 / 1.15;
  setZoom(zoomScale * direction, e.clientX - rect.left - rect.width / 2, e.clientY - rect.top - rect.height / 2);
}, { passive: false });
viewer.addEventListener('pointerdown', (e) => {
  if (!zoomTarget || zoomScale <= 1 || e.button !== 0) return;
  panPointerId = e.pointerId;
  panStartX = e.clientX - panX;
  panStartY = e.clientY - panY;
  viewer.setPointerCapture(e.pointerId);
  applyZoom();
});
viewer.addEventListener('pointermove', (e) => {
  if (e.pointerId !== panPointerId) return;
  panX = e.clientX - panStartX;
  panY = e.clientY - panStartY;
  applyZoom();
});
function endPan(e) {
  if (e.pointerId !== panPointerId) return;
  panPointerId = null;
  applyZoom();
}
viewer.addEventListener('pointerup', endPan);
viewer.addEventListener('pointercancel', endPan);

function showImage(i) {
  if (!images.length) {
    current = 0;
    viewer.replaceChildren();
    resetZoom();
    caption.textContent = 'No media files found';
    return;
  }
  current = (i + images.length) % images.length;
  viewer.replaceChildren();
  const entry = images[current];
  const isVideo = isVideoName(entry.name);
  const media = document.createElement(isVideo ? 'video' : isDocumentName(entry.name) ? 'iframe' : 'img');
  if (media.tagName === 'IFRAME') {
    media.title = entry.name;
    if (entry.name.toLowerCase().endsWith('.md')) {
      media.setAttribute('sandbox', '');
      media.srcdoc = markdownDocument(entry.markdown || '');
    } else {
      media.src = mediaSrc(entry.name);
    }
  } else {
    media.src = mediaSrc(entry.name);
    media.alt = entry.name;
  }
  if (isVideo) {
    media.controls = true;
    media.autoplay = true;
  }
  viewer.appendChild(media);
  resetZoom();
  updateCaption(entry);
  document.querySelectorAll('.thumb').forEach((el, idx) => {
    el.classList.toggle('active', idx === current);
  });
}

function renderThumbs() {
  thumbsEl.innerHTML = '';
  let imageIndex = 0;
  entries.forEach((entry) => {
    if (entry.type === 'subtitle') {
      const subtitle = document.createElement('div');
      subtitle.className = 'subtitle';
      subtitle.textContent = entry.text;
      thumbsEl.appendChild(subtitle);
      return;
    }
    const idx = imageIndex++;
    const item = document.createElement('div');
    item.className = 'thumb';
    item.draggable = true;
    item.dataset.index = idx;

    const num = document.createElement('span');
    num.className = 'num';
    num.textContent = (idx + 1) + '.';

    let img;
    if (isVideoName(entry.name) || isDocumentName(entry.name)) {
      img = document.createElement('div');
      img.className = 'thumb-icon';
      img.textContent = isVideoName(entry.name) ? '🎬' : entry.name.toLowerCase().endsWith('.pdf') ? 'PDF' : 'MD';
    } else {
      img = document.createElement('img');
      img.src = mediaSrc(entry.name);
      img.loading = 'lazy';
    }

    const nameEl = document.createElement('input');
    nameEl.className = 'name';
    nameEl.type = 'text';
    nameEl.value = displayName(entry);
    nameEl.title = entry.name;
    nameEl.addEventListener('click', (e) => e.stopPropagation());
    nameEl.addEventListener('dragstart', (e) => e.stopPropagation());
    nameEl.addEventListener('change', () => {
      const value = nameEl.value.trim();
      entry.comment = (value === entry.name) ? '' : value;
      if (current === idx) updateCaption(entry);
      saveStatus.textContent = 'Comment changed (not saved yet)';
    });

    item.appendChild(num);
    item.appendChild(img);
    item.appendChild(nameEl);

    item.addEventListener('click', () => showImage(idx));

    item.addEventListener('dragstart', (e) => {
      dragSrcIndex = idx;
      item.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });
    item.addEventListener('dragend', () => item.classList.remove('dragging'));
    item.addEventListener('dragover', (e) => e.preventDefault());
    item.addEventListener('drop', (e) => {
      e.preventDefault();
      const targetIndex = idx;
      if (dragSrcIndex === null || dragSrcIndex === targetIndex) return;
      const activeEntry = images[current];
      const [moved] = images.splice(dragSrcIndex, 1);
      images.splice(targetIndex, 0, moved);
      let imageCursor = 0;
      entries = entries.map((entry) => entry.type === 'subtitle' ? entry : images[imageCursor++]);
      dragSrcIndex = null;
      renderThumbs();
      const newCurrent = images.indexOf(activeEntry);
      showImage(newCurrent >= 0 ? newCurrent : 0);
      saveStatus.textContent = 'Order changed (not saved yet)';
    });

    thumbsEl.appendChild(item);
  });
}

document.getElementById('prevBtn').addEventListener('click', () => showImage(current - 1));
document.getElementById('nextBtn').addEventListener('click', () => showImage(current + 1));
document.addEventListener('keydown', (e) => {
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  if (e.key === 'ArrowLeft') showImage(current - 1);
  if (e.key === 'ArrowRight') showImage(current + 1);
});

const resizer = document.getElementById('resizer');
const indexEl = document.getElementById('index');
let resizing = false;
resizer.addEventListener('mousedown', () => {
  resizing = true;
  resizer.classList.add('resizing');
  document.body.style.userSelect = 'none';
});
document.addEventListener('mousemove', (e) => {
  if (!resizing) return;
  const newWidth = document.body.clientWidth - e.clientX - resizer.offsetWidth;
  const clamped = Math.min(Math.max(newWidth, 180), document.body.clientWidth * 0.5);
  indexEl.style.flexBasis = clamped + 'px';
});
document.addEventListener('mouseup', () => {
  if (!resizing) return;
  resizing = false;
  resizer.classList.remove('resizing');
  document.body.style.userSelect = '';
});

document.getElementById('saveBtn').addEventListener('click', async () => {
  saveStatus.textContent = 'Saving...';
  try {
    const res = await fetch('/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries })
    });
    if (!res.ok) throw new Error(await res.text());
    saveStatus.textContent = 'Saved to slides.md';
  } catch (err) {
    saveStatus.textContent = 'Save failed: ' + err.message;
  }
});

document.getElementById('reloadBtn').addEventListener('click', async () => {
  const currentName = images[current]?.name;
  saveStatus.textContent = 'Reloading media files...';
  try {
    const res = await fetch('/reload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries })
    });
    if (!res.ok) throw new Error(await res.text());
    entries = await res.json();
    images = entries.filter((entry) => entry.type === 'image');
    renderThumbs();
    const currentIndex = images.findIndex((entry) => entry.name === currentName);
    showImage(currentIndex >= 0 ? currentIndex : Math.min(current, images.length - 1));
    saveStatus.textContent = `Reloaded ${images.length} media files`;
  } catch (err) {
    saveStatus.textContent = 'Reload failed: ' + err.message;
  }
});

document.getElementById('exportBtn').addEventListener('click', async () => {
  saveStatus.textContent = 'Exporting...';
  try {
    const res = await fetch('/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries })
    });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'slideshow_export.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    saveStatus.textContent = 'Exported slideshow_export.zip';
  } catch (err) {
    saveStatus.textContent = 'Export failed: ' + err.message;
  }
});

renderThumbs();
showImage(0);
</script>
</body>
</html>
"""


STATIC_EXPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Slideshow</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #111; color: #eee; display: flex; height: 100vh; overflow: hidden; }
  #main { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; position: relative; min-width: 0; min-height: 0; }
  #viewer { display: flex; flex: 1 1 auto; align-items: center; justify-content: center; width: 95%; min-height: 0; overflow: hidden; touch-action: none; }
  #viewer img, #viewer video { display: block; max-width: 100%; max-height: 100%; object-fit: contain; box-shadow: 0 0 20px rgba(0,0,0,.6); transform-origin: center; }
  #viewer iframe { flex: 1; width: 100%; height: 100%; border: 0; background: #fff; }
  #caption { flex: 0 0 auto; max-width: 95%; margin: 10px 0; font-size: 14px; opacity: .8; overflow-wrap: anywhere; text-align: center; }
  .zoom-controls { position: absolute; top: 16px; right: 16px; z-index: 2; display: flex; align-items: center; gap: 6px; padding: 5px; background: rgba(17,17,17,.9); border: 1px solid #444; border-radius: 4px; }
  .zoom-controls[hidden] { display: none; }
  .zoom-controls button { min-width: 32px; height: 30px; background: #292929; border: 1px solid #444; border-radius: 3px; color: #fff; cursor: pointer; }
  .zoom-controls button:hover:not(:disabled) { background: #3a3a3a; }
  .zoom-controls button:disabled { opacity: .45; cursor: default; }
  #zoomLevel { min-width: 44px; font-size: 12px; text-align: center; }
  .metadata-dialog { position: fixed; top: 16px; right: 16px; left: auto; width: min(520px, calc(100vw - 32px)); max-height: 75vh; overflow: auto; margin: 0; padding: 20px; box-sizing: border-box; background: #1a1a1a; border: 1px solid #555; border-radius: 4px; color: #eee; }
  .metadata-dialog::backdrop { background: rgba(0,0,0,.7); }
  .metadata-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
  .metadata-header h2 { margin: 0; font-size: 18px; }
  .metadata-header button { width: 32px; height: 32px; background: #292929; border: 1px solid #444; border-radius: 3px; color: #fff; cursor: pointer; }
  .metadata-dialog dl { display: grid; grid-template-columns: minmax(110px, .4fr) minmax(0, 1fr); gap: 8px 16px; margin: 0; }
  .metadata-dialog dt { color: #aaa; overflow-wrap: anywhere; }
  .metadata-dialog dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }
  #metadataEmpty { margin: 16px 0 0; color: #aaa; font-size: 13px; }
  .nav-btn { position: absolute; top: 50%; transform: translateY(-50%); background: rgba(0,0,0,.4); border: none; color: #fff; font-size: 32px; padding: 10px 16px; cursor: pointer; border-radius: 4px; }
  .nav-btn:hover { background: rgba(0,0,0,.7); }
  #prevBtn { left: 20px; }
  #nextBtn { right: 20px; }
  #resizer { flex: 0 0 6px; width: 6px; cursor: col-resize; background: #222; }
  #resizer:hover, #resizer.resizing { background: #2d6cdf; }
  #index { flex: 0 0 auto; width: 220px; min-width: 180px; max-width: 50vw; min-height: 0; overflow-y: auto; overflow-x: hidden; padding: 10px; box-sizing: border-box; }
  #index h3 { position: sticky; top: -10px; z-index: 2; background: #111; margin: -10px -10px 10px; padding: 10px 10px 0; font-size: 14px; }
  .thumb { display: flex; align-items: center; gap: 8px; padding: 4px; margin-bottom: 4px; background: #1c1c1c; border-radius: 4px; cursor: pointer; border: 2px solid transparent; }
  .thumb.active { border-color: #2d6cdf; }
  .thumb img, .thumb video { width: 48px; height: 36px; object-fit: cover; border-radius: 2px; flex-shrink: 0; }
  .thumb .thumb-icon { width: 48px; height: 36px; border-radius: 2px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; background: #262626; font-size: 18px; }
  .subtitle { padding: 10px 4px 4px; color: #8fb3e8; font-size: 12px; font-weight: 600; }
  .thumb .num { width: 20px; font-size: 11px; opacity: .6; flex-shrink: 0; }
  .thumb .name { flex: 1; min-width: 0; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
</head>
<body>
  <div id="main">
    <button id="prevBtn" class="nav-btn">&#8249;</button>
    <div id="viewer"></div>
    <div id="caption"></div>
    <div id="zoomControls" class="zoom-controls" aria-label="Image zoom" hidden>
      <button id="zoomOutBtn" type="button" aria-label="Zoom out" title="Zoom out">&minus;</button>
      <output id="zoomLevel" aria-live="polite">100%</output>
      <button id="zoomInBtn" type="button" aria-label="Zoom in" title="Zoom in">&plus;</button>
      <button id="zoomResetBtn" type="button" aria-label="Reset zoom" title="Reset zoom">Reset</button>
      <button id="metadataBtn" type="button" aria-label="Image metadata" title="Image metadata">&#9432;</button>
    </div>
    <button id="nextBtn" class="nav-btn">&#8250;</button>
  </div>
  <dialog id="metadataDialog" class="metadata-dialog">
    <div class="metadata-header">
      <h2>Image metadata</h2>
      <button id="metadataCloseBtn" type="button" aria-label="Close metadata">&times;</button>
    </div>
    <dl id="metadataList"></dl>
    <p id="metadataEmpty" hidden>No embedded EXIF metadata found.</p>
  </dialog>
  <div id="resizer"></div>
  <div id="index">
    <h3>Index</h3>
    <div id="thumbs"></div>
  </div>

<script>
const MEDIA_BASE = __MEDIA_BASE__;
const VIDEO_EXTS = __VIDEO_EXTS__;
const isVideoName = (name) => VIDEO_EXTS.some((ext) => name.toLowerCase().endsWith(ext));
const isDocumentName = (name) => /\\.(pdf|md)$/i.test(name);
const mediaSrc = (name) => MEDIA_BASE + encodeURIComponent(name);
const entries = __IMAGES_JSON__;
const images = entries.filter(e => e.type === 'image');
let current = 0;

const viewer = document.getElementById('viewer');
const caption = document.getElementById('caption');
const thumbsEl = document.getElementById('thumbs');
const zoomControls = document.getElementById('zoomControls');
const zoomLevel = document.getElementById('zoomLevel');
const zoomOutBtn = document.getElementById('zoomOutBtn');
const zoomInBtn = document.getElementById('zoomInBtn');
const metadataBtn = document.getElementById('metadataBtn');
const metadataDialog = document.getElementById('metadataDialog');
const metadataList = document.getElementById('metadataList');
const metadataEmpty = document.getElementById('metadataEmpty');
let zoomTarget = null;
let zoomScale = 1;
let panX = 0;
let panY = 0;
let panPointerId = null;
let panStartX = 0;
let panStartY = 0;

function displayName(entry) {
  return entry.comment ? entry.comment : entry.name;
}

function markdownDocument(text) {
  const escaped = text.replace(/[&<>]/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;'
  })[char]);
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    body { margin: 0; padding: 16px; color: #000; background: #fff; font: 14px/1.5 system-ui, sans-serif; }
    pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
  </style></head><body><pre>${escaped}</pre></body></html>`;
}

function updateCaption(entry) {
  caption.replaceChildren(document.createTextNode(`${current + 1} / ${images.length} - `));
  displayName(entry).split(new RegExp('<br */?>', 'i')).forEach((part, index) => {
    if (index > 0) caption.appendChild(document.createElement('br'));
    caption.appendChild(document.createTextNode(part));
  });
}

function applyZoom() {
  if (!zoomTarget) return;
  const maxX = Math.max(0, (zoomTarget.offsetWidth * zoomScale - viewer.clientWidth) / 2);
  const maxY = Math.max(0, (zoomTarget.offsetHeight * zoomScale - viewer.clientHeight) / 2);
  panX = Math.max(-maxX, Math.min(maxX, panX));
  panY = Math.max(-maxY, Math.min(maxY, panY));
  zoomTarget.style.transform = `translate(${panX}px, ${panY}px) scale(${zoomScale})`;
  zoomTarget.style.cursor = zoomScale > 1 ? (panPointerId === null ? 'grab' : 'grabbing') : '';
  zoomLevel.textContent = `${Math.round(zoomScale * 100)}%`;
  zoomOutBtn.disabled = zoomScale <= 1;
  zoomInBtn.disabled = zoomScale >= 4;
}

function setZoom(nextScale, focusX = 0, focusY = 0) {
  if (!zoomTarget) return;
  const boundedScale = Math.max(1, Math.min(4, nextScale));
  const ratio = boundedScale / zoomScale;
  panX = focusX - (focusX - panX) * ratio;
  panY = focusY - (focusY - panY) * ratio;
  zoomScale = boundedScale;
  applyZoom();
}

function resetZoom() {
  zoomTarget = viewer.querySelector('img');
  zoomScale = 1;
  panX = 0;
  panY = 0;
  panPointerId = null;
  zoomControls.hidden = !zoomTarget;
  if (zoomTarget) {
    zoomTarget.draggable = false;
    applyZoom();
  } else {
    zoomLevel.textContent = '100%';
  }
}

function showMetadata(entry) {
  const metadata = { ...(entry.metadata || {}) };
  if (zoomTarget) metadata["Pixel dimensions"] = `${zoomTarget.naturalWidth} x ${zoomTarget.naturalHeight}`;
  metadataList.replaceChildren();
  Object.entries(metadata).forEach(([label, value]) => {
    const term = document.createElement('dt');
    term.textContent = label;
    const detail = document.createElement('dd');
    detail.textContent = value;
    metadataList.append(term, detail);
  });
  const baseFields = new Set(['File', 'File size', 'Format', 'Dimensions', 'Pixel dimensions']);
  metadataEmpty.hidden = !Object.keys(metadata).some((key) => !baseFields.has(key));
  metadataDialog.showModal();
  positionMetadataDialog();
}

function positionMetadataDialog() {
  const anchor = metadataBtn.getBoundingClientRect();
  const dialog = metadataDialog.getBoundingClientRect();
  const margin = 16;
  const left = Math.max(margin, Math.min(anchor.right - dialog.width, window.innerWidth - dialog.width - margin));
  const top = Math.max(margin, Math.min(anchor.bottom + 8, window.innerHeight - dialog.height - margin));
  metadataDialog.style.left = `${left}px`;
  metadataDialog.style.right = 'auto';
  metadataDialog.style.top = `${top}px`;
}

metadataBtn.addEventListener('click', () => showMetadata(images[current]));
document.getElementById('metadataCloseBtn').addEventListener('click', () => metadataDialog.close());
window.addEventListener('resize', () => {
  if (metadataDialog.open) positionMetadataDialog();
});
zoomOutBtn.addEventListener('click', () => setZoom(zoomScale / 1.25));
zoomInBtn.addEventListener('click', () => setZoom(zoomScale * 1.25));
document.getElementById('zoomResetBtn').addEventListener('click', () => {
  zoomScale = 1;
  panX = 0;
  panY = 0;
  applyZoom();
});
viewer.addEventListener('wheel', (e) => {
  if (!zoomTarget) return;
  e.preventDefault();
  const rect = viewer.getBoundingClientRect();
  const direction = e.deltaY < 0 ? 1.15 : 1 / 1.15;
  setZoom(zoomScale * direction, e.clientX - rect.left - rect.width / 2, e.clientY - rect.top - rect.height / 2);
}, { passive: false });
viewer.addEventListener('pointerdown', (e) => {
  if (!zoomTarget || zoomScale <= 1 || e.button !== 0) return;
  panPointerId = e.pointerId;
  panStartX = e.clientX - panX;
  panStartY = e.clientY - panY;
  viewer.setPointerCapture(e.pointerId);
  applyZoom();
});
viewer.addEventListener('pointermove', (e) => {
  if (e.pointerId !== panPointerId) return;
  panX = e.clientX - panStartX;
  panY = e.clientY - panStartY;
  applyZoom();
});
function endPan(e) {
  if (e.pointerId !== panPointerId) return;
  panPointerId = null;
  applyZoom();
}
viewer.addEventListener('pointerup', endPan);
viewer.addEventListener('pointercancel', endPan);

function showImage(i) {
  current = (i + images.length) % images.length;
  viewer.replaceChildren();
  const entry = images[current];
  const isVideo = isVideoName(entry.name);
  const media = document.createElement(isVideo ? 'video' : isDocumentName(entry.name) ? 'iframe' : 'img');
  if (media.tagName === 'IFRAME') {
    media.title = entry.name;
    if (entry.name.toLowerCase().endsWith('.md')) {
      media.setAttribute('sandbox', '');
      media.srcdoc = markdownDocument(entry.markdown || '');
    } else {
      media.src = mediaSrc(entry.name);
    }
  } else {
    media.src = mediaSrc(entry.name);
    media.alt = entry.name;
  }
  if (isVideo) {
    media.controls = true;
    media.autoplay = true;
  }
  viewer.appendChild(media);
  resetZoom();
  updateCaption(entry);
  document.querySelectorAll('.thumb').forEach((el, idx) => {
    el.classList.toggle('active', idx === current);
  });
}

function renderThumbs() {
  thumbsEl.innerHTML = '';
  let imageIndex = 0;
  entries.forEach((entry) => {
    if (entry.type === 'subtitle') {
      const subtitle = document.createElement('div');
      subtitle.className = 'subtitle';
      subtitle.textContent = entry.text;
      thumbsEl.appendChild(subtitle);
      return;
    }
    const idx = imageIndex++;
    const item = document.createElement('div');
    item.className = 'thumb';

    const num = document.createElement('span');
    num.className = 'num';
    num.textContent = (idx + 1) + '.';

    let img;
    if (isVideoName(entry.name) || isDocumentName(entry.name)) {
      img = document.createElement('div');
      img.className = 'thumb-icon';
      img.textContent = isVideoName(entry.name) ? '🎬' : entry.name.toLowerCase().endsWith('.pdf') ? 'PDF' : 'MD';
    } else {
      img = document.createElement('img');
      img.src = mediaSrc(entry.name);
      img.loading = 'lazy';
    }

    const nameEl = document.createElement('span');
    nameEl.className = 'name';
    nameEl.textContent = displayName(entry);
    nameEl.title = entry.name;

    item.appendChild(num);
    item.appendChild(img);
    item.appendChild(nameEl);
    item.addEventListener('click', () => showImage(idx));

    thumbsEl.appendChild(item);
  });
}

document.getElementById('prevBtn').addEventListener('click', () => showImage(current - 1));
document.getElementById('nextBtn').addEventListener('click', () => showImage(current + 1));
document.addEventListener('keydown', (e) => {
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  if (e.key === 'ArrowLeft') showImage(current - 1);
  if (e.key === 'ArrowRight') showImage(current + 1);
});

const resizer = document.getElementById('resizer');
const indexEl = document.getElementById('index');
let resizing = false;
resizer.addEventListener('mousedown', () => {
  resizing = true;
  resizer.classList.add('resizing');
  document.body.style.userSelect = 'none';
});
document.addEventListener('mousemove', (e) => {
  if (!resizing) return;
  const newWidth = document.body.clientWidth - e.clientX - resizer.offsetWidth;
  const clamped = Math.min(Math.max(newWidth, 180), document.body.clientWidth * 0.5);
  indexEl.style.flexBasis = clamped + 'px';
});
document.addEventListener('mouseup', () => {
  if (!resizing) return;
  resizing = false;
  resizer.classList.remove('resizing');
  document.body.style.userSelect = '';
});

renderThumbs();
showImage(0);
</script>
</body>
</html>
"""


def render_template(template, entries, media_base):
    return (template
            .replace("__MEDIA_BASE__", json.dumps(media_base))
            .replace("__VIDEO_EXTS__", json.dumps(sorted(VIDEO_EXTS)))
            .replace("__IMAGES_JSON__", json.dumps(entries).replace("<", "\\u003c")))


def generate_html(directory, slides_dir, lines):
    entries = [entry for entry in (line_to_entry(line) for line in lines) if entry is not None]
    entries = enrich_media_entries(slides_dir, entries)
    html = render_template(HTML_TEMPLATE, entries, SLIDES_DIRNAME + "/")
    html_path = os.path.join(directory, EDITABLE_HTML_FILENAME)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html_path


def generate_static_export_html(entries):
    """Renders the trimmed-down, dependency-free viewer; media sits next to the HTML file."""
    return render_template(STATIC_EXPORT_TEMPLATE, entries, "")


def generate_static_html(slides_dir, entries):
    entries = enrich_media_entries(slides_dir, entries)
    html_path = os.path.join(slides_dir, HTML_FILENAME)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(generate_static_export_html(entries))
    return html_path


def make_handler(directory, slides_dir):
    list_path = os.path.join(directory, LIST_FILENAME)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, fmt, *args):
            pass

        def _parse_entries(self, raw_entries, on_disk, allow_empty=False):
            """Validates posted entries, returning (clean_entries, image_names_used)."""
            if not isinstance(raw_entries, list):
                raise ValueError("entries must be a list")
            clean = []
            image_names = []
            for entry in raw_entries:
                if not isinstance(entry, dict) or "type" not in entry:
                    raise ValueError("invalid entry")
                if entry["type"] == "subtitle":
                    text = str(entry.get("text", "")).strip()
                    level = entry.get("level", 2)
                    level = min(max(int(level), 1), 6) if isinstance(level, (int, float)) else 2
                    clean.append({"type": "subtitle", "level": level, "text": text})
                elif entry["type"] == "image":
                    name = str(entry.get("name", "")).strip()
                    comment = str(entry.get("comment", "")).strip()
                    if name != os.path.basename(name) or name not in on_disk:
                        continue
                    clean.append({"type": "image", "name": name, "comment": comment})
                    image_names.append(name)
                else:
                    raise ValueError("invalid entry type")
            if not image_names and not allow_empty:
                raise ValueError("no valid image names provided")
            return clean, image_names

        def do_POST(self):
            if self.path not in ("/save", "/export", "/reload"):
                self.send_error(404, "Not found")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                on_disk = set(find_images(slides_dir))
                clean_entries, image_names = self._parse_entries(
                    data.get("entries"), on_disk, allow_empty=self.path == "/reload"
                )
            except Exception as exc:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(str(exc).encode("utf-8"))
                return

            if self.path == "/reload":
                existing_names = {entry["name"] for entry in clean_entries if entry["type"] == "image"}
                clean_entries.extend(
                    {"type": "image", "name": name, "comment": ""}
                    for name in find_images(slides_dir)
                    if name not in existing_names
                )
                response = json.dumps(enrich_media_entries(slides_dir, clean_entries)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                return

            if self.path == "/save":
                if os.path.exists(list_path):
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup_path = os.path.join(directory, f"slides_{timestamp}.md")
                    shutil.copy2(list_path, backup_path)
                lines = merge_save_lines(read_list(list_path), clean_entries)
                write_list(list_path, lines)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
                return

            # /export: bundle a dependency-free viewer with the referenced media files.
            static_html = generate_static_export_html(enrich_media_entries(slides_dir, clean_entries))
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(HTML_FILENAME, static_html)
                for name in image_names:
                    zf.write(os.path.join(slides_dir, name), arcname=name)
            zip_bytes = buffer.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="slideshow_export.zip"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)

    return Handler


def serve(directory, slides_dir, port):
    if not 1 <= port <= 65535:
        print(f"Error: invalid port {port}. Use a number between 1 and 65535.", file=sys.stderr)
        sys.exit(1)
    handler = make_handler(directory, slides_dir)
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as err:
        reasons = {
            errno.EADDRINUSE: "it is already in use by another program (maybe a previous slideshow server is still running)",
            errno.EACCES: "permission denied (ports below 1024 usually need administrator rights)",
        }
        reason = reasons.get(err.errno, err.strerror or str(err))
        print(f"Error: cannot start the server on port {port}: {reason}.", file=sys.stderr)
        print(f"Try a different port, e.g.: python3 generate_slideshow.py --port {port + 1 if port < 65535 else 8000}", file=sys.stderr)
        sys.exit(1)
    url = f"http://127.0.0.1:{port}/{EDITABLE_HTML_FILENAME}"
    print(f"Serving {directory} at {url} (Ctrl+C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
        httpd.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".", help=f"Project directory containing the '{SLIDES_DIRNAME}' media folder (default: current directory)")
    parser.add_argument("--port", type=int, default=8000, help="Port for the local server (default: 8000)")
    parser.add_argument("--no-serve", action="store_true", help="Only generate slides.md, slideshow-gen.html, and index.html")
    parser.add_argument("--serve-only", action="store_true", help="Start the server using existing slides.md and slideshow-gen.html without regenerating them")
    parser.add_argument("--fix-videos", action="store_true", help="Re-encode any .mp4 whose video codec isn't H.264 (e.g. old mpeg4/DivX clips that play audio only in browsers) to H.264/AAC in place, keeping a .bak backup. Requires ffmpeg/ffprobe.")
    args = parser.parse_args()

    directory = os.path.abspath(args.dir)
    if not os.path.isdir(directory):
        print(f"Not a directory: {directory}", file=sys.stderr)
        sys.exit(1)
    slides_dir = os.path.join(directory, SLIDES_DIRNAME)
    if not os.path.isdir(slides_dir):
        print(f"Slides folder not found: {slides_dir}", file=sys.stderr)
        sys.exit(1)

    if args.fix_videos:
        fix_video_codecs(slides_dir)

    if args.serve_only:
        list_path = os.path.join(directory, LIST_FILENAME)
        html_path = os.path.join(directory, EDITABLE_HTML_FILENAME)
        if not os.path.exists(list_path) or not os.path.exists(html_path):
            print(f"--serve-only requires existing {LIST_FILENAME} and {EDITABLE_HTML_FILENAME} in {directory}", file=sys.stderr)
            sys.exit(1)
    else:
        images = update_list(directory, slides_dir)
        entries = [entry for entry in (line_to_entry(line) for line in images) if entry is not None]
        editable_html_path = generate_html(directory, slides_dir, images)
        readonly_html_path = generate_static_html(slides_dir, entries)
        print(f"Wrote {os.path.join(directory, LIST_FILENAME)} ({len(images)} slides)")
        print(f"Wrote {editable_html_path}")
        print(f"Wrote {readonly_html_path}")

    if not args.no_serve:
        serve(directory, slides_dir, args.port)


if __name__ == "__main__":
    main()
