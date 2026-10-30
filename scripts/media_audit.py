#!/usr/bin/env python3
"""Decode a local video and create timestamped evidence, never a semantic QA claim.

Requires Python 3.10+, NumPy, OpenCV, FFmpeg and FFprobe. Inputs are read-only.
"""
import argparse
import csv
from fractions import Fraction
import html
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave

import cv2
import numpy as np


def run_json(args):
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', check=True)
    return json.loads(result.stdout)


def rational(value):
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining:
        part = stream.read(remaining)
        if not part:
            break
        chunks.append(part)
        remaining -= len(part)
    return b''.join(chunks)


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError('Plan must be a JSON object.')
    frames = plan.get('frame_count')
    if frames is not None and (not isinstance(frames, int) or isinstance(frames, bool) or frames < 1):
        raise ValueError('frame_count must be a positive integer.')
    events = plan.get('events', [])
    for event in events:
        frame = event.get('frame')
        if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
            raise ValueError('Each event needs a nonnegative integer frame.')
        if frames is not None and frame >= frames:
            raise ValueError('Event is at or beyond the exclusive end frame.')
    for span in plan.get('holds', []):
        if len(span) != 2 or not all(isinstance(x, int) and not isinstance(x, bool) for x in span):
            raise ValueError('A hold is [start, end] with integer bounds.')
        if not 0 <= span[0] < span[1] or (frames is not None and span[1] > frames):
            raise ValueError('Invalid half-open hold range.')
    shots = plan.get('shots', [])
    for shot in shots:
        if not isinstance(shot.get('start'), int) or not isinstance(shot.get('end'), int):
            raise ValueError('Shot start/end must be integers.')
        if not 0 <= shot['start'] < shot['end'] or (frames is not None and shot['end'] > frames):
            raise ValueError('Invalid shot range.')
    if shots:
        if shots[0]['start'] != 0 or any(a['end'] != b['start'] for a, b in zip(shots, shots[1:])):
            raise ValueError('Shots must be ordered and contiguous from frame 0.')
        if frames is not None and shots[-1]['end'] != frames:
            raise ValueError('Shot ranges do not cover frame_count.')
    return plan


def audio_candidates(video, stream_index=0):
    # Decode rather than multiplying compressed packet counts (HE-AAC/SBR).
    with tempfile.TemporaryDirectory(prefix='structure-beat-audio-') as folder:
        path = Path(folder) / 'audio.wav'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(video), '-map', f'0:a:{stream_index}',
                        '-vn', '-ac', '1', '-ar', '22050', '-c:a', 'pcm_s16le', str(path)], check=True)
        with wave.open(str(path), 'rb') as source:
            rate = source.getframerate()
            audio = np.frombuffer(source.readframes(source.getnframes()), np.int16).astype(np.float32) / 32768
    size, hop = 1024, 220
    if len(audio) < size + hop:
        return {'decoded_duration_seconds': len(audio) / rate, 'candidates': [], 'status': 'audio_too_short'}
    window = np.hanning(size)
    flux = []
    last = None
    for offset in range(0, len(audio) - size + 1, hop):
        spectrum = np.log1p(np.abs(np.fft.rfft(audio[offset:offset + size] * window)) * 10)
        if last is not None:
            flux.append(float(np.maximum(spectrum - last, 0).sum()))
        last = spectrum
    threshold = float(np.percentile(flux, 70))
    peaks = []
    for index in range(4, len(flux) - 4):
        strength = flux[index]
        if strength > threshold and strength == max(flux[index - 4:index + 5]):
            # flux[index] describes the later STFT window, not its predecessor.
            seconds = ((index + 1) * hop + size / 2) / rate
            peaks.append((seconds, strength))
    selected = []
    for seconds, strength in sorted(peaks, key=lambda x: -x[1]):
        if all(abs(seconds - previous[0]) > .09 for previous in selected):
            selected.append((seconds, strength))
    return {'decoded_duration_seconds': len(audio) / rate, 'status': 'candidates_only',
            'method': 'positive_log_spectral_flux', 'audition_performed': False,
            'candidates': [{'time_seconds': round(t, 6), 'strength': round(s, 4)} for t, s in sorted(selected)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--all-frames', action='store_true')
    parser.add_argument('--step', type=int, default=15)
    parser.add_argument('--columns', type=int, default=4)
    parser.add_argument('--per-sheet', type=int, default=24)
    parser.add_argument('--thumb-width', type=int, default=360)
    parser.add_argument('--native-frames', default='')
    parser.add_argument('--save-events', action='store_true')
    parser.add_argument('--audio-onsets', action='store_true')
    parser.add_argument('--audio-stream', type=int, default=0, help='Audio-track index among audio streams (default 0).')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--change-threshold', type=float, default=12.0)
    parser.add_argument('--hold-threshold', type=float, default=.15)
    args = parser.parse_args()
    if min(args.step, args.columns, args.per_sheet, args.thumb_width) <= 0:
        parser.error('Step, columns, sheet size and thumbnail width must be positive.')
    if args.columns > 8 or args.per_sheet > 120 or args.thumb_width < 80:
        parser.error('Use 1–8 columns, at most 120 tiles, thumbnail width at least 80.')
    if args.change_threshold < 0 or args.hold_threshold < 0:
        parser.error('Thresholds must be nonnegative.')
    if args.audio_stream < 0:
        parser.error('audio-stream must be nonnegative.')
    if not args.video.is_file():
        parser.error('Video does not exist.')
    for command in ['ffmpeg', 'ffprobe']:
        if not shutil.which(command):
            parser.error(f'{command} is required in PATH.')
    try:
        native = {int(x.strip()) for x in args.native_frames.split(',') if x.strip()}
    except ValueError:
        parser.error('native-frames must be comma-separated nonnegative frame numbers.')
    if any(x < 0 for x in native):
        parser.error('native-frames must be nonnegative.')
    plan = validate_plan(json.loads(args.plan.read_text(encoding='utf-8'))) if args.plan else {}
    event_map = {}
    for event in plan.get('events', []):
        event_map.setdefault(event['frame'], []).append(event)
    if args.save_events:
        native.update(event_map)
    video = args.video.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    probe = run_json(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(video)])
    streams = [s for s in probe.get('streams', []) if s.get('codec_type') == 'video' and
               not s.get('disposition', {}).get('attached_pic')]
    if not streams:
        raise ValueError('No video stream.')
    stream = streams[0]
    width, height = int(stream['width']), int(stream['height'])
    rotation = int(round(float(stream.get('tags', {}).get('rotate', 0)))) % 360
    for data in stream.get('side_data_list', []):
        rotation = int(round(float(data.get('rotation', rotation)))) % 360
    if rotation not in (0, 180):
        width, height = height, width
    if stream.get('color_transfer') in ('smpte2084', 'arib-std-b67'):
        raise ValueError('HDR/HLG input: create a colour-managed SDR review proxy first; do not use unlabelled SDR previews.')
    (out / 'probe.json').write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding='utf-8')
    pts_result = run_json(['ffprobe', '-v', 'error', '-select_streams', str(stream['index']),
                          '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(video)])
    timestamps = [float(x['best_effort_timestamp_time']) if 'best_effort_timestamp_time' in x else None
                  for x in pts_result.get('frames', [])]
    fps = rational(stream.get('avg_frame_rate')) or rational(stream.get('r_frame_rate'))
    if fps <= 0:
        raise ValueError('No usable frame rate for fallback timestamps.')
    pts_values = [x for x in timestamps if x is not None]
    intervals = np.diff(pts_values)
    is_vfr = bool(len(intervals) and np.max(np.abs(intervals - np.median(intervals))) > .0002)
    origin = pts_values[0] if pts_values else 0
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(video), '-map', f'0:{stream["index"]}', '-an', '-sn', '-dn']
    if stream.get('color_space') == 'bt709':
        cmd += ['-vf', 'scale=in_color_matrix=bt709:out_range=pc']
    cmd += ['-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
    thumb_height = max(1, round(args.thumb_width * height / width))
    metric_height = max(1, round(320 * height / width))
    rows, sheets, tiles, native_written = [], [], [], []
    last_metric = None
    last_image = None
    last_selected = -1

    def add_tile(image, frame, seconds):
        tile = np.zeros((thumb_height + 24, args.thumb_width, 3), np.uint8)
        tile[:thumb_height] = cv2.resize(image, (args.thumb_width, thumb_height), interpolation=cv2.INTER_AREA)
        colour = (150, 255, 180) if frame in event_map else (235, 235, 235)
        cv2.putText(tile, f'{frame:04d} | {seconds:.3f}s', (5, thumb_height + 18), 0, .42, colour, 1, cv2.LINE_AA)
        tiles.append(tile)
        if len(tiles) >= args.per_sheet:
            flush_tiles()

    def flush_tiles():
        if not tiles:
            return
        batch = list(tiles)
        while len(batch) % args.columns:
            batch.append(np.zeros_like(batch[0]))
        canvas = np.vstack([np.hstack(batch[i:i + args.columns]) for i in range(0, len(batch), args.columns)])
        name = f'sheet_{len(sheets) + 1:03d}.jpg'
        if not cv2.imwrite(str(out / name), canvas, [cv2.IMWRITE_JPEG_QUALITY, 94]):
            raise OSError(f'Cannot write {name}')
        sheets.append(name)
        tiles.clear()

    with tempfile.TemporaryFile() as errors:
        decoder = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors)
        try:
            frame = 0
            while True:
                data = read_exact(decoder.stdout, width * height * 3)
                if not data:
                    break
                if len(data) != width * height * 3:
                    raise RuntimeError('Truncated raw frame.')
                image = np.frombuffer(data, np.uint8).reshape(height, width, 3)
                metric = cv2.resize(image, (320, metric_height), interpolation=cv2.INTER_AREA)
                hsv = cv2.cvtColor(metric, cv2.COLOR_BGR2HSV)
                delta = 0.0 if last_metric is None else float(np.abs(metric.astype(np.float32) - last_metric).mean())
                exact_pts = timestamps[frame] if frame < len(timestamps) else None
                seconds = exact_pts - origin if exact_pts is not None else frame / fps
                row = {'frame': frame, 'time_seconds': round(seconds, 6), 'source_pts_seconds': exact_pts,
                       'timestamp_estimated': exact_pts is None, 'mean_delta': round(delta, 5),
                       'brightness': round(float(hsv[..., 2].mean()), 4),
                       'colour_fraction': round(float((hsv[..., 1] > 35).mean()), 5),
                       'planned_event': frame in event_map}
                rows.append(row)
                if args.all_frames or frame % args.step == 0:
                    add_tile(image, frame, seconds)
                    last_selected = frame
                if frame in native:
                    name = f'frame_{frame:06d}.png'
                    if not cv2.imwrite(str(out / name), image):
                        raise OSError(f'Cannot write {name}')
                    native_written.append(name)
                last_metric, last_image = metric, image.copy()
                frame += 1
            decoder.stdout.close()
            code = decoder.wait()
            if code:
                errors.seek(0)
                raise RuntimeError(errors.read().decode('utf-8', 'replace'))
        except BaseException:
            decoder.kill()
            decoder.wait()
            raise
    if not rows:
        raise ValueError('No frames decoded.')
    if last_selected != len(rows) - 1:
        add_tile(last_image, len(rows) - 1, rows[-1]['time_seconds'])
    flush_tiles()
    missing_native = sorted(native - set(range(len(rows))))
    if missing_native:
        raise ValueError(f'Requested frames outside decoded range: {missing_native}')
    holds = []
    for start, end in plan.get('holds', []):
        if end > len(rows):
            raise ValueError('A planned hold extends beyond the decoded video.')
        peak = max((r['mean_delta'] for r in rows[start + 1:end]), default=0.0)
        holds.append({'start': start, 'end': end, 'max_delta': peak,
                      'within_threshold': peak <= args.hold_threshold})
    frame_count_matches = None if 'frame_count' not in plan else len(rows) == plan['frame_count']
    planned_fps = rational(plan.get('fps'))
    fps_matches = None if not planned_fps else not is_vfr and abs(fps - planned_fps) < .001
    events_outside = sorted(set(event_map) - set(range(len(rows))))
    summary = {'input': str(video), 'decoded_frames': len(rows), 'width': width, 'height': height,
               'fps_average': fps, 'variable_frame_timestamps': is_vfr,
               'timestamps_complete': len(timestamps) == len(rows) and all(x is not None for x in timestamps),
               'sheet_coverage': 'every_decoded_frame' if args.all_frames else f'every_{args.step}_frames_plus_last',
               'sheets': sheets, 'native_images': native_written,
               'change_candidates': [r['frame'] for r in rows[1:] if r['mean_delta'] > args.change_threshold],
               'dark_frame_candidates': [r['frame'] for r in rows if r['brightness'] < 2],
               'frame_count_matches_plan': frame_count_matches, 'fps_matches_plan': fps_matches,
               'events_outside_video': events_outside, 'static_holds': holds,
               'visual_inspection_performed_by_script': False,
               'note': 'Candidates and threshold checks are evidence only; they do not establish semantic correctness.'}
    if args.audio_onsets:
        audio_streams = [s for s in probe.get('streams', []) if s.get('codec_type') == 'audio']
        if audio_streams:
            if args.audio_stream >= len(audio_streams):
                raise ValueError('Requested audio stream does not exist.')
            summary['audio_onsets'] = audio_candidates(video, args.audio_stream)
        else:
            summary['audio_onsets'] = {'status': 'no_audio', 'candidates': []}
    (out / 'frames.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
    with (out / 'frames.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    figures = ''.join(f'<figure><a href="{name}"><img loading="lazy" src="{name}"></a><figcaption>{name}</figcaption></figure>' for name in sheets)
    native_links = ''.join(f'<li><a href="{name}">{name}</a></li>' for name in native_written)
    title = html.escape(video.name)
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    page += f'<title>Frame evidence — {title}</title><style>body{{font:16px/1.6 sans-serif;background:#171a19;color:#ddd;margin:0}}main{{max-width:1600px;margin:auto;padding:24px}}img{{width:100%}}figure{{margin:24px 0}}a{{color:#9ed6b9}}</style><main>'
    page += f'<h1>{title}</h1><p>{len(rows)} decoded frames; {html.escape(summary["sheet_coverage"])}. Frame numbers start at 0. Candidate changes are not verified shot cuts.</p>'
    page += figures + '<h2>Native-size frames</h2><ul>' + native_links + '</ul></main></html>'
    (out / 'review.html').write_text(page, encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ['decoded_frames', 'width', 'height', 'sheet_coverage',
          'frame_count_matches_plan', 'fps_matches_plan', 'variable_frame_timestamps']}, indent=2))
    print(f'Review: {out / "review.html"}')
    return 2 if frame_count_matches is False or fps_matches is False or events_outside or any(not h['within_threshold'] for h in holds) else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'Error: {error}', file=sys.stderr)
        raise SystemExit(1)
