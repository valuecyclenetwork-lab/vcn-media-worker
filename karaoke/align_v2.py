"""
VCN lyric timing engine **align-v2** (isolated from the proven align-v1 path).

align-v1 (see `_time_lyrics` in app.py) seeds each lyric line into a
proportional time window derived from lyric word counts, then forced-aligns
inside that guessed window. When a seed is wrong the timestamps stay wrong.

align-v2 removes proportional seeding entirely:

    original audio
      -> vocal-focused preprocessing (timeline preserving)
      -> whole-track recognition with word timestamps
      -> global monotonic match of the AUTHORITATIVE lyrics to recognised words
      -> word timestamps (matched / interpolated, each scored)
      -> line timestamps derived from the words

The authoritative VCN lyrics remain the source of truth: the ASR transcript is
used ONLY to locate the performance in time. Word timing is always preserved,
including when confidence is low, so it can later power word-by-word Karaoke.

This module is imported by app.py and is only reachable through /v1/align2.
It never touches Karaoke rendering, Karaoke pricing or align-v1.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

ENGINE_VERSION = "align-v2"
SCHEMA_VERSION = 2

# Recognition model for align-v2 (baked into the image; falls back to the
# align-v1 model if the larger one cannot be loaded on this machine).
V2_WHISPER_MODEL = os.environ.get("KARAOKE_ALIGN_V2_MODEL", "medium")
V2_FALLBACK_MODEL = os.environ.get("KARAOKE_WHISPER_MODEL", "small")

# Vocal-focused preprocessing.
#   "dsp"    – deterministic FFmpeg vocal emphasis (default: seconds, no RAM)
#   "demucs" – Demucs `mdx_extra_q` two-stem vocals (heavier, higher isolation)
V2_SEPARATOR = os.environ.get("KARAOKE_ALIGN_V2_SEPARATOR", "dsp")
V2_DEMUCS_MODEL = os.environ.get("KARAOKE_ALIGN_V2_DEMUCS_MODEL", "mdx_extra_q")


# --------------------------------------------------------------- text utils
_PUNCT = re.compile(r"[^\w\s']", re.UNICODE)
_FILLERS = {"oh", "ooh", "oooh", "ah", "aah", "yeah", "yea", "eh", "hey", "hmm",
            "mmm", "na", "la", "woah", "whoa", "uh", "huh", "ay", "aye"}
_CONTRACTIONS = {
    "im": "i am", "ill": "i will", "ive": "i have", "id": "i would",
    "youre": "you are", "youll": "you will", "youve": "you have",
    "hes": "he is", "shes": "she is", "its": "it is", "thats": "that is",
    "were": "we are", "weve": "we have", "well": "we will", "theyre": "they are",
    "theyll": "they will", "dont": "do not", "doesnt": "does not",
    "didnt": "did not", "cant": "can not", "cannot": "can not",
    "wont": "will not", "aint": "is not", "gonna": "going to",
    "wanna": "want to", "gotta": "got to", "gimme": "give me",
    "lemme": "let me", "cos": "because", "cuz": "because", "dey": "they",
    "dem": "them", "wey": "which", "abeg": "please",
}


def _fold(word: str) -> str:
    """Casing / punctuation / accent insensitive comparison form."""
    w = unicodedata.normalize("NFKD", str(word or ""))
    w = "".join(c for c in w if not unicodedata.combining(c))
    w = _PUNCT.sub(" ", w.lower()).strip()
    w = w.replace("'", "")
    # elongated sung words: "waaaay" -> "way", "ohhhh" -> "oh"
    w = re.sub(r"(.)\1{2,}", r"\1\1", w)
    return w


def _expand(word: str) -> List[str]:
    folded = _fold(word)
    if not folded:
        return []
    expanded = _CONTRACTIONS.get(folded)
    if expanded:
        return expanded.split()
    return folded.split()


def _similar(a: str, b: str) -> float:
    """1.0 identical, 0.0 unrelated. Tolerates minor ASR substitutions."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) > 3 and len(b) > 3 and (a.startswith(b) or b.startswith(a)):
        return 0.86
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    # collapsed repeats: sung "goooo" vs recognised "go"
    if ratio < 0.7:
        ca = re.sub(r"(.)\1+", r"\1", a)
        cb = re.sub(r"(.)\1+", r"\1", b)
        if ca == cb:
            return 0.9
    return ratio


def normalise_lyric_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.fullmatch(r"[\[\(\*]+\s*[A-Za-z0-9 \-:']+\s*[\]\)\*]+", line):
            continue
        lines.append(line)
    return lines


# ------------------------------------------------------- vocal preprocessing
def prepare_vocal_track(work: str, src: str, diag: Dict[str, Any]) -> str:
    """
    Produces a vocal-focused 16 kHz mono WAV **in the original song timeline**.

    Guarantees: no trimming, no silence removal, no padding, no tempo change —
    only band limiting / centre emphasis / level normalisation, all of which are
    sample-aligned. Timestamps therefore remain original song time.
    """
    t0 = time.time()
    out = os.path.join(work, "vocals_v2.wav")

    if V2_SEPARATOR == "demucs":
        try:
            stem = _demucs_vocals(work, src, diag)
            _ffmpeg_emphasis(stem, out, emphasise=False)
            diag["v2_separation"] = f"demucs:{V2_DEMUCS_MODEL}"
            diag["v2_separation_seconds"] = round(time.time() - t0, 1)
            return out
        except Exception as exc:  # noqa: BLE001 — fall back, never fail the job
            diag["v2_separation_error"] = str(exc)[:200]

    _ffmpeg_emphasis(src, out, emphasise=True)
    diag["v2_separation"] = "dsp:centre+bandpass+dynaudnorm"
    diag["v2_separation_seconds"] = round(time.time() - t0, 1)
    return out


def _ffmpeg_emphasis(src: str, out: str, emphasise: bool) -> None:
    if emphasise:
        # Centre-channel emphasis (vocals sit centre) + vocal band limiting.
        # `dynaudnorm` is a gain curve only: it never moves a sample in time.
        chain = ("highpass=f=140,lowpass=f=7000,"
                 "dynaudnorm=f=250:g=15:p=0.9,"
                 "aresample=16000:resampler=soxr")
    else:
        chain = "aresample=16000:resampler=soxr"
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", src, "-vn",
           "-af", chain, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("vocal preprocessing failed")


def _demucs_vocals(work: str, src: str, diag: Dict[str, Any]) -> str:
    outdir = os.path.join(work, "sep_v2")
    cmd = ["python", "-m", "demucs", "-n", V2_DEMUCS_MODEL, "--two-stems", "vocals",
           "-d", "cpu", "-o", outdir, "--filename", "{stem}.{ext}", src]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3000)
    stem = os.path.join(outdir, V2_DEMUCS_MODEL, "vocals.wav")
    if proc.returncode != 0 or not os.path.exists(stem):
        raise RuntimeError("demucs vocals stem unavailable")
    return stem


# ------------------------------------------------------ whole-track ASR pass
def recognise_words(models: Dict[str, Any], audio, language_hint: Optional[str],
                    diag: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """
    Whole-song recognition with word timestamps. The transcript is NOT
    authoritative — it only tells us where singing happens in time.
    """
    whisperx = models["whisperx"]
    asr = models["asr_v2"]
    t0 = time.time()
    try:
        result = asr.transcribe(audio, batch_size=8, language=language_hint)
    except ValueError:
        result = asr.transcribe(audio, batch_size=8, language=None)
    language = result.get("language") or language_hint or "en"
    segments = result.get("segments") or []
    diag["v2_asr_seconds"] = round(time.time() - t0, 1)
    diag["v2_asr_segments"] = len(segments)
    diag["v2_language"] = language

    t1 = time.time()
    words: List[Dict[str, Any]] = []
    try:
        model_a, meta = models["align_model"](language)
        aligned = whisperx.align(segments, model_a, meta, audio, models["device"],
                                 return_char_alignments=False)
        for seg in aligned.get("segments") or []:
            for w in seg.get("words") or []:
                if w.get("start") is None or w.get("end") is None:
                    continue
                for token in _expand(w.get("word", "")):
                    words.append({
                        "token": token,
                        "start": float(w["start"]),
                        "end": float(w["end"]),
                        "score": float(w.get("score") or 0.0),
                    })
    except Exception as exc:  # noqa: BLE001
        diag["v2_align_error"] = str(exc)[:250]

    if not words:
        # Segment-level fallback keeps the pipeline monotonic and usable.
        for seg in segments:
            s, e = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
            toks = [t for wtext in str(seg.get("text", "")).split() for t in _expand(wtext)]
            if not toks or e <= s:
                continue
            step = (e - s) / len(toks)
            for i, tok in enumerate(toks):
                words.append({"token": tok, "start": s + i * step,
                              "end": s + (i + 1) * step, "score": 0.2})
        diag["v2_word_source"] = "segment_fallback"
    else:
        diag["v2_word_source"] = "forced_word"

    words.sort(key=lambda w: (w["start"], w["end"]))
    diag["v2_alignment_seconds"] = round(time.time() - t1, 1)
    diag["v2_recognised_words"] = len(words)
    return words, language


# ------------------------------------------------- global monotonic matching
GAP_LYRIC = -0.55      # authoritative word with no recognised counterpart
GAP_ASR = -0.35        # recognised extra (ad-lib, filler, repeat)
MATCH_FLOOR = 0.62     # below this a pair is not considered a match


def match_globally(lyric_tokens: List[str], asr_words: List[Dict[str, Any]]
                   ) -> List[Optional[int]]:
    """
    Needleman-Wunsch global alignment between the authoritative lyric tokens and
    the time-ordered recognised words. Being a strictly monotonic DP, a repeated
    chorus can never be matched to an earlier or later occurrence: matches are
    forced to advance through both sequences in order.

    Returns, for each lyric token, the index of its matched recognised word or
    None.
    """
    n, m = len(lyric_tokens), len(asr_words)
    if not n or not m:
        return [None] * n

    # Score matrix rows are kept as lists of floats; back-pointers as bytes.
    prev = [j * GAP_ASR for j in range(m + 1)]
    ptr: List[bytearray] = []
    for i in range(1, n + 1):
        cur = [i * GAP_LYRIC] + [0.0] * m
        row = bytearray(m + 1)
        row[0] = 2  # came from above (lyric gap)
        a = lyric_tokens[i - 1]
        for j in range(1, m + 1):
            sim = _similar(a, asr_words[j - 1]["token"])
            diag_score = prev[j - 1] + (sim if sim >= MATCH_FLOOR else -0.5 + sim * 0.5)
            up = prev[j] + GAP_LYRIC
            left = cur[j - 1] + GAP_ASR
            best = diag_score
            move = 1
            if up > best:
                best, move = up, 2
            if left > best:
                best, move = left, 3
            cur[j] = best
            row[j] = move
        ptr.append(row)
        prev = cur

    out: List[Optional[int]] = [None] * n
    i, j = n, m
    while i > 0 and j > 0:
        move = ptr[i - 1][j]
        if move == 1:
            sim = _similar(lyric_tokens[i - 1], asr_words[j - 1]["token"])
            if sim >= MATCH_FLOOR:
                out[i - 1] = j - 1
            i -= 1
            j -= 1
        elif move == 2:
            i -= 1
        else:
            j -= 1
    return out


# ------------------------------------------------------------ timing builder
def build_timing(lines: List[str], asr_words: List[Dict[str, Any]], language: str,
                 audio_seconds: float, diag: Dict[str, Any]) -> Dict[str, Any]:
    """Word timing first, line timing derived from it. Nothing is discarded."""
    # Flatten authoritative lyrics into comparison tokens, remembering the
    # display word and line each token belongs to.
    tokens: List[str] = []
    owners: List[Tuple[int, int]] = []          # (line index, word index in line)
    display: List[List[str]] = []
    for li, line in enumerate(lines):
        words = line.split()
        display.append(words)
        for wi, w in enumerate(words):
            parts = _expand(w)
            if not parts:
                parts = [_fold(w) or w.lower()]
            for p in parts:
                tokens.append(p)
                owners.append((li, wi))

    matches = match_globally(tokens, asr_words)

    # Collapse token matches back onto display words (a word may expand to
    # several tokens, e.g. "I'll" -> "i will").
    word_slots: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for idx, owner in enumerate(owners):
        mi = matches[idx]
        if mi is None:
            continue
        w = asr_words[mi]
        slot = word_slots.get(owner)
        if slot is None:
            word_slots[owner] = {"start": w["start"], "end": w["end"],
                                 "score": w["score"], "hits": 1}
        else:
            slot["start"] = min(slot["start"], w["start"])
            slot["end"] = max(slot["end"], w["end"])
            slot["score"] = max(slot["score"], w["score"])
            slot["hits"] += 1

    # Build the ordered word list with matched anchors, then interpolate the
    # gaps BETWEEN trusted anchors only.
    flat: List[Dict[str, Any]] = []
    for li, words in enumerate(display):
        for wi, text in enumerate(words):
            slot = word_slots.get((li, wi))
            if slot:
                flat.append({"line": li, "word": text,
                             "start": float(slot["start"]), "end": float(slot["end"]),
                             "score": round(float(slot["score"]), 3),
                             "method": "matched"})
            else:
                flat.append({"line": li, "word": text, "start": None, "end": None,
                             "score": 0.0, "method": "unmatched"})

    # Enforce monotonicity on anchors (defensive; the DP already guarantees it).
    last = -1.0
    for w in flat:
        if w["start"] is None:
            continue
        if w["start"] < last:
            w["start"] = last
        if w["end"] <= w["start"]:
            w["end"] = w["start"] + 0.12
        last = w["start"]

    anchors = [i for i, w in enumerate(flat) if w["start"] is not None]
    interpolated = 0
    unmatched = 0
    if anchors:
        for i, w in enumerate(flat):
            if w["start"] is not None:
                continue
            before = next((a for a in reversed(anchors) if a < i), None)
            after = next((a for a in anchors if a > i), None)
            if before is not None and after is not None:
                span_start = flat[before]["end"]
                span_end = flat[after]["start"]
                total = max(after - before - 1, 1)
                slot = max((span_end - span_start) / total, 0.06)
                pos = i - before - 1
                w["start"] = span_start + pos * slot
                w["end"] = min(w["start"] + slot, span_end)
                w["method"] = "interpolated"
                w["score"] = 0.25
                interpolated += 1
            else:
                # Outside the trusted anchor range: never guess a timestamp.
                w["method"] = "unmatched"
                unmatched += 1
    else:
        unmatched = len(flat)

    # ---- line timing derived strictly from word timing -------------------
    out_lines: List[Dict[str, Any]] = []
    for li, words in enumerate(display):
        items = [w for w in flat if w["line"] == li]
        timed = [w for w in items if w["start"] is not None]
        trusted = [w for w in timed if w["method"] == "matched"]
        basis = trusted or timed
        if basis:
            start = min(float(w["start"]) for w in basis)
            end = max(float(w["end"]) for w in basis)
            line_source = "words" if trusted else "interpolated_words"
        elif out_lines:
            start = float(out_lines[-1]["end"]) + 0.2
            end = start + 1.6
            line_source = "fallback"
        else:
            start, end = 0.0, 1.6
            line_source = "fallback"
        end = max(end, start + 0.8)
        line_words = [{"word": w["word"],
                       "start": round(float(w["start"]), 3) if w["start"] is not None else None,
                       "end": round(float(w["end"]), 3) if w["end"] is not None else None,
                       "score": round(float(w["score"]), 3),
                       "method": w["method"]} for w in items]
        scores = [w["score"] for w in trusted]
        out_lines.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": lines[li],
            "words": line_words,
            "source": line_source,
            "confidence": round(sum(scores) / len(scores), 3) if scores else 0.0,
        })

    # Line starts must stay chronological.
    for i in range(1, len(out_lines)):
        if out_lines[i]["start"] < out_lines[i - 1]["start"]:
            out_lines[i]["start"] = out_lines[i - 1]["start"]
            out_lines[i]["end"] = max(out_lines[i]["end"], out_lines[i]["start"] + 0.8)

    total_words = len(flat)
    matched = sum(1 for w in flat if w["method"] == "matched")
    scores = sorted(w["score"] for w in flat if w["method"] == "matched")
    mean_conf = (sum(scores) / len(scores)) if scores else 0.0
    median_conf = scores[len(scores) // 2] if scores else 0.0
    low_conf = sum(1 for s in scores if s < 0.5)
    coverage = ((matched + interpolated) / total_words) if total_words else 0.0

    diag["v2_total_words"] = total_words
    diag["v2_matched_words"] = matched
    diag["v2_interpolated_words"] = interpolated
    diag["v2_unmatched_words"] = unmatched

    return {
        "lines": out_lines,
        # Word data is ALWAYS retained. `mode` only advises the renderer which
        # presentation the current production ASS builder should use.
        "mode": "word" if (matched / total_words if total_words else 0) >= 0.85 and mean_conf >= 0.6 else "line",
        "confidence": round(mean_conf, 3),
        "coverage": round(coverage, 3),
        "language": language,
        "source": "stored",
        "engine_version": ENGINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "model": model_fingerprint(),
        "timing_source": "global_monotonic_word_match",
        "metrics": {
            "total_lyric_words": total_words,
            "matched_words": matched,
            "interpolated_words": interpolated,
            "unmatched_words": unmatched,
            "coverage": round(coverage, 3),
            "mean_confidence": round(mean_conf, 3),
            "median_confidence": round(median_conf, 3),
            "low_confidence_words": low_conf,
            "recognised_words": len(asr_words),
            "audio_seconds": round(audio_seconds, 3),
            "first_word_start": next((w["start"] for w in flat if w["start"] is not None), None),
            "last_word_end": max((w["end"] for w in flat if w["end"] is not None), default=None),
        },
    }


def model_fingerprint() -> str:
    sep = f"demucs-{V2_DEMUCS_MODEL}" if V2_SEPARATOR == "demucs" else "dsp-centre-bandpass"
    return f"{sep}/faster-whisper-{V2_WHISPER_MODEL}/wav2vec2-forced-align/global-monotonic-dp"


# ---------------------------------------------------------------- entrypoint
def time_lyrics_v2(models: Dict[str, Any], work: str, src: str, lyrics: str,
                   language_hint: Optional[str], diag: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.time()
    lines = normalise_lyric_lines(lyrics)
    if not lines:
        raise RuntimeError("align-v2 requires authoritative stored lyrics")

    vocal = prepare_vocal_track(work, src, diag)
    whisperx = models["whisperx"]
    audio = whisperx.load_audio(vocal)
    audio_seconds = float(len(audio)) / 16000.0

    asr_words, language = recognise_words(models, audio, language_hint, diag)
    timing = build_timing(lines, asr_words, language, audio_seconds, diag)
    diag["v2_total_seconds"] = round(time.time() - t0, 1)
    timing["runtime_seconds"] = diag["v2_total_seconds"]
    return timing
